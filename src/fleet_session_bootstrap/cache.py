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
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".new-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _manifest_digest(manifest: dict[str, object]) -> str:
    return sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
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
                return data if isinstance(data, dict) else None
            valid = (
                isinstance(data, dict)
                and type(data.get("schema")) is int
                and data.get("schema") == 1
                and type(data.get("created_at")) is int
                and data.get("partition_identity") == partition_identity
                and data.get("manifest_digest") == _manifest_digest(manifest)
                and data.get("source_version") == source_version
                and isinstance(data.get("payload"), dict)
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
        if partition_identity is None or manifest is None or source_version is None:
            payload = value
        else:
            if not _SAFE_KEY.fullmatch(partition_identity):
                raise ValueError("invalid partition identity")
            payload = {"schema": 1, "partition_identity": partition_identity, "manifest_digest": _manifest_digest(manifest), "source_version": source_version, "created_at": int(time()) if now is None else now, "payload": value}
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
        safe_receipt = {key: receipt[key] for key in sorted(_RECEIPT_FIELDS & receipt.keys())}
        safe_receipt.update({"schema": 1, "attempt_digest": attempt_digest})
        path = self.path_for(attempt_digest)
        with self.lock_for(attempt_digest):
            _atomic_json(path, safe_receipt)
        return path
