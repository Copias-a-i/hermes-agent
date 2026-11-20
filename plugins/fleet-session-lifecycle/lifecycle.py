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
import stat as stat_mode
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
_SESSION_HASH = re.compile(r"[0-9a-f]{64}")
_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}")
_MAX_TEXT = 600

logger = logging.getLogger(__name__)
_reported_lock_aliases: set[str] = set()


def redact_for_export(value: Any) -> str:
    """Use canonical egress redaction plus local summaries' credential-specific coverage."""
    text = redact_for_egress(str(value or "").replace("\x00", " "))
    for pattern in _EXPORT_SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:_MAX_TEXT]


def session_identity_hash(session_id: str) -> str:
    """Stable internal identity for a session, taken before any display redaction.

    Durable records may only carry the redacted display id, which is lossy: a
    credential-shaped session id redacts to a different (and colliding) string,
    so dedup, cancellation and finalization all key on this hash instead.
    """
    return hashlib.sha256(str(session_id).encode()).hexdigest()


def _is_outbox_record(line: bytes) -> bool:
    """Whether one JSONL line is a readable outbox record an adapter can consume."""
    try:
        return isinstance(json.loads(line.decode("utf-8")), dict)
    except (UnicodeDecodeError, ValueError):
        return False


class UnreadableLedger(RuntimeError):
    """The lifecycle ledger exists but cannot be read; it is preserved, never replaced."""


def _report_once(key: str, message: str, *args: Any) -> None:
    """Every hook builds a controller, so a standing problem is logged once per path."""
    if key not in _reported_lock_aliases:
        _reported_lock_aliases.add(key)
        logger.error(message, *args)


def _open_private_file(path: Path) -> int | None:
    """Open ``path`` read-write (created 0600), or None when something else stands at the path.

    The state directory is writable by candidate-executed tools, so a symlink or hard link can stand
    where a plugin file belongs. The path is opened without following links and only a private
    regular file whose descriptor matches the path is accepted, so an alias is refused, not followed.
    """
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        if os.path.islink(path):
            return None
        raise
    try:
        opened = os.fstat(fd)
        standing = os.lstat(path)
        owned = os.name == "nt" or opened.st_uid == os.geteuid()  # windows-footgun: ok — guarded by os.name
        if (
            stat_mode.S_ISREG(opened.st_mode) and opened.st_nlink == 1 and owned
            and (opened.st_dev, opened.st_ino) == (standing.st_dev, standing.st_ino)
        ):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 1 << 16):
        chunks.append(chunk)
    return b"".join(chunks)


def _write_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _append_private(path: Path, text: str) -> None:
    """Append ``text`` durably through a descriptor validated as this plugin's private file.

    Checkpoint partitions have predictable names in a tree candidate-executed tools can write, so an
    alias planted there is refused (the event stays pending and durable) instead of being followed.
    """
    fd = _open_private_file(path)
    if fd is None:
        _report_once("checkpoint:" + str(path),
                     "fleet-session-lifecycle: refusing %s: it is not this plugin's private checkpoint file "
                     "(symlink, hard link or foreign file); remove it so checkpoints can resume", path)
        raise PermissionError(f"refusing aliased checkpoint partition {path}")
    try:
        os.lseek(fd, 0, os.SEEK_END)
        _write_fd(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(path.parent)


def profile_name_for_home(home: Path) -> str:
    """Return a stable, profile-local vault namespace without ambient-home fallback."""
    if home.parent.name == "profiles":
        return home.name
    from hermes_constants import _get_platform_default_hermes_home
    if home.resolve() == _get_platform_default_hermes_home().resolve():
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
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(raw)
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    """Make a replace durable by syncing its parent directory when supported."""
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, os.O_RDONLY | directory_flag)
    except OSError:
        logger.warning("fleet-session-lifecycle: unable to open state directory for fsync: %s", path)
        return
    try:
        os.fsync(fd)
    except OSError:
        # Windows has no directory fd/fsync equivalent through Python's os module.
        if os.name != "nt":
            raise
    finally:
        os.close(fd)


_STATE_ARTIFACTS = (
    "fleet-session-lifecycle.json",
    "fleet-session-lifecycle.lock",
    "fleet-session-lifecycle-outbox.jsonl",
    "fleet-session-lifecycle-admissions",
    "fleet-session-lifecycle-completions",
    "fleet-session-lifecycle-cancellations",
    "fleet-session-lifecycle-finalizations",
)


class LifecycleController:
    """Profile-local durable side effects for one completed or interrupted turn."""

    _locks_guard = threading.Lock()
    _process_locks: dict[Path, threading.RLock] = {}

    @staticmethod
    def has_durable_state(home: Path) -> bool:
        """Whether this profile ever produced lifecycle state worth cleaning up.

        A profile that never enabled the plugin must not have ``state/`` or a
        lock file materialised for it by a disabled-session cleanup pass.
        """
        state = Path(home) / "state"
        return any((state / name).exists() for name in _STATE_ARTIFACTS)

    @staticmethod
    def _event_session_hash(event: dict[str, Any]) -> str:
        """Identity of a durable record; legacy rows carry only the redacted display id."""
        stored = str(event.get("session_hash") or "")
        if _SESSION_HASH.fullmatch(stored):
            return stored
        return session_identity_hash(str(event.get("session_id") or ""))

    @staticmethod
    def _session_identity_candidates(session_id: str) -> set[str]:
        """Every hash a record for ``session_id`` may carry, including the legacy redacted one."""
        if not session_id:
            return set()
        return {session_identity_hash(session_id), session_identity_hash(redact_for_export(session_id))}

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
        # Set when the lock path holds something that is not this plugin's lock file: no retry can change that.
        self._lease_refused = False

    def _state(self) -> dict[str, Any]:
        """The ledger, empty only when it does not exist yet.

        An existing ledger that cannot be read or parsed raises ``UnreadableLedger``: replacing it
        with an empty one would discard every pending turn and intent it records.
        """
        try:
            raw = self.state_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise UnreadableLedger(str(self.state_path)) from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise UnreadableLedger(str(self.state_path)) from exc
        if not isinstance(data, dict):
            raise UnreadableLedger(str(self.state_path))
        return data

    def _writable_state(self) -> dict[str, Any] | None:
        """The ledger to update under the lease, or None when it must be preserved untouched.

        Callers then take their lease-independent intent path, so the event stays durable and is
        adopted by the first holder that finds a readable ledger.
        """
        try:
            return self._state()
        except UnreadableLedger:
            _report_once(
                "ledger:" + str(self.state_path),
                "fleet-session-lifecycle: %s exists but is not a readable ledger; it is preserved and new "
                "events are kept as intents until it is repaired or removed",
                self.state_path,
            )
            return None

    def _admission_event(self, event_id: str, session_id: str, turn_id: str, user_message: Any) -> dict[str, Any]:
        """Durable row of one admitted turn; identical whether it lands in state or in an intent."""
        return {
            "event_id": event_id,
            "session_hash": session_identity_hash(session_id),
            "session_id": redact_for_export(session_id),
            "turn_id": redact_for_export(turn_id),
            "completed": False,
            "interrupted": True,
            "active": True,
            "owner_pid": os.getpid(),
            "owner_birth": self._owner_birth(),
            "user": redact_for_export(user_message),
            "response": "",
            "checkpoint_partition": self._checkpoint_path().name,
        }

    def _admission_intent_path(self, event_id: str) -> Path:
        return self.home / "state" / "fleet-session-lifecycle-admissions" / f"{event_id}.json"

    def _persist_admission_intent(self, event: dict[str, Any]) -> None:
        """Keep a lease-starved admission recoverable without touching another owner's state file."""
        event_id = str(event.get("event_id") or "")
        if event_id:
            _atomic_json(self._admission_intent_path(event_id), event)

    def _merge_admission_intents(
        self, state: dict[str, Any], *, cancelled_sessions: set[str] | None = None,
    ) -> list[Path]:
        """Adopt lease-starved admissions as active turns; the adopted file is always safe to clear.

        An admission carries no outcome, so it may never replace a row that has one: an event that is
        already processed, already pending, or owned by a cancelled session is dropped instead.
        """
        directory = self.home / "state" / "fleet-session-lifecycle-admissions"
        try:
            intents = tuple(directory.glob("*.json"))
        except OSError:
            return []
        processed = {str(item) for item in (state.get("processed_turns") or ())}
        pending = state.setdefault("pending_turns", {})
        adopted: list[Path] = []
        cancelled_sessions = cancelled_sessions or set()
        for path in intents:
            try:
                event = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("event_id") or "")
            if not event_id or path.name != f"{event_id}.json":
                continue
            adopted.append(path)
            if self._event_session_hash(event) in cancelled_sessions or event_id in processed:
                continue
            existing = pending.get(event_id)
            if isinstance(existing, dict):
                # A lease-starved reply record can land after its admission reached state: carry the
                # reply onto the still-unsettled row, never over a row a completion already settled.
                if existing.get("active") and not existing.get("response") and event.get("response"):
                    existing["response"] = event["response"]
                continue
            event["active"] = True
            pending[event_id] = event
        return adopted

    def _merge_event_intents(
        self, state: dict[str, Any], *, cancelled_sessions: set[str] | None = None,
    ) -> list[Path]:
        """Adopt every lease-starved per-event intent; admissions first so a completion can settle them."""
        return [
            *self._merge_admission_intents(state, cancelled_sessions=cancelled_sessions),
            *self._merge_completion_intents(state, cancelled_sessions=cancelled_sessions),
        ]

    def _completion_intent_path(self, event_id: str) -> Path:
        return self.home / "state" / "fleet-session-lifecycle-completions" / f"{event_id}.json"

    def _persist_completion_intent(self, event: dict[str, Any]) -> None:
        """Make a lease-starved completion replayable without mutating another owner's state file."""
        event_id = str(event.get("event_id") or "")
        if event_id:
            _atomic_json(self._completion_intent_path(event_id), event)

    def _merge_completion_intents(
        self, state: dict[str, Any], *, cancelled_sessions: set[str] | None = None,
    ) -> list[Path]:
        """Adopt completions unless a durable cancellation already owns their session."""
        directory = self.home / "state" / "fleet-session-lifecycle-completions"
        try:
            intents = tuple(directory.glob("*.json"))
        except OSError:
            return []
        pending = state.setdefault("pending_turns", {})
        adopted: list[Path] = []
        cancelled_sessions = cancelled_sessions or set()
        for path in intents:
            try:
                event = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("event_id") or "")
            if not event_id or path.name != f"{event_id}.json":
                continue
            # A cancellation is terminal: never let a lease-starved completion
            # re-admit its event after cancellation removed the durable pending row.
            if self._event_session_hash(event) in cancelled_sessions:
                adopted.append(path)
                continue
            event["active"] = False
            pending[event_id] = event
            adopted.append(path)
        return adopted

    @staticmethod
    def _clear_completion_intents(paths: list[Path]) -> None:
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _cancellation_intent_path(self, session_hash: str) -> Path:
        return self.home / "state" / "fleet-session-lifecycle-cancellations" / f"{session_hash}.json"

    def _persist_cancellation_intent(self, session_id: str) -> None:
        """Durably defer disabled-session cleanup while another process owns state."""
        session_hash = session_identity_hash(session_id)
        _atomic_json(self._cancellation_intent_path(session_hash), {
            "session_hash": session_hash,
            "session_id": redact_for_export(session_id),
        })

    def _merge_cancellation_intents(self, state: dict[str, Any]) -> tuple[list[Path], list[str], set[str]]:
        """Apply deferred cancellation only while holding the lifecycle lease."""
        directory = self.home / "state" / "fleet-session-lifecycle-cancellations"
        try:
            paths = tuple(directory.glob("*.json"))
        except OSError:
            return [], [], set()
        pending = state.setdefault("pending_turns", {})
        adopted: list[Path] = []
        removed: list[str] = []
        cancelled_sessions: set[str] = set()
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            session_hash = str(payload.get("session_hash") or "")
            if not _SESSION_HASH.fullmatch(session_hash) or path.name != f"{session_hash}.json":
                continue
            # Legacy rows key on the redacted display id, which the raw-id hash cannot reproduce.
            identities = {session_hash, session_identity_hash(str(payload.get("session_id") or ""))}
            cancelled_sessions |= identities
            for event_id, event in list(pending.items()):
                if isinstance(event, dict) and self._event_session_hash(event) in identities:
                    pending.pop(event_id, None)
                    removed.append(str(event_id))
            adopted.append(path)
        return adopted, removed, cancelled_sessions

    def _clear_cancellation_intents(self, paths: list[Path], removed: list[str]) -> None:
        self._clear_completion_intents([self._completion_intent_path(event_id) for event_id in removed])
        self._clear_completion_intents(paths)

    def _finalization_intent_path(self, session_hash: str) -> Path:
        return self.home / "state" / "fleet-session-lifecycle-finalizations" / f"{session_hash}.json"

    def _persist_finalization_intent(self, session_id: str, reason: str) -> None:
        """Keep the boundary replayable; repeated busy attempts collapse onto one intent."""
        session_hash = session_identity_hash(session_id)
        _atomic_json(self._finalization_intent_path(session_hash), {
            "session_hash": session_hash,
            "session_id": redact_for_export(session_id),
            "reason": redact_for_export(reason),
            "partition": self._checkpoint_path().name,
            "attempt_id": uuid.uuid4().hex,
        })

    def _merge_finalization_intents(self, state: dict[str, Any]) -> list[Path]:
        """Adopt lease-starved finalization boundaries before any further side effect."""
        directory = self.home / "state" / "fleet-session-lifecycle-finalizations"
        try:
            paths = tuple(directory.glob("*.json"))
        except OSError:
            return []
        intents = state.setdefault("finalization_intents", {})
        adopted: list[Path] = []
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            session_hash = str(payload.get("session_hash") or "")
            display_id = str(payload.get("session_id") or "")
            partition = str(payload.get("partition") or "")
            attempt_id = str(payload.get("attempt_id") or "")
            if (
                not display_id
                or not _SESSION_HASH.fullmatch(session_hash)
                or path.name != f"{session_hash}.json"
                or not _PARTITION_NAME.fullmatch(partition)
                or not _ATTEMPT_ID.fullmatch(attempt_id)
            ):
                continue
            adopted.append(path)
            if isinstance(intents.get(session_hash), dict):
                # An in-state intent may already own a committed marker; never re-identify it.
                continue
            intents[session_hash] = {
                "session_hash": session_hash,
                "session_id": display_id,
                "reason": redact_for_export(payload.get("reason")),
                "partition": partition,
                "attempt_id": attempt_id,
            }
        return adopted

    def _finalization_attempt(self, intents: dict[str, Any], session_hash: str) -> tuple[str, str]:
        """Attempt identity and partition for one boundary; a resumed intent keeps both.

        Its append may already be on disk, and the committed marker only answers
        for the partition that append went to, so the pair travels together.
        """
        resumed = intents.get(session_hash)
        if isinstance(resumed, dict) and _ATTEMPT_ID.fullmatch(str(resumed.get("attempt_id") or "")):
            partition = str(resumed.get("partition") or "")
            if _PARTITION_NAME.fullmatch(partition):
                return str(resumed["attempt_id"]), partition
        return uuid.uuid4().hex, self._checkpoint_path().name

    def _replay_finalization_intents(self, state: dict[str, Any], processed: list[str]) -> None:
        """Commit every fully-described deferred finalization while the holder owns the lease."""
        intents = state.setdefault("finalization_intents", {})
        finalized = [str(item) for item in (state.get("finalized_sessions") or ())]
        for session_hash, payload in list(intents.items()):
            if not isinstance(payload, dict):
                continue  # Legacy in-state intent is resumed by its original full_wrapup caller.
            display_id = str(payload.get("session_id") or "")
            partition = str(payload.get("partition") or "")
            if not display_id or not _PARTITION_NAME.fullmatch(partition):
                continue
            replayed = self._drain_pending(state, processed, active_hashes={session_hash})
            if session_hash in replayed:
                finalized = [item for item in finalized if item != session_hash]
            if session_hash not in finalized:
                # The attempt is identified independently of the session so a resumed session can
                # finalize again, while an append whose ledger write was lost is never duplicated.
                attempt_id = str(payload.get("attempt_id") or session_hash)
                if not self._full_wrapup_has(attempt_id, partition):
                    self._append_full_wrapup(display_id, str(payload.get("reason") or ""), attempt_id, partition)
                finalized.append(session_hash)
            intents.pop(session_hash, None)
        state["finalized_sessions"] = finalized[-512:]

    @staticmethod
    def _run(argv: list[str], **kwargs: Any) -> int:
        return subprocess.run(argv, check=False, stdin=kwargs.get("stdin", subprocess.DEVNULL), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=kwargs.get("timeout", 5),
                              env=kwargs.get("env")).returncode

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
    def _owner_is_alive(pid: int, birth: float | None = None) -> bool:
        """Identify an owner by PID plus process birth time to reject PID reuse."""
        if not psutil.pid_exists(pid):
            return False
        if birth is None:
            return True  # Legacy records predate the birth-time ownership token.
        try:
            return abs(psutil.Process(pid).create_time() - birth) < 0.001
        except (psutil.Error, OSError):
            return False

    @staticmethod
    def _owner_birth() -> float | None:
        try:
            return psutil.Process(os.getpid()).create_time()
        except (psutil.Error, OSError):
            return None

    def _reclaim_stale_windows_lease(self) -> bool:
        """Reclaim only an aged lock whose recorded owner is no longer alive.

        Validating the lock and then deleting it by its shared pathname cannot tell the stale file that
        was validated from a newer lock a rival reclaimer created there in between.  The lock is instead
        claimed by renaming it to a private name, one atomic step that takes whichever file stands at the
        path, and only the claimed file's own contents decide whether it was the stale lock.
        """
        try:
            stale_seconds = max(0.0, float(self.config.get("lock_stale_seconds", 300)))
            if time.time() - self.lock_path.stat().st_mtime < stale_seconds:
                return False
            owner = self.lock_path.read_text(encoding="ascii")
            pid_text, separator, remainder = owner.partition(":")
            birth_text, has_birth, token = remainder.partition(":")
            try:
                birth = float(birth_text) if has_birth and birth_text else None
            except ValueError:
                return False
            token = token if has_birth else remainder
            if not separator or not token or self._owner_is_alive(int(pid_text), birth):
                return False
        except (OSError, ValueError):
            return False
        claim = self.lock_path.with_name(f"{self.lock_path.name}.claim-{uuid.uuid4().hex}")
        try:
            os.rename(self.lock_path, claim)
        except OSError:
            return False  # Already taken by a rival, or still open under a live owner on Windows.
        try:
            claimed = claim.read_text(encoding="ascii")
        except (OSError, ValueError):
            claimed = None
        if claimed == owner:
            try:
                os.unlink(claim)
            except OSError:
                pass
            return True
        self._return_claimed_lock(claim)
        return False

    def _return_claimed_lock(self, claim: Path) -> None:
        """Put back a claimed file that was not the stale lock, never over a lock acquired since.

        Hard-linking back fails if the path is occupied again, so a newer lock always wins.  A live Windows
        owner keeps its handle open, which blocks the claim itself; this only repairs the instant between
        an owner closing its lock and removing it.
        """
        try:
            os.link(claim, self.lock_path)
        except FileExistsError:
            pass
        except OSError:
            # No hard links on this volume: rename back only into a free path.
            if not self.lock_path.exists():
                try:
                    os.rename(claim, self.lock_path)
                    return
                except OSError:
                    logger.warning("fleet-session-lifecycle: could not return a claimed lock to %s", self.lock_path)
        try:
            os.unlink(claim)
        except OSError:
            pass

    def _open_posix_lock(self) -> int | None:
        """Open the profile lock file, or None when the lock path holds something else.

        The state directory is writable by candidate-executed tools, so a symlink or a hard link can stand
        where the lock file belongs; following it would truncate and then write its target.  The path is
        opened without following links and only a private regular file is accepted, so an alias is
        refused rather than followed (the caller then takes its lease-independent intent path).
        """
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError:
            if not os.path.islink(self.lock_path):
                raise
            fd = None
        if fd is not None:
            try:
                opened = os.fstat(fd)
                standing = os.lstat(self.lock_path)
                if (
                    stat_mode.S_ISREG(opened.st_mode) and opened.st_nlink == 1
                    and opened.st_uid == os.geteuid()  # windows-footgun: ok — only reached under the fcntl (POSIX) lease branch
                    and (opened.st_dev, opened.st_ino) == (standing.st_dev, standing.st_ino)
                ):
                    return fd
            except OSError:
                pass
            os.close(fd)
        self._lease_refused = True
        if str(self.lock_path) not in _reported_lock_aliases:  # every hook builds a controller: report once
            _reported_lock_aliases.add(str(self.lock_path))
            logger.error(
                "fleet-session-lifecycle: refusing %s: it is not this plugin's private lock file (symlink, hard "
                "link or foreign file); remove it so lifecycle checkpoints can take their lease again",
                self.lock_path,
            )
        return None

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
            birth = self._owner_birth()
            token = f"{os.getpid()}:{birth if birth is not None else ''}:{uuid.uuid4().hex}"
            fd: int | None = None
            windows_lease = fcntl is None
            locked = False
            try:
                if fcntl is not None:
                    fd = self._open_posix_lock()
                    if fd is None:
                        yield False
                        return
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
        self._lease_refused = False
        while True:
            with self._lease() as acquired:
                if acquired:
                    yield True
                    return
            # A refused lock path cannot free up by waiting; do not stall the turn on it.
            if self._lease_refused or time.monotonic() >= deadline:
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
        _append_private(path, line)

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
                state = self._writable_state() if acquired else None
                if state is None:
                    # Another process may hold the lease for seconds (an outbox adapter runs under it). The
                    # admission must not live only in this process, so it gets its own per-event intent and
                    # the next lease holder adopts it; a crash before ``after_turn`` then still replays it.
                    self._persist_admission_intent(self._admission_event(event_id, session_id, turn_id, user_message))
                    logger.warning("fleet-session-lifecycle: turn admission lease or ledger unavailable; intent retained")
                    return
                processed = {str(item) for item in (state.get("processed_turns") or ())}
                pending = state.setdefault("pending_turns", {})
                if event_id not in processed and not isinstance(pending.get(event_id), dict):
                    pending[event_id] = self._admission_event(event_id, session_id, turn_id, user_message)
                    _atomic_json(self.state_path, state)
        except Exception:
            logger.exception("fleet-session-lifecycle: turn admission failed for %s", redact_for_export(session_id))

    def record_response(self, *, session_id: str, turn_id: str, user_message: Any, assistant_response: Any) -> None:
        """Make a finished turn's reply durable on its admission before the turn settles.

        The core persists the reply and fires ``post_llm_call`` well before ``on_session_end`` brings the
        turn's outcome. Without this the reply lives only in process memory across that window, and a
        crash there replays the admission with an empty response. The row stays active and keeps its
        owner: this records content, never an outcome, so a live owner's turn is not replayed early.
        """
        response = redact_for_export(assistant_response)
        if not session_id or not turn_id or not response:
            return
        event_id = hashlib.sha256(f"{session_id}\0{turn_id}".encode()).hexdigest()
        try:
            with self._lease_with_retry() as acquired:
                state = self._writable_state() if acquired else None
                if state is None:
                    event = self._admission_event(event_id, session_id, turn_id, user_message)
                    event["response"] = response
                    self._persist_admission_intent(event)
                    logger.warning("fleet-session-lifecycle: reply record lease or ledger unavailable; intent retained")
                    return
                if event_id in {str(item) for item in (state.get("processed_turns") or ())}:
                    return
                pending = state.setdefault("pending_turns", {})
                event = pending.get(event_id)
                if not isinstance(event, dict):
                    event = pending[event_id] = self._admission_event(event_id, session_id, turn_id, user_message)
                elif not event.get("active"):
                    return  # already settled by a completion that carries its own reply
                event["response"] = response
                event["user"] = event.get("user") or redact_for_export(user_message)
                _atomic_json(self.state_path, state)
        except Exception:
            logger.exception("fleet-session-lifecycle: reply record failed for %s", redact_for_export(session_id))

    def _append_full_wrapup(self, session_id: str, reason: str, attempt_id: str, partition: str) -> None:
        """Write one identifiable final checkpoint without creating an agent turn."""
        path = self._checkpoint_path(partition)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = "\n".join((
            "\n<!-- hermes-lifecycle full-wrapup checkpoint -->",
            f"- attempt_id: {attempt_id}",
            f"- session_id: {redact_for_export(session_id)}",
            f"- reason: {redact_for_export(reason)}",
            "- transcript_checkpointed: true",
            "- delivery_message: none",
            f"<!-- hermes-lifecycle full-wrapup committed: {attempt_id} -->",
        )) + "\n"
        _append_private(path, line)

    def _full_wrapup_has(self, attempt_id: str, partition: str) -> bool:
        try:
            return f"<!-- hermes-lifecycle full-wrapup committed: {attempt_id} -->" in self._checkpoint_path(partition).read_text(encoding="utf-8-sig")
        except OSError:
            return False

    def _pending_is_replayable(self, event: dict[str, Any], active_hashes: set[str]) -> bool:
        """Only replay completed, explicitly-finalizing, or demonstrably orphaned admissions."""
        if bool(event.get("completed")) or not bool(event.get("active")):
            return True
        if self._event_session_hash(event) in active_hashes:
            return True
        try:
            return not self._owner_is_alive(
                int(event.get("owner_pid")),
                float(event["owner_birth"]) if event.get("owner_birth") is not None else None,
            )
        except (TypeError, ValueError):
            return True  # legacy pending record: no liveness claim to honor.

    def _drain_pending(self, state: dict[str, Any], processed: list[str], *, active_hashes: set[str] | None = None) -> set[str]:
        """Replay eligible durable events, returning the session identities they belong to."""
        replayed_sessions: set[str] = set()
        pending = state.setdefault("pending_turns", {})
        for pending_id, pending_event in list(pending.items()):
            if not isinstance(pending_event, dict):
                pending.pop(pending_id, None)
                continue
            if not self._pending_is_replayable(pending_event, active_hashes or set()):
                continue
            if pending_id not in processed:
                # Make completion and planned delivery recoverable before any filesystem or adapter effect.
                pending_event["outbox_intent"] = True
                _atomic_json(self.state_path, state)
                partition = str(pending_event.get("checkpoint_partition") or "") or None
                if not self._checkpoint_has(pending_id, partition, recovery=partition is None):
                    self._append_checkpoint(pending_event)
                # Append and fsync the durable adapter queue before recording the
                # event as processed, so a crash cannot permanently skip delivery.
                self._outbox_index(pending_event, state)
                processed.append(pending_id)
                _atomic_json(self.state_path, state)
                replayed_sessions.add(self._event_session_hash(pending_event))
            pending.pop(pending_id, None)
        return replayed_sessions

    def full_wrapup(self, *, session_id: str, reason: str = "") -> None:
        """Persist the final `/wrapup` boundary with a durable pre-append intent."""
        if not session_id:
            return
        try:
            with self._lease_with_retry() as acquired:
                state = self._writable_state() if acquired else None
                if state is None:
                    self._persist_finalization_intent(session_id, reason)
                    logger.warning("fleet-session-lifecycle: final wrap-up lease or ledger unavailable; intent retained")
                    return
                cancellation_intents, cancelled_events, cancelled_sessions = self._merge_cancellation_intents(state)
                completion_intents = self._merge_event_intents(state, cancelled_sessions=cancelled_sessions)
                finalization_intents = self._merge_finalization_intents(state)
                if cancellation_intents or completion_intents or finalization_intents:
                    _atomic_json(self.state_path, state)
                    self._clear_cancellation_intents(cancellation_intents, cancelled_events)
                    self._clear_completion_intents(completion_intents)
                    self._clear_completion_intents(finalization_intents)
                state["vault_profile"] = self.profile
                processed = [str(item) for item in (state.get("processed_turns") or ())]
                intents = state.setdefault("finalization_intents", {})
                session_hash = session_identity_hash(session_id)
                attempt_id, partition = self._finalization_attempt(intents, session_hash)
                intents[session_hash] = {
                    "session_hash": session_hash,
                    "session_id": redact_for_export(session_id),
                    "reason": redact_for_export(reason),
                    "partition": partition,
                    "attempt_id": attempt_id,
                }
                _atomic_json(self.state_path, state)
                self._replay_finalization_intents(state, processed)
                state["processed_turns"] = processed[-512:]
                state["updated_at"] = int(time.time())
                _atomic_json(self.state_path, state)
        except Exception:
            logger.exception("fleet-session-lifecycle: full wrap-up checkpoint failed for this session")
            return

    def cancel_session(self, *, session_id: str) -> None:
        """Remove durable admissions when a profile is disabled at session end."""
        if not session_id:
            return
        try:
            with self._lease_with_retry() as acquired:
                state = self._writable_state() if acquired else None
                if state is None:
                    self._persist_cancellation_intent(session_id)
                    logger.warning("fleet-session-lifecycle: disabled session cancellation lease or ledger unavailable; "
                                   "intent retained")
                    return
                cancellation_intents, cancelled_events, cancelled_sessions = self._merge_cancellation_intents(state)
                identities = self._session_identity_candidates(session_id)
                cancelled_sessions |= identities
                completion_intents = self._merge_event_intents(state, cancelled_sessions=cancelled_sessions)
                pending = state.setdefault("pending_turns", {})
                removed = [
                    event_id for event_id, event in pending.items()
                    if isinstance(event, dict) and self._event_session_hash(event) in identities
                ]
                for event_id in removed:
                    pending.pop(event_id, None)
                if cancellation_intents or removed or completion_intents:
                    _atomic_json(self.state_path, state)
                    self._clear_cancellation_intents(cancellation_intents, [*cancelled_events, *removed])
                    self._clear_completion_intents(completion_intents)
                else:
                    self._clear_cancellation_intents(cancellation_intents, cancelled_events)
        except Exception:
            logger.exception("fleet-session-lifecycle: disabled session cancellation failed")

    @staticmethod
    def _repair_outbox_tail(fd: int) -> None:
        """Drop an unreadable trailing record so the next append cannot fuse onto it.

        A crash between the queue write and its fsync can leave the file ending
        mid-record.  Appending after that produces one unparseable line, which
        blocks delivery of every record in the file — including the ones that
        were already durable — so the torn tail is repaired before enqueueing.
        """
        raw = _read_fd(fd)
        if not raw:
            return
        records = raw.split(b"\n")
        trailing = records.pop()  # b"" when the queue already ends on a record terminator
        if trailing and _is_outbox_record(trailing):
            records.append(trailing)  # complete record whose terminator was lost
        while records and not _is_outbox_record(records[-1]):
            records.pop()
        repaired = b"".join(line + b"\n" for line in records)
        if repaired == raw:
            return
        os.lseek(fd, 0, os.SEEK_SET)
        _write_fd(fd, repaired)
        os.ftruncate(fd, len(repaired))
        os.fsync(fd)
        logger.warning("fleet-session-lifecycle: repaired a torn outbox tail (%d byte(s) dropped)",
                       len(raw) - len(repaired))

    def _deliver_outbox(self, command: list[str], queue: Path, fd: int, outbox: dict[str, Any]) -> int | None:
        """Hand the adapter the validated queue itself, never a pathname that could be swapped.

        On POSIX the open descriptor is the adapter's stdin and ``--input`` names ``/dev/stdin``, so a
        link planted at the queue path after validation cannot redirect what the adapter reads. On
        Windows the held descriptor (opened without delete sharing) keeps the path from being replaced
        while the adapter reads it. Returns the adapter's exit code, or None after recording a failure.
        """
        os.lseek(fd, 0, os.SEEK_SET)
        source = str(queue) if os.name == "nt" else "/dev/stdin"
        try:
            return self._invoke(command + ["--input", source], stdin=fd,
                                timeout=float(self.config.get("outbox_timeout_seconds", 5)))
        except FileNotFoundError:
            outbox.update(last_status="unavailable", write_pending=True)
        except Exception:
            logger.exception("fleet-session-lifecycle: external outbox adapter failed; retained locally")
            outbox.update(last_status="failed", write_pending=True)
        return None

    def _outbox_index(self, event: dict[str, Any], state: dict[str, Any]) -> None:
        """Deliver redacted events through an optional vendor-neutral durable outbox.

        Deployments install an adapter outside this repository and configure
        ``outbox_command``. Missing or failed adapters leave the locally durable
        record available for a later turn without affecting response delivery.
        The queue is only read, repaired and appended through a descriptor validated
        as this plugin's private file, so a planted alias is never written through.
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
        fd = _open_private_file(queue)
        if fd is None:
            _report_once("outbox:" + str(queue),
                         "fleet-session-lifecycle: refusing %s: it is not this plugin's private outbox file "
                         "(symlink, hard link or foreign file); remove it so outbox delivery can resume", queue)
            state.setdefault("outbox", {}).update(last_status="refused", write_pending=True)
            return
        try:
            self._repair_outbox_tail(fd)
            os.lseek(fd, 0, os.SEEK_END)
            _write_fd(fd, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
            code = self._deliver_outbox(command, queue, fd, state.setdefault("outbox", {}))
        finally:
            os.close(fd)
        outbox = state["outbox"]
        if code is None:
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
                state = self._writable_state() if acquired else None
                if state is None:
                    # The admission is already durable. Publish an independent per-event completion
                    # intent so a later holder can safely make it replayable instead of waiting for
                    # this still-live PID to die (or for an unreadable ledger to be repaired).
                    state = (self._writable_state() if not acquired else None) or {}
                    pending = state.get("pending_turns") if isinstance(state.get("pending_turns"), dict) else {}
                    event = dict(pending.get(event_id) or {})
                    event.update({
                        "event_id": event_id,
                        "session_hash": session_identity_hash(session_id),
                        "session_id": redact_for_export(session_id),
                        "turn_id": redact_for_export(turn_id),
                        "completed": bool(completed),
                        "interrupted": bool(interrupted),
                        "active": False,
                        "user": redact_for_export(user_message) or event.get("user", ""),
                        "response": redact_for_export(assistant_response),
                        "checkpoint_partition": event.get("checkpoint_partition") or self._checkpoint_path().name,
                    })
                    self._persist_completion_intent(event)
                    logger.warning("fleet-session-lifecycle: after-turn lease or ledger unavailable; completion intent "
                                   "retained")
                    return
                cancellation_intents, cancelled_events, cancelled_sessions = self._merge_cancellation_intents(state)
                completion_intents = self._merge_event_intents(state, cancelled_sessions=cancelled_sessions)
                finalization_intents = self._merge_finalization_intents(state)
                if cancellation_intents or completion_intents or finalization_intents:
                    _atomic_json(self.state_path, state)
                    self._clear_cancellation_intents(cancellation_intents, cancelled_events)
                    self._clear_completion_intents(completion_intents)
                    self._clear_completion_intents(finalization_intents)
                state["vault_profile"] = self.profile
                processed = [str(item) for item in (state.get("processed_turns") or ())]
                self._replay_finalization_intents(state, processed)
                current_was_processed = event_id in processed
                pending = state.setdefault("pending_turns", {})
                event = pending.get(event_id)
                if not current_was_processed and not isinstance(event, dict):
                    event = {"event_id": event_id, "session_hash": session_identity_hash(session_id),
                             "session_id": redact_for_export(session_id),
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
                state["finalized_sessions"] = [
                    item for item in (state.get("finalized_sessions") or ()) if str(item) not in replayed_sessions
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
