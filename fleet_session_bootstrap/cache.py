"""Private, trusted-state cache and receipt files for session bootstrap."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from time import time
from typing import Any

_SAFE_KEY = re.compile(r"[a-f0-9]{64}\Z")
_RECEIPT_FIELDS = frozenset({"cache_state", "manifest_version", "availability", "count", "elapsed_ms", "timestamp", "error", "namespace_digest"})
_AVAILABILITY = frozenset({"available", "unavailable", "timeout"})
_CACHE_STATE = frozenset({"hit", "miss", "unavailable"})
_ERROR = frozenset({"none", "invalid", "io", "timeout"})
_POINTER = re.compile(r"(?:SOUL|personality|environment):[a-z0-9._-]{1,64}\Z")
_SOURCE_VERSION = re.compile(r"v[0-9]{1,8}\Z")
_MAX_CACHE_BYTES = 4096


def _require_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} is invalid")
    return value


def _validate_cache_payload(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"availability", "count", "items"}:
        raise ValueError("cache payload schema is invalid")
    if value["availability"] not in _AVAILABILITY or type(value["availability"]) is not str:
        raise ValueError("cache availability is invalid")
    _require_int(value["count"], "cache count", 0, 128)
    items = value["items"]
    if not isinstance(items, list) or len(items) > 128 or any(type(item) is not str or not _POINTER.fullmatch(item) for item in items):
        raise ValueError("cache pointers are invalid")
    payload = {"availability": value["availability"], "count": value["count"], "items": list(items)}
    if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > _MAX_CACHE_BYTES:
        raise ValueError("cache payload is too large")
    return payload


def _validate_receipt(receipt: object) -> dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ValueError("receipt schema is invalid")
    if receipt["cache_state"] not in _CACHE_STATE or type(receipt["cache_state"]) is not str:
        raise ValueError("receipt cache state is invalid")
    if receipt["manifest_version"] != "v1" or type(receipt["manifest_version"]) is not str:
        raise ValueError("receipt manifest version is invalid")
    if receipt["availability"] not in _AVAILABILITY or type(receipt["availability"]) is not str:
        raise ValueError("receipt availability is invalid")
    if receipt["error"] not in _ERROR or type(receipt["error"]) is not str:
        raise ValueError("receipt error is invalid")
    if type(receipt["namespace_digest"]) is not str or not _SAFE_KEY.fullmatch(receipt["namespace_digest"]):
        raise ValueError("receipt namespace digest is invalid")
    _require_int(receipt["count"], "receipt count", 0, 128)
    _require_int(receipt["elapsed_ms"], "receipt elapsed time", 0, 60_000)
    _require_int(receipt["timestamp"], "receipt timestamp", 0, 4_102_444_800)
    return dict(receipt)


def _existing_components(path: Path) -> tuple[Path, ...]:
    absolute = Path(os.path.abspath(path))
    parts: list[Path] = []
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if not current.exists() and not current.is_symlink():
            break
        parts.append(current)
    return tuple(parts)


def _is_in_git_tree(path: Path) -> bool:
    if (path / ".git").exists() or (path / ".git").is_symlink():
        return True
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=5,
    )
    return completed.returncode == 0


def _trusted_state_root(path: Path) -> Path:
    """Admit only a direct, non-Git directory; never follow lexical symlinks."""
    if not path.is_absolute():
        raise ValueError("trusted state root must be absolute")
    for component in _existing_components(path):
        if component.is_symlink():
            raise ValueError("trusted state root contains a symlink")
        if _is_in_git_tree(component):
            raise ValueError("trusted state root is inside a Git worktree or repository")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or any(component.is_symlink() for component in _existing_components(path)):
        raise ValueError("trusted state root contains a symlink")
    resolved = path.resolve(strict=True)
    if resolved != path or _is_in_git_tree(resolved):
        raise ValueError("trusted state root is inside a Git worktree or repository")
    os.chmod(resolved, 0o700)
    return resolved


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=False, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("private state directory is invalid")
    os.chmod(path, 0o700)
    return path


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return  # Windows cannot open directory descriptors; os.replace remains atomic.
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _best_effort_private_fd(descriptor: int, path: Path) -> None:
    try:
        os.fchmod(descriptor, 0o600)
    except (AttributeError, OSError):
        # Windows has no fchmod; chmod is best-effort ACL-independent privacy.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".new-", dir=path.parent)
    try:
        _best_effort_private_fd(descriptor, Path(temporary))
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _manifest_digest(manifest: dict[str, object]) -> str:
    return sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _is_valid_cache_payload(value: object) -> bool:
    try:
        _validate_cache_payload(value)
    except ValueError:
        return False
    return True


class _StateStore:
    def __init__(self, state_root: Path, name: str) -> None:
        root = _trusted_state_root(state_root)
        self.root = _private_directory(_private_directory(root / "fleet-session-bootstrap") / name)

    def path_for(self, key: str, suffix: str = ".json") -> Path:
        if not _SAFE_KEY.fullmatch(key):
            raise ValueError("invalid state key")
        return self.root / f"{key}{suffix}"

    @contextmanager
    def lock_for(self, key: str):
        lock_path = self.path_for(key, ".lock")
        if lock_path.is_symlink():
            raise ValueError("state lock is a symlink")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        locked = False
        try:
            if lock_path.is_symlink() or not os.path.isfile(lock_path):
                raise ValueError("state lock is invalid")
            _best_effort_private_fd(descriptor, lock_path)
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            elif os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            locked = True
            yield
        finally:
            if locked and os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif locked and os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            os.close(descriptor)


class CacheStore(_StateStore):
    """Shared cache keyed by digests, with locked freshness validation."""

    def __init__(self, state_root: Path) -> None:
        super().__init__(state_root, "cache")

    def lock_path_for(self, key: str) -> Path:
        return self.path_for(key, ".lock")

    def get(self, key: str, *, partition_identity: str | None = None, manifest: dict[str, object] | None = None, source_version: str | None = None, now: int | None = None, ttl_seconds: int = 300) -> dict[str, Any] | None:
        path = self.path_for(key)
        with self.lock_for(key):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                return None
            if partition_identity is None or manifest is None or source_version is None:
                try:
                    return _validate_cache_payload(data)
                except ValueError:
                    return None
            if (not _SAFE_KEY.fullmatch(partition_identity) or not _SOURCE_VERSION.fullmatch(source_version)
                    or type(ttl_seconds) is not int or not 0 <= ttl_seconds <= 86_400
                    or now is not None and type(now) is not int):
                return None
            valid = (
                isinstance(data, dict)
                and type(data.get("schema")) is int
                and data.get("schema") == 1
                and type(data.get("created_at")) is int
                and data.get("partition_identity") == partition_identity
                and data.get("manifest_digest") == _manifest_digest(manifest)
                and data.get("source_version") == source_version
                and isinstance(data.get("payload"), dict)
                and _is_valid_cache_payload(data.get("payload"))
                and 0 <= (int(time()) if now is None else now) - data["created_at"] <= ttl_seconds
            )
            if valid:
                return data["payload"]
            try:
                path.unlink()
                _fsync_directory(path.parent)
            except FileNotFoundError:
                pass
            return None

    def put(self, key: str, value: dict[str, Any], *, partition_identity: str | None = None, manifest: dict[str, object] | None = None, source_version: str | None = None, now: int | None = None) -> Path:
        path = self.path_for(key)
        safe_value = _validate_cache_payload(value)
        if partition_identity is None or manifest is None or source_version is None:
            payload = safe_value
        else:
            if not _SAFE_KEY.fullmatch(partition_identity) or not _SOURCE_VERSION.fullmatch(source_version):
                raise ValueError("invalid cache metadata")
            _require_int(int(time()) if now is None else now, "cache timestamp", 0, 4_102_444_800)
            payload = {"schema": 1, "partition_identity": partition_identity, "manifest_digest": _manifest_digest(manifest), "source_version": source_version, "created_at": int(time()) if now is None else now, "payload": safe_value}
        with self.lock_for(key):
            _atomic_json(path, payload)
        return path


class ReceiptStore(_StateStore):
    """Atomic per-session receipts containing only bounded, non-PII metadata."""

    def __init__(self, state_root: Path) -> None:
        super().__init__(state_root, "receipts")

    @staticmethod
    def attempt_digest(session_id: str) -> str:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            raise ValueError("attempt identity is required")
        return sha256(b"COP-752/session-attempt/v1\0" + session_id.encode("utf-8")).hexdigest()

    def write(self, session_id: str, receipt: dict[str, Any]) -> Path:
        attempt_digest = self.attempt_digest(session_id)
        safe_receipt = _validate_receipt(receipt)
        safe_receipt.update({"schema": 1, "attempt_digest": attempt_digest})
        path = self.path_for(attempt_digest)
        with self.lock_for(attempt_digest):
            _atomic_json(path, safe_receipt)
        return path
