"""Crash-safe per-turn maintenance for the bundled fleet lifecycle plugin.

The plugin deliberately owns only derived checkpoint metadata.  The canonical
vault is never passed to gbrain, and optional integration failures are recorded
for a later turn instead of raising into Hermes' delivery path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses the ownership-token fallback.
    fcntl = None

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic|token)\s+[^\s,;]+"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|pk)_[A-Za-z0-9_-]{16,}\b"),
)
_MAX_TEXT = 600
_DEFAULT_GBRAIN = ("gbrain", "sync")

logger = logging.getLogger(__name__)


def redact_for_export(value: Any) -> str:
    """Bounded plain text with credential-shaped values removed."""
    text = str(value or "").replace("\x00", " ")[:_MAX_TEXT]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def profile_name_for_home(home: Path) -> str:
    """Return a stable, profile-local vault namespace without ambient-home fallback."""
    if home.parent.name == "profiles":
        return home.name
    if home.name == ".hermes":
        return "default"
    # Custom homes must not collapse into default: their resolved identity remains stable
    # across processes without exposing the local path in vault filenames.
    return "custom-" + hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:16]


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(raw, path)
    finally:
        try:
            os.unlink(raw)
        except FileNotFoundError:
            pass


class LifecycleController:
    """Profile-local durable side effects for one completed or interrupted turn."""

    _locks_guard = threading.Lock()
    _process_locks: dict[Path, threading.RLock] = {}

    @classmethod
    def _process_lock_for(cls, home: Path) -> threading.RLock:
        """Return the in-process lock dedicated to one resolved profile home."""
        key = home.resolve()
        with cls._locks_guard:
            return cls._process_locks.setdefault(key, threading.RLock())

    def __init__(self, home: Path, config: dict[str, Any] | None = None, *,
                 runner: Callable[..., Any] | None = None) -> None:
        self.home = Path(home)
        self.config = config or {}
        self.profile = profile_name_for_home(self.home)
        configured_vault_root = str(self.config.get("vault_root") or Path.home() / ".agent-vault")
        self.vault_root = Path(os.path.expanduser(configured_vault_root)).resolve()
        self.state_path = self.home / "state" / "fleet-session-lifecycle.json"
        self.lock_path = self.home / "state" / "fleet-session-lifecycle.lock"
        prior_namespace = self._state().get("vault_profile")
        if isinstance(prior_namespace, str) and prior_namespace:
            self.profile = prior_namespace
        self._uses_default_runner = runner is None
        self.runner = runner or self._run

    def _state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _run(argv: list[str], **kwargs: Any) -> int:
        return subprocess.run(argv, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=kwargs.get("timeout", 5), cwd=kwargs.get("cwd") or None,
                              env=kwargs.get("env")).returncode

    @staticmethod
    def _git_root(cwd: str) -> Path | None:
        """Locate the nearest worktree root, including a linked-worktree .git file."""
        if not cwd:
            return None
        try:
            current = Path(cwd).resolve()
        except OSError:
            return None
        for candidate in (current, *current.parents):
            if (candidate / ".git").exists():
                return candidate
        return None

    def _command_env(self) -> dict[str, str]:
        """Build a profile-scoped, secret-scrubbed environment for optional commands."""
        from tools.environments.local import build_subprocess_env
        return build_subprocess_env(
            scrub_secrets=True, inherit_profile_home=False,
            extra={"HERMES_HOME": str(self.home)}, strip_launch_profile=True,
        )

    def _resolved_command(self, command: list[str]) -> list[str] | None:
        """Resolve the executable using Hermes' platform resolver before execution."""
        from hermes_platform.resolver import locate_command
        resolved = locate_command(command[0]).command
        return [*resolved, *command[1:]] if resolved else None

    def _invoke(self, command: list[str], **kwargs: Any) -> int:
        """Invoke test-injected adapters unchanged; production uses resolved command and scrubbed env."""
        if not self._uses_default_runner:
            return self.runner(command, **kwargs)
        resolved = self._resolved_command(command)
        if resolved is None:
            raise FileNotFoundError(command[0])
        return self.runner(resolved, env=self._command_env(), **kwargs)

    @contextmanager
    def _lease(self) -> Iterator[bool]:
        """Acquire one profile lease without unlinking another reclaimer's lock.

        On POSIX an advisory flock is released by the kernel when its owner dies,
        so a leftover metadata file is safely reclaimed by the next holder.  The
        ownership token makes cleanup conditional: a late finalizer cannot remove
        a lock it no longer owns.
        """
        with self._process_lock_for(self.home):
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            token = f"{os.getpid()}:{uuid.uuid4().hex}"
            locked = False
            try:
                if fcntl is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        yield False
                        return
                    locked = True
                else:  # pragma: no cover - Windows fallback retains exclusive-create semantics.
                    try:
                        os.close(fd)
                        fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        yield False
                        return
                os.ftruncate(fd, 0)
                os.write(fd, token.encode("ascii"))
                os.fsync(fd)
                yield True
            finally:
                if locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _checkpoint_path(self) -> Path:
        return self.vault_root / self.profile / "daily" / f"{time.strftime('%Y-%m-%d')}-hermes-lifecycle.md"

    def _append_checkpoint(self, event: dict[str, Any]) -> None:
        path = self._checkpoint_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = "\n".join((
            "\n<!-- hermes-lifecycle incremental checkpoint -->",
            f"- event_id: {event['event_id']}",
            f"- session_id: {event['session_id']}", f"- turn_id: {event['turn_id']}",
            f"- completed: {str(event['completed']).lower()}", f"- interrupted: {str(event['interrupted']).lower()}",
            f"- user: {event['user']}", f"- response: {event['response']}",
        )) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _checkpoint_has(self, event_id: str) -> bool:
        try:
            return f"- event_id: {event_id}" in self._checkpoint_path().read_text(encoding="utf-8")
        except OSError:
            return False

    def _append_full_wrapup(self, session_id: str, reason: str) -> None:
        """Write the session-bound final checkpoint without creating an agent turn."""
        path = self._checkpoint_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = "\n".join((
            "\n<!-- hermes-lifecycle full-wrapup checkpoint -->",
            f"- session_id: {redact_for_export(session_id)}",
            f"- reason: {redact_for_export(reason)}",
            "- transcript_checkpointed: true",
            "- delivery_message: none",
        )) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def full_wrapup(self, *, session_id: str, reason: str = "") -> None:
        """Persist the final `/wrapup`-equivalent boundary exactly once, fail isolated.

        Finalization is intentionally filesystem-only: invoking a slash command or
        model turn here could recursively fire lifecycle hooks or race response delivery.
        """
        if not session_id:
            return
        try:
            with self._lease() as acquired:
                if not acquired:
                    return
                state = self._state()
                state.setdefault("vault_profile", self.profile)
                finalized = [str(item) for item in (state.get("finalized_sessions") or ())]
                final_id = hashlib.sha256(session_id.encode()).hexdigest()
                if final_id in finalized:
                    return
                self._append_full_wrapup(session_id, reason)
                finalized.append(final_id)
                # Preserve arrival order, not lexical hash order, when pruning recency.
                state["finalized_sessions"] = finalized[-512:]
                state["updated_at"] = int(time.time())
                _atomic_json(self.state_path, state)
        except Exception:
            logger.exception("fleet-session-lifecycle: full wrap-up checkpoint failed for this session")
            return

    def _pinecone_index(self, event: dict[str, Any], state: dict[str, Any]) -> None:
        """Queue only redacted derived text for an explicitly configured indexer.

        The queue lives under the active Hermes profile, never in the Obsidian vault.
        A deployment can point ``pinecone_command`` at its approved adapter; no
        credentials, vault path, or raw history are put on argv.
        """
        command = self.config.get("pinecone_command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(x, str) and x for x in command)
        ):
            return
        queue = self.home / "state" / "fleet-session-lifecycle-pinecone.jsonl"
        queue.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": "fleet-lifecycle.v1", "profile": self.profile,
                   "session_id": event["session_id"], "turn_id": event["turn_id"],
                   "summary": f"user: {event['user']}\\nresponse: {event['response']}"}
        with queue.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        pinecone = state.setdefault("pinecone", {})
        try:
            code = self._invoke(command + ["--input", str(queue)], timeout=float(self.config.get("pinecone_timeout_seconds", 5)))
        except FileNotFoundError:
            pinecone.update(last_status="unavailable", write_pending=True)
            return
        except Exception:
            logger.exception("fleet-session-lifecycle: pinecone indexer failed; queued for a later turn")
            pinecone.update(last_status="failed", write_pending=True)
            return
        if code == 0:
            try:
                queue.unlink()
            except FileNotFoundError:
                pass
            pinecone.update(last_status="ok", write_pending=False)
        else:
            pinecone.update(last_status="write_pending", write_pending=True)

    def _gbrain_sync(self, cwd: str, state: dict[str, Any]) -> None:
        root = self._git_root(cwd)
        if root is None:
            return
        gbrain = state.setdefault("gbrain", {})
        now = time.time()
        try:
            retry_seconds = max(0.0, float(self.config.get("gbrain_retry_seconds", 300)))
        except (TypeError, ValueError):
            retry_seconds = 300.0
        if gbrain.get("write_pending") and now - float(gbrain.get("last_attempt_at") or 0) < retry_seconds:
            return
        try:
            debounce = max(0.0, float(self.config.get("debounce_seconds", 30)))
        except (TypeError, ValueError):
            debounce = 30.0
        if (str(gbrain.get("last_repo") or "") == str(root)
                and now - float(gbrain.get("last_attempt_at") or 0) < debounce):
            gbrain["last_status"] = "debounced"
            return
        gbrain["last_attempt_at"] = now
        command: list[str] | Any = list(_DEFAULT_GBRAIN) if "gbrain_command" not in self.config else self.config["gbrain_command"]
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            return
        try:
            code = self._invoke(command, timeout=float(self.config.get("gbrain_timeout_seconds", 5)), cwd=str(root))
        except FileNotFoundError:
            gbrain.update(last_status="unavailable", write_pending=True)
            return
        except Exception:
            logger.exception("fleet-session-lifecycle: gbrain sync failed; queued for a later turn")
            gbrain.update(last_status="failed", write_pending=True)
            return
        if code == 0:
            gbrain.update(last_status="ok", write_pending=False, last_repo=str(root))
        else:
            gbrain.update(last_status="write_pending", write_pending=True, last_repo=str(root))

    def after_turn(self, *, session_id: str, turn_id: str, completed: bool, interrupted: bool,
                   user_message: Any, assistant_response: Any, conversation_history: list[Any], cwd: str) -> None:
        """Durably checkpoint exactly once per turn; never propagate a lifecycle failure."""
        if not session_id or not turn_id:
            return
        event_id = hashlib.sha256(f"{session_id}\0{turn_id}".encode()).hexdigest()
        try:
            with self._lease() as acquired:
                if not acquired:
                    return
                state = self._state()
                state.setdefault("vault_profile", self.profile)
                processed = [str(item) for item in (state.get("processed_turns") or ())]
                if event_id in processed:
                    return
                pending = state.setdefault("pending_turns", {})
                event = pending.get(event_id)
                if not isinstance(event, dict):
                    event = {"event_id": event_id, "session_id": redact_for_export(session_id),
                             "turn_id": redact_for_export(turn_id), "completed": bool(completed),
                             "interrupted": bool(interrupted), "user": redact_for_export(user_message),
                             "response": redact_for_export(assistant_response)}
                    pending[event_id] = event
                    # Intent is durable before any side effect; a crash can now be recovered.
                    _atomic_json(self.state_path, state)
                if not self._checkpoint_has(event_id):
                    self._append_checkpoint(event)
                processed.append(event_id)
                # Bound ledger preserves turn arrival order so retry dedup retains the newest ids.
                state["processed_turns"] = processed[-512:]
                pending.pop(event_id, None)
                state["updated_at"] = int(time.time())
                self._pinecone_index(event, state)
                self._gbrain_sync(cwd, state)
                _atomic_json(self.state_path, state)
        except Exception:
            # Hooks must never block/damage response delivery; the core already persisted the transcript first.
            logger.exception("fleet-session-lifecycle: per-turn checkpoint failed for %s",
                             redact_for_export(session_id))
            return
