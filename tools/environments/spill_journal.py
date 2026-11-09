"""Atomic bounded daily journal for recoverable terminal-output spills."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator

from tools.spill_safety import ensure_spill_dir, open_exclusive

_MAX_AGE_S = 7 * 86400
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


def _record_id(payload: str, now_ns: int | None = None) -> str:
    now_ns = time.time_ns() if now_ns is None else now_ns
    digest = hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{now_ns:x}-{os.getpid():x}-{digest}"


@contextmanager
def _locked_file(path: Path, flags: int, mode: int = 0o600) -> Iterator[int]:
    """Open a private regular file without following links and hold an advisory lock."""
    fd = os.open(path, flags | _O_NOFOLLOW, mode)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"journal path is not a regular file: {path}")
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            unlock = lambda: msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            unlock = lambda: fcntl.flock(fd, fcntl.LOCK_UN)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            yield fd
        finally:
            unlock()
    finally:
        os.close(fd)


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while (chunk := os.read(fd, 1024 * 1024)):
        chunks.append(chunk)
    return b"".join(chunks)


def _append_atomic(path: Path, payload: bytes) -> None:
    """Append one complete JSONL record while excluding concurrent writers."""
    ensure_spill_dir(path.parent, private=True)
    with _locked_file(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT) as fd:
        os.write(fd, payload)
        os.fsync(fd)


def redact_record(path: Path, record_id: str, redact: Callable[[str], str]) -> bool:
    """Redact exactly one journal record under the same lock used for appends."""
    try:
        with _locked_file(path, os.O_RDWR) as fd:
            records = [json.loads(line) for line in _read_fd(fd).decode("utf-8-sig", "replace").splitlines()]
            record = next((item for item in records if item.get("id") == record_id), None)
            if record is None:
                return False
            output = record.get("output")
            if not isinstance(output, str):
                return False
            redacted = redact(output)
            record["output"] = redacted
            record["sha256"] = hashlib.sha256(redacted.encode("utf-8", "replace")).hexdigest()
            payload = b"".join(
                (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                for item in records
            )
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)
            return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _prune_empty_or_expired_shards(directory: Path, now: float) -> list[str]:
    """Bound retention. A shard is removed only after its immutable archive exists."""
    cutoff = now - _MAX_AGE_S
    archived: list[str] = []
    for shard in directory.glob("spill-*.jsonl"):
        try:
            if shard.stat().st_mtime >= cutoff:
                continue
            data = shard.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            archive = directory / "archive" / f"{shard.stem}-{digest[:16]}.jsonl"
            ensure_spill_dir(archive.parent, private=True)
            if not archive.exists():
                with open_exclusive(archive, private=True) as handle:
                    handle.write(data.decode("utf-8", "replace"))
                os.chmod(archive, 0o400)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
                continue
            index = directory / "archive" / "index.jsonl"
            _append_atomic(index, (json.dumps({"source": shard.name, "archive": archive.name, "sha256": digest}, sort_keys=True) + "\n").encode())
            shard.unlink()
            archived.append(shard.name)
        except OSError:
            continue
    return archived


def migrate_legacy_spills(directory: Path) -> dict[str, object] | None:
    """Archive legacy one-file-per-run spills once, preserving a hashed bundle/index."""
    legacy = sorted(directory.glob("out-*.log"))
    if not legacy:
        return None
    archive_dir = directory / "archive"
    ensure_spill_dir(archive_dir, private=True)
    manifest = [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in legacy]
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    bundle = archive_dir / f"legacy-spills-{digest[:16]}.tar.gz"
    if not bundle.exists():
        staging = bundle.with_suffix(".tar.gz.tmp")
        with tarfile.open(staging, "w:gz") as tar:
            for path in legacy:
                tar.add(path, arcname=path.name, recursive=False)
        os.chmod(staging, 0o400)
        os.replace(staging, bundle)
    if not bundle.exists():
        return None
    index = archive_dir / "index.jsonl"
    record = {"kind": "legacy-terminal-spill-migration", "bundle": bundle.name, "files": manifest, "sha256": digest}
    _append_atomic(index, (json.dumps(record, sort_keys=True) + "\n").encode())
    for path in legacy:
        path.unlink()
    return record


class RollingSpillJournal:
    """Streams one spill to a private staging file, then atomically appends one daily record."""

    def __init__(self, directory: Path, now: float | None = None):
        self.directory = directory
        self.now = time.time() if now is None else now
        self._staging: Path | None = None
        self._handle = None
        self._chars = 0
        self._capped = False
        self._cap = 5_000_000

    def append(self, text: str) -> None:
        if self._capped or not text:
            return
        if self._handle is None:
            ensure_spill_dir(self.directory, private=True)
            self._staging = self.directory / f".spill-{_record_id('staging')}.tmp"
            self._handle = open_exclusive(self._staging, private=True, errors="replace")
        budget = self._cap - self._chars
        if budget <= 0 or len(text) > budget:
            self._handle.write(text[:max(0, budget)])
            self._handle.write("\n... [spill capped at 5,000,000 chars] ...\n")
            self._capped = True
        else:
            self._handle.write(text)
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._chars += len(text)

    def close(self) -> tuple[str, str] | None:
        if self._handle is None or self._staging is None:
            return None
        try:
            self._handle.close()
            output = self._staging.read_text(encoding="utf-8", errors="replace")
            migrate_legacy_spills(self.directory)
            record_id = _record_id(output)
            path = self.directory / f"spill-{_day(self.now)}.jsonl"
            record = {
                "id": record_id,
                "created_at": datetime.fromtimestamp(self.now, UTC).isoformat().replace("+00:00", "Z"),
                "output": output,
                "sha256": hashlib.sha256(output.encode("utf-8", "replace")).hexdigest(),
            }
            _append_atomic(path, (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            _prune_empty_or_expired_shards(self.directory, self.now)
            return str(path), record_id
        finally:
            try:
                self._staging.unlink()
            except OSError:
                pass
            self._handle = None
            self._staging = None
            self._chars = 0
