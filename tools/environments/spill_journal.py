"""Atomic bounded daily journal for recoverable, already-redacted terminal spills."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator

from tools.spill_safety import ensure_spill_dir, open_exclusive

_MAX_AGE_S = 7 * 86400
_CHUNK_CHARS = 1_500
_MAX_SHARD_BYTES = 10_000_000
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


def _record_id(payload: str, now_ns: int | None = None) -> str:
    now_ns = time.time_ns() if now_ns is None else now_ns
    digest = hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{now_ns:x}-{os.getpid():x}-{digest}"


@contextmanager
def _locked_file(path: Path, flags: int, mode: int = 0o600) -> Iterator[int]:
    """Open a private regular file without following links and hold a lock."""
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


@contextmanager
def _journal_lock(path: Path) -> Iterator[None]:
    """Lock a stable sidecar, so atomic replacement cannot strand writers."""
    ensure_spill_dir(path.parent, private=True)
    with _locked_file(path.with_suffix(path.suffix + ".lock"), os.O_RDWR | os.O_CREAT):
        yield


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(fd, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short journal write")
        view = view[written:]


def _atomic_replace(path: Path, payload: bytes) -> None:
    """Commit a whole shard only after durable temp bytes exist.

    Any write/fsync/rename failure leaves the prior shard name and bytes intact.
    """
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temporary)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        _write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _append_atomic(path: Path, payload: bytes) -> None:
    """Atomically append framed records without ever tearing a live shard."""
    with _journal_lock(path):
        try:
            with _locked_file(path, os.O_RDONLY) as fd:
                current = _read_fd(fd)
        except FileNotFoundError:
            current = b""
        _atomic_replace(path, current + payload)


def _framed_records(record_id: str, output: str, created_at: str) -> bytes:
    """Bound individual JSONL lines while retaining deterministic reconstruction."""
    chunks = [output[i:i + _CHUNK_CHARS] for i in range(0, len(output), _CHUNK_CHARS)] or [""]
    digest = hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()
    return b"".join(
        (json.dumps({
            "id": record_id, "created_at": created_at, "chunk": index,
            "chunks": len(chunks), "output": chunk,
            **({"sha256": digest} if index == len(chunks) - 1 else {}),
        }, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        for index, chunk in enumerate(chunks)
    )


def _shard_path(directory: Path, day: str, pending_bytes: int) -> Path:
    """Choose the newest bounded shard for a UTC day."""
    def ordinal(path: Path) -> int:
        suffix = path.stem.removeprefix(f"spill-{day}")
        return int(suffix[1:]) if suffix.startswith("-") and suffix[1:].isdigit() else 1

    candidates = sorted(directory.glob(f"spill-{day}*.jsonl"), key=ordinal)
    if candidates:
        latest = candidates[-1]
        try:
            if latest.stat().st_size + pending_bytes <= _MAX_SHARD_BYTES:
                return latest
        except OSError:
            pass
        suffix = ordinal(latest) + 1
    else:
        suffix = 1
    return directory / (f"spill-{day}.jsonl" if suffix == 1 else f"spill-{day}-{suffix:03d}.jsonl")


def _append_daily(directory: Path, day: str, payload: bytes) -> Path:
    """Reserve shard capacity and atomically commit under one day-wide lock."""
    with _journal_lock(directory / f".spill-{day}-rotation"):
        path = _shard_path(directory, day, len(payload))
        _append_atomic(path, payload)
        return path


def reconstruct_record(path: Path, record_id: str) -> str | None:
    """Read one framed record deterministically; reject incomplete/corrupt frames."""
    frames = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]
    selected = [frame for frame in frames if frame.get("id") == record_id]
    if not selected:
        return None
    count = selected[0].get("chunks")
    if not isinstance(count, int) or count < 1 or len(selected) != count:
        return None
    if sorted(frame.get("chunk") for frame in selected) != list(range(count)):
        return None
    output = "".join(frame["output"] for frame in sorted(selected, key=lambda frame: frame["chunk"]))
    digest = selected[-1].get("sha256")
    if not isinstance(digest, str) or hashlib.sha256(output.encode("utf-8", "replace")).hexdigest() != digest:
        return None
    return output


def redact_record(path: Path, record_id: str, redact: Callable[[str], str]) -> bool:
    """Atomically rewrite one record without risking another writer's frames.

    This is retained for remediation of old records. New records are redacted in
    memory before their first journal write.
    """
    try:
        with _journal_lock(path):
            with _locked_file(path, os.O_RDONLY) as fd:
                raw = _read_fd(fd)
            frames = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
            selected = [frame for frame in frames if frame.get("id") == record_id]
            if not selected:
                return False
            count = selected[0].get("chunks")
            if not isinstance(count, int) or len(selected) != count:
                return False
            output = "".join(frame["output"] for frame in sorted(selected, key=lambda frame: frame["chunk"]))
            redacted = redact(output)
            if not isinstance(redacted, str):
                return False
            kept = [frame for frame in frames if frame.get("id") != record_id]
            payload = b"".join((json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8") for frame in kept)
            payload += _framed_records(record_id, redacted, datetime.now(UTC).isoformat().replace("+00:00", "Z"))
            _atomic_replace(path, payload)
            return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _prune_empty_or_expired_shards(directory: Path, now: float) -> list[str]:
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
            _append_atomic(directory / "archive" / "index.jsonl", (json.dumps({"source": shard.name, "archive": archive.name, "sha256": digest}, sort_keys=True) + "\n").encode())
            shard.unlink()
            archived.append(shard.name)
        except OSError:
            continue
    return archived


def migrate_legacy_spills(directory: Path, redact: Callable[[str], str] = lambda value: value) -> dict[str, object] | None:
    """Archive only redacted legacy contents, serializing the one-time migration."""
    migration_lock = directory / ".legacy-spill-migration"
    with _journal_lock(migration_lock):
        legacy = sorted(directory.glob("out-*.log"))
        if not legacy:
            return None
        archive_dir = directory / "archive"
        ensure_spill_dir(archive_dir, private=True)
        # Read/redact completely before opening any archive staging path.
        from agent.redact import redact_terminal_output
        from tools.ansi_strip import strip_ansi
        contents = [
            (path, redact(redact_terminal_output(
                strip_ansi(path.read_text(encoding="utf-8-sig", errors="replace")), "env")))
            for path in legacy
        ]
        if not all(isinstance(content, str) for _, content in contents):
            raise TypeError("legacy spill redactor must return text")
        manifest = [{"name": path.name, "sha256": hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()} for path, content in contents]
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        bundle = archive_dir / f"legacy-spills-{digest[:16]}.tar.gz"
        if not bundle.exists():
            staging = bundle.with_suffix(f".tar.gz.{os.getpid()}.tmp")
            with tarfile.open(staging, "w:gz") as tar:
                for path, content in contents:
                    encoded = content.encode("utf-8", "replace")
                    info = tarfile.TarInfo(path.name)
                    info.size = len(encoded)
                    tar.addfile(info, io.BytesIO(encoded))
            os.chmod(staging, 0o400)
            os.replace(staging, bundle)
        if not bundle.exists():
            return None
        record = {"kind": "legacy-terminal-spill-migration", "bundle": bundle.name, "files": manifest, "sha256": digest}
        _append_atomic(archive_dir / "index.jsonl", (json.dumps(record, sort_keys=True) + "\n").encode())
        for path, _ in contents:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return record


class RollingSpillJournal:
    """Accumulate one bounded spill only in memory until redaction succeeds."""

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

    def close(self, redact: Callable[[str], str] = lambda value: value) -> tuple[str, str] | None:
        if not self._chunks:
            return None
        output = "".join(self._chunks)
        self._chunks = []
        # This must finish before any disk operation, including directory setup.
        redacted = redact(output)
        if not isinstance(redacted, str):
            raise TypeError("spill redactor must return text")
        record_id = _record_id(redacted)
        created_at = datetime.fromtimestamp(self.now, UTC).isoformat().replace("+00:00", "Z")
        payload = _framed_records(record_id, redacted, created_at)
        try:
            migrate_legacy_spills(self.directory, redact)
            path = _append_daily(self.directory, _day(self.now), payload)
            _prune_empty_or_expired_shards(self.directory, self.now)
            return str(path), record_id
        finally:
            self._chars = 0
