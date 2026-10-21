"""Private atomic state files for the session-bootstrap core."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any

_SAFE_KEY = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
_RECEIPT_FIELDS = frozenset({"availability", "namespace_digest", "elapsed_ms", "count"})


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


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
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CacheStore:
    """Cache keyed only by a safe shared partition identity, never session ID."""

    def __init__(self, root: Path) -> None:
        self.root = _private_directory(root)

    def path_for(self, key: str) -> Path:
        if not _SAFE_KEY.fullmatch(key):
            raise ValueError("invalid cache key")
        return self.root / f"{key}.json"

    def lock_path_for(self, key: str) -> Path:
        if not _SAFE_KEY.fullmatch(key):
            raise ValueError("invalid cache key")
        return self.root / f"{key}.lock"

    @contextmanager
    def lock_for(self, key: str):
        """Serialize one shared cache entry without exposing a world-readable lock."""
        lock_path = self.lock_path_for(key)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.path_for(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def put(self, key: str, value: dict[str, Any]) -> Path:
        path = self.path_for(key)
        with self.lock_for(key):
            _atomic_json(path, value)
        return path


class ReceiptStore:
    """Write redacted attempt receipts; opaque session IDs are never persisted."""

    def __init__(self, root: Path) -> None:
        self.root = _private_directory(root)

    def write(self, attempt_id: str, receipt: dict[str, Any]) -> Path:
        if not attempt_id:
            raise ValueError("attempt identity is required")
        safe_receipt = {key: receipt[key] for key in sorted(_RECEIPT_FIELDS & receipt.keys())}
        path = self.root / f"{sha256(attempt_id.encode()).hexdigest()}.json"
        _atomic_json(path, safe_receipt)
        return path
