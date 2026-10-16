"""Crash-safe per-turn maintenance for the bundled fleet lifecycle plugin.

The plugin deliberately owns only derived checkpoint metadata.  The vault path,
credentials and raw conversation history are never handed to the optional
external outbox adapter, and an adapter failure is recorded for a later turn
instead of raising into Hermes' delivery path.
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

import psutil

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses the ownership-token fallback.
    fcntl = None

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from agent.redact import redact_for_egress

_EXPORT_SECRET_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic|token)\s+[^\s,;]+"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|pk)[_-][A-Za-z0-9_-]{16,}\b"),
)
_PARTITION_NAME = re.compile(r"\d{4}-\d{2}-\d{2}-hermes-lifecycle\.md")
_MAX_TEXT = 600

logger = logging.getLogger(__name__)


def redact_for_export(value: Any) -> str:
    """Use canonical egress redaction plus local summaries' credential-specific coverage."""
    text = redact_for_egress(str(value or "").replace("\x00", " "))
    for pattern in _EXPORT_SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:_MAX_TEXT]


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
                              timeout=kwargs.get("timeout", 5), env=kwargs.get("env")).returncode

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

    @staticmethod
    def _owner_is_alive(pid: int) -> bool:
        return psutil.pid_exists(pid)

    def _reclaim_stale_windows_lease(self) -> bool:
        """Reclaim only an aged lock whose recorded owner is no longer alive."""
        try:
            stale_seconds = max(0.0, float(self.config.get("lock_stale_seconds", 300)))
            stat = self.lock_path.stat()
            if time.time() - stat.st_mtime < stale_seconds:
                return False
            owner = self.lock_path.read_text(encoding="ascii")
            pid_text, separator, token = owner.partition(":")
            if not separator or not token or self._owner_is_alive(int(pid_text)):
                return False
            if self.lock_path.read_text(encoding="ascii") != owner:
                return False
            self.lock_path.unlink()
            return True
        except (OSError, ValueError):
            return False

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
            token = f"{os.getpid()}:{uuid.uuid4().hex}"
            fd: int | None = None
            windows_lease = fcntl is None
            locked = False
            try:
                if fcntl is not None:
                    fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        yield False
                        return
                    locked = True
                else:  # pragma: no cover - Windows fallback retains exclusive-create semantics.
                    try:
                        fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        if not self._reclaim_stale_windows_lease():
                            yield False
                            return
                        try:
                            fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        except FileExistsError:
                            yield False
                            return
                os.ftruncate(fd, 0)
                os.write(fd, token.encode("ascii"))
                os.fsync(fd)
                yield True
            finally:
                if fd is not None:
                    if locked and fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                if windows_lease:
                    try:
                        if self.lock_path.read_text(encoding="ascii") == token:
                            self.lock_path.unlink()
                    except FileNotFoundError:
                        pass

    @contextmanager
    def _lease_with_retry(self) -> Iterator[bool]:
        """Acquire the profile lease within the configured bounded contention window."""
        deadline = time.monotonic() + max(0.0, float(self.config.get("finalize_lease_wait_seconds", 2)))
        while True:
            with self._lease() as acquired:
                if acquired:
                    yield True
                    return
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(0.05)

    def _checkpoint_path(self, partition: str | None = None) -> Path:
        name = partition if partition and _PARTITION_NAME.fullmatch(partition) else f"{time.strftime('%Y-%m-%d')}-hermes-lifecycle.md"
        return self.vault_root / self.profile / "daily" / name

    def _append_checkpoint(self, event: dict[str, Any]) -> None:
        path = self._checkpoint_path(str(event.get("checkpoint_partition") or "") or None)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = "\n".join((
            "\n<!-- hermes-lifecycle incremental checkpoint -->",
            f"- event_id: {event['event_id']}",
            f"- session_id: {event['session_id']}", f"- turn_id: {event['turn_id']}",
            f"- completed: {str(event['completed']).lower()}", f"- interrupted: {str(event['interrupted']).lower()}",
            f"- user: {event['user']}", f"- response: {event['response']}",
            f"<!-- hermes-lifecycle checkpoint committed: {event['event_id']} -->",
        )) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _checkpoint_has(self, event_id: str, partition: str | None = None, *, recovery: bool = False) -> bool:
        """Check one recorded partition; scan history only to recover legacy pending state.

        New admissions persist the partition with their intent, keeping the hot
        path to one bounded read. A pre-existing record without that field is a
        legacy recovery case, so scan the profile's daily partitions once before
        risking a duplicate checkpoint.
        """
        daily = self.vault_root / self.profile / "daily"
        if partition and _PARTITION_NAME.fullmatch(partition):
            paths = (daily / partition,)
        elif recovery:
            try:
                paths = tuple(path for path in daily.iterdir() if _PARTITION_NAME.fullmatch(path.name))
            except OSError:
                paths = ()
        else:
            paths = (self._checkpoint_path(),)
        for path in paths:
            try:
                contents = path.read_text(encoding="utf-8-sig")
                if (
                    f"- event_id: {event_id}" in contents
                    and f"<!-- hermes-lifecycle checkpoint committed: {event_id} -->" in contents
                ):
                    return True
            except OSError:
                continue
        return False

    def admit_turn(self, *, session_id: str, turn_id: str, user_message: Any) -> None:
        """Durably record a turn intent before model execution can be interrupted."""
        if not session_id or not turn_id:
            return
        event_id = hashlib.sha256(f"{session_id}\0{turn_id}".encode()).hexdigest()
        try:
            with self._lease_with_retry() as acquired:
                if not acquired:
                    logger.warning("fleet-session-lifecycle: turn admission lease remained busy")
                    return
                state = self._state()
                processed = {str(item) for item in (state.get("processed_turns") or ())}
                pending = state.setdefault("pending_turns", {})
                if event_id not in processed and not isinstance(pending.get(event_id), dict):
                    pending[event_id] = {
                        "event_id": event_id,
                        "session_id": redact_for_export(session_id),
                        "turn_id": redact_for_export(turn_id),
                        "completed": False,
                        "interrupted": True,
                        "active": True,
                        "owner_pid": os.getpid(),
                        "user": redact_for_export(user_message),
                        "response": "",
                        "checkpoint_partition": self._checkpoint_path().name,
                    }
                    _atomic_json(self.state_path, state)
        except Exception:
            logger.exception("fleet-session-lifecycle: turn admission failed for %s", redact_for_export(session_id))

    def _append_full_wrapup(self, session_id: str, reason: str, final_id: str, partition: str) -> None:
        """Write one identifiable final checkpoint without creating an agent turn."""
        path = self._checkpoint_path(partition)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = "\n".join((
            "\n<!-- hermes-lifecycle full-wrapup checkpoint -->",
            f"- final_id: {final_id}",
            f"- session_id: {redact_for_export(session_id)}",
            f"- reason: {redact_for_export(reason)}",
            "- transcript_checkpointed: true",
            "- delivery_message: none",
            f"<!-- hermes-lifecycle full-wrapup committed: {final_id} -->",
        )) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _full_wrapup_has(self, final_id: str, partition: str) -> bool:
        try:
            return f"<!-- hermes-lifecycle full-wrapup committed: {final_id} -->" in self._checkpoint_path(partition).read_text(encoding="utf-8-sig")
        except OSError:
            return False

    def _pending_is_replayable(self, event: dict[str, Any], active_session_id: str | None) -> bool:
        """Only replay completed, explicitly-finalizing, or demonstrably orphaned admissions."""
        if bool(event.get("completed")) or not bool(event.get("active")):
            return True
        if str(event.get("session_id") or "") == str(active_session_id or ""):
            return True
        try:
            return not self._owner_is_alive(int(event.get("owner_pid")))
        except (TypeError, ValueError):
            return True  # legacy pending record: no liveness claim to honor.

    def _drain_pending(self, state: dict[str, Any], processed: list[str], *, active_session_id: str | None = None) -> set[str]:
        """Replay eligible durable events before removing their intent or closing a session."""
        replayed_sessions: set[str] = set()
        pending = state.setdefault("pending_turns", {})
        for pending_id, pending_event in list(pending.items()):
            if not isinstance(pending_event, dict):
                pending.pop(pending_id, None)
                continue
            if not self._pending_is_replayable(pending_event, active_session_id):
                continue
            if pending_id not in processed:
                # Make completion and planned delivery recoverable before any filesystem or adapter effect.
                pending_event["outbox_intent"] = True
                _atomic_json(self.state_path, state)
                partition = str(pending_event.get("checkpoint_partition") or "") or None
                if not self._checkpoint_has(pending_id, partition, recovery=partition is None):
                    self._append_checkpoint(pending_event)
                processed.append(pending_id)
                _atomic_json(self.state_path, state)
                self._outbox_index(pending_event, state)
                replayed_sessions.add(str(pending_event.get("session_id") or ""))
            pending.pop(pending_id, None)
        return replayed_sessions

    def full_wrapup(self, *, session_id: str, reason: str = "") -> None:
        """Persist the final `/wrapup` boundary with a durable pre-append intent."""
        if not session_id:
            return
        try:
            with self._lease_with_retry() as acquired:
                if not acquired:
                    logger.warning("fleet-session-lifecycle: final wrap-up lease remained busy")
                    return
                state = self._state()
                state["vault_profile"] = self.profile
                processed = [str(item) for item in (state.get("processed_turns") or ())]
                replayed_sessions = self._drain_pending(state, processed, active_session_id=session_id)
                finalized = [str(item) for item in (state.get("finalized_sessions") or ())]
                cleared = {hashlib.sha256(item.encode()).hexdigest() for item in replayed_sessions if item}
                finalized = [item for item in finalized if item not in cleared]
                final_id = hashlib.sha256(session_id.encode()).hexdigest()
                intents = state.setdefault("finalization_intents", {})
                resuming_finalization = final_id in intents
                partition = str(intents.get(final_id) or self._checkpoint_path().name)
                if not _PARTITION_NAME.fullmatch(partition):
                    partition = self._checkpoint_path().name
                intents[final_id] = partition
                _atomic_json(self.state_path, state)
                if final_id not in finalized:
                    if not resuming_finalization or not self._full_wrapup_has(final_id, partition):
                        self._append_full_wrapup(session_id, reason, final_id, partition)
                    finalized.append(final_id)
                intents.pop(final_id, None)
                state["finalized_sessions"] = finalized[-512:]
                state["processed_turns"] = processed[-512:]
                state["updated_at"] = int(time.time())
                _atomic_json(self.state_path, state)
        except Exception:
            logger.exception("fleet-session-lifecycle: full wrap-up checkpoint failed for this session")
            return

    def _outbox_index(self, event: dict[str, Any], state: dict[str, Any]) -> None:
        """Deliver redacted events through an optional vendor-neutral durable outbox.

        Deployments install an adapter outside this repository and configure
        ``outbox_command``. Missing or failed adapters leave the locally durable
        record available for a later turn without affecting response delivery.
        """
        command = self.config.get("outbox_command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(x, str) and x for x in command)
        ):
            return
        queue = self.home / "state" / "fleet-session-lifecycle-outbox.jsonl"
        queue.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": "fleet-lifecycle-outbox.v1", "event_id": event["event_id"],
                   "profile": self.profile, "session_id": event["session_id"],
                   "turn_id": event["turn_id"],
                   "summary": f"user: {event['user']}\\nresponse: {event['response']}"}
        with queue.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        outbox = state.setdefault("outbox", {})
        try:
            code = self._invoke(command + ["--input", str(queue)], timeout=float(self.config.get("outbox_timeout_seconds", 5)))
        except FileNotFoundError:
            outbox.update(last_status="unavailable", write_pending=True)
            return
        except Exception:
            logger.exception("fleet-session-lifecycle: external outbox adapter failed; retained locally")
            outbox.update(last_status="failed", write_pending=True)
            return
        if code == 0:
            try:
                queue.unlink()
            except FileNotFoundError:
                pass
            outbox.update(last_status="ok", write_pending=False)
        else:
            outbox.update(last_status="write_pending", write_pending=True)

    def after_turn(self, *, session_id: str, turn_id: str, completed: bool, interrupted: bool,
                   user_message: Any, assistant_response: Any, conversation_history: list[Any] | None = None,
                   cwd: str = "") -> None:
        """Durably checkpoint exactly once per turn; never propagate a lifecycle failure."""
        if not session_id or not turn_id:
            return
        event_id = hashlib.sha256(f"{session_id}\0{turn_id}".encode()).hexdigest()
        try:
            with self._lease_with_retry() as acquired:
                if not acquired:
                    logger.warning("fleet-session-lifecycle: after-turn lease remained busy")
                    return
                state = self._state()
                state["vault_profile"] = self.profile
                processed = [str(item) for item in (state.get("processed_turns") or ())]
                current_was_processed = event_id in processed
                pending = state.setdefault("pending_turns", {})
                event = pending.get(event_id)
                if not current_was_processed and not isinstance(event, dict):
                    event = {"event_id": event_id, "session_id": redact_for_export(session_id),
                             "turn_id": redact_for_export(turn_id), "completed": bool(completed),
                             "interrupted": bool(interrupted), "active": False,
                             "user": redact_for_export(user_message), "response": redact_for_export(assistant_response),
                             "checkpoint_partition": self._checkpoint_path().name}
                    pending[event_id] = event
                    # Intent is durable before any side effect; a crash can now be recovered.
                    _atomic_json(self.state_path, state)
                elif not current_was_processed:
                    event.update(completed=bool(completed), interrupted=bool(interrupted), active=False,
                                 user=redact_for_export(user_message) or event.get("user", ""),
                                 response=redact_for_export(assistant_response))
                    # An admitted event becomes complete before any checkpoint/outbox side effect.
                    _atomic_json(self.state_path, state)
                replayed_sessions = self._drain_pending(state, processed)
                # Any recovered event is new activity for its own session, not just this callback.
                cleared = {hashlib.sha256(item.encode()).hexdigest() for item in replayed_sessions if item}
                state["finalized_sessions"] = [
                    item for item in (state.get("finalized_sessions") or ()) if str(item) not in cleared
                ]
                # Bound ledger preserves turn arrival order so retry dedup retains the newest ids.
                state["processed_turns"] = processed[-512:]
                state["updated_at"] = int(time.time())
                _atomic_json(self.state_path, state)
        except Exception:
            # Hooks must never block/damage response delivery; the core already persisted the transcript first.
            logger.exception("fleet-session-lifecycle: per-turn checkpoint failed for %s",
                             redact_for_export(session_id))
            return
