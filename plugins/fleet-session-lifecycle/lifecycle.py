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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

_SECRET_PATTERNS = (
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
    """Profile name from its resolved Hermes home, never the ambient process home."""
    if home.parent.name == "profiles":
        return home.name
    return "default"


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

    _process_lock = threading.RLock()

    def __init__(self, home: Path, config: dict[str, Any] | None = None, *,
                 runner: Callable[..., Any] | None = None) -> None:
        self.home = Path(home)
        self.config = config or {}
        self.profile = profile_name_for_home(self.home)
        self.vault_root = Path(str(self.config.get("vault_root") or Path.home() / ".agent-vault"))
        self.state_path = self.home / "state" / "fleet-session-lifecycle.json"
        self.lock_path = self.home / "state" / "fleet-session-lifecycle.lock"
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
                              timeout=kwargs.get("timeout", 5), cwd=kwargs.get("cwd") or None).returncode

    @contextmanager
    def _lease(self) -> Iterator[bool]:
        """Cross-thread/process best-effort exclusion; failures always leave the turn deliverable."""
        with self._process_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                yield False
                return
            try:
                os.write(fd, str(os.getpid()).encode())
                yield True
            finally:
                os.close(fd)
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _checkpoint_path(self) -> Path:
        return self.vault_root / self.profile / "daily" / "hermes-lifecycle.md"

    def _append_checkpoint(self, event: dict[str, Any]) -> None:
        path = self._checkpoint_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = "\n".join((
            "\n<!-- hermes-lifecycle incremental checkpoint -->",
            f"- session_id: {event['session_id']}", f"- turn_id: {event['turn_id']}",
            f"- completed: {str(event['completed']).lower()}", f"- interrupted: {str(event['interrupted']).lower()}",
            f"- user: {event['user']}", f"- response: {event['response']}",
        )) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

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
                finalized = {str(item) for item in (state.get("finalized_sessions") or ())}
                final_id = hashlib.sha256(session_id.encode()).hexdigest()
                if final_id in finalized:
                    return
                self._append_full_wrapup(session_id, reason)
                finalized.add(final_id)
                state["finalized_sessions"] = sorted(finalized)[-512:]
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
        if not isinstance(command, list) or not all(isinstance(x, str) and x for x in command):
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
            code = self.runner(command + ["--input", str(queue)], timeout=float(self.config.get("pinecone_timeout_seconds", 5)))
        except FileNotFoundError:
            pinecone.update(last_status="unavailable", write_pending=True)
            return
        except Exception:
            logger.exception("fleet-session-lifecycle: pinecone indexer failed; queued for a later turn")
            pinecone.update(last_status="failed", write_pending=True)
            return
        pinecone.update(last_status="ok" if code == 0 else "write_pending", write_pending=code != 0)

    def _gbrain_sync(self, cwd: str, state: dict[str, Any]) -> None:
        root = Path(cwd).resolve() if cwd else None
        if root is None or not (root / ".git").exists():
            return
        gbrain = state.setdefault("gbrain", {})
        if gbrain.get("write_pending"):
            return
        now = time.time()
        try:
            debounce = max(0.0, float(self.config.get("debounce_seconds", 30)))
        except (TypeError, ValueError):
            debounce = 30.0
        if now - float(gbrain.get("last_attempt_at") or 0) < debounce:
            gbrain["last_status"] = "debounced"
            return
        gbrain["last_attempt_at"] = now
        command = self.config.get("gbrain_command") or list(_DEFAULT_GBRAIN)
        if not isinstance(command, list) or not all(isinstance(x, str) and x for x in command):
            return
        try:
            code = self.runner(command, timeout=float(self.config.get("gbrain_timeout_seconds", 5)), cwd=str(root))
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
                processed = set(state.get("processed_turns") or ())
                if event_id in processed:
                    return
                event = {"session_id": redact_for_export(session_id), "turn_id": redact_for_export(turn_id),
                         "completed": bool(completed), "interrupted": bool(interrupted),
                         "user": redact_for_export(user_message), "response": redact_for_export(assistant_response)}
                self._append_checkpoint(event)
                processed.add(event_id)
                # Bound ledger protects profile state from unbounded growth while preserving recent retry dedup.
                state["processed_turns"] = sorted(processed)[-512:]
                state["updated_at"] = int(time.time())
                self._pinecone_index(event, state)
                self._gbrain_sync(cwd, state)
                _atomic_json(self.state_path, state)
        except Exception:
            # Hooks must never block/damage response delivery; the core already persisted the transcript first.
            logger.exception("fleet-session-lifecycle: per-turn checkpoint failed for %s",
                             redact_for_export(session_id))
            return
