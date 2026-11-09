"""Atomic bounded daily journal for recoverable terminal output spills."""

from __future__ import annotations

import hashlib
import json
import os
import time
import tarfile
from datetime import UTC, datetime
from pathlib import Path

_MAX_AGE_S = 7 * 86400


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


def _record_id(payload: str, now_ns: int | None = None) -> str:
    now_ns = time.time_ns() if now_ns is None else now_ns
    digest = hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{now_ns:x}-{os.getpid():x}-{digest}"


def _append_atomic(path: Path, payload: bytes) -> None:
    """Append one complete JSONL record while excluding concurrent writers."""
    import fcntl

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


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
            archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not archive.exists():
                fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
                try:
                    os.write(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
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
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    """Buffers one spill then atomically upserts it into a single UTC daily shard."""

    def __init__(self, directory: Path, now: float | None = None):
        self.directory = directory
        self.now = time.time() if now is None else now
        self._chunks: list[str] = []
        self._chars = 0
        self._capped = False
        self._cap = 5_000_000

    def append(self, text: str) -> None:
        if self._capped or not text:
            return
        budget = self._cap - self._chars
        if budget <= 0 or len(text) > budget:
            self._chunks.append(text[:max(0, budget)])
            self._chunks.append("\n... [spill capped at 5,000,000 chars] ...\n")
            self._capped = True
        else:
            self._chunks.append(text)
        self._chars += len(text)

    def close(self) -> tuple[str, str] | None:
        if not self._chunks:
            return None
        output = "".join(self._chunks)
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
        self._chunks.clear()
        self._chars = 0
        return str(path), record_id
