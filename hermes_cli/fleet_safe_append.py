"""Fleet-safe append-only Markdown updates.

This is intentionally a small stdlib-only boundary for profile hooks and cron
jobs that write agent-vault notes.  Atomic replacement alone prevents torn
files but not read/modify/write loss; every mutation therefore holds a stable
sidecar ``flock`` while it reads, deduplicates, writes, fsyncs and renames.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

_MARKER_PREFIX = "<!-- fleet-safe-append:"
_SECRET_DESTINATION_SUFFIXES = {
    ".env",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".token",
    ".secret",
}
_SECRET_VALUE = re.compile(
    r"(?im)(?:^|[\s'\"])(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+"
)
_DEDUPE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class SafeAppendError(ValueError):
    """The requested append is outside the safe Markdown-note contract."""


@dataclass(frozen=True)
class AppendResult:
    appended: bool
    path: Path
    dedupe_key: str


def _normalize_markdown(text: str) -> str:
    """Use LF and exactly one terminal newline without changing interior bytes."""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_target(root: str | Path, target: str | Path) -> tuple[Path, Path, tuple[int, int]]:
    root_path = Path(root)
    if not root_path.exists() or root_path.is_symlink() or not root_path.is_dir():
        raise SafeAppendError("vault root must be an existing non-symlink directory")
    real_root = root_path.resolve(strict=True)
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    # Resolve only the parent: the target can be newly created, but no parent
    # symlink may point the write out of the declared vault root.
    parent = candidate.parent.resolve(strict=False)
    normalized = parent / candidate.name
    if not _inside(real_root, parent) or not _inside(real_root, normalized):
        raise SafeAppendError("target escapes the declared vault root")
    if (
        candidate.suffix.lower() != ".md"
        or candidate.suffix.lower() in _SECRET_DESTINATION_SUFFIXES
    ):
        raise SafeAppendError("fleet-safe append accepts Markdown notes only")
    if candidate.exists() and (candidate.is_symlink() or not candidate.is_file()):
        raise SafeAppendError("target must be a regular non-symlink file")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_stat = parent.stat()
    return real_root, normalized, (parent_stat.st_dev, parent_stat.st_ino)


@contextmanager
def _target_parent(target: Path, expected_identity: tuple[int, int]) -> Iterator[int]:
    """Pin the verified directory before any target mutation.

    Every target-file operation below is relative to this descriptor.  A later
    pathname swap (including replacing the parent with a symlink) therefore
    cannot redirect the temp file or publication outside the verified parent.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    try:
        fd = os.open(target.parent, flags)
    except OSError as exc:
        raise SafeAppendError("target parent changed after validation") from exc
    try:
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) != expected_identity:
            raise SafeAppendError("target parent changed after validation")
        yield fd
    finally:
        os.close(fd)


def _read_target(parent_fd: int, name: str) -> tuple[str, os.stat_result | None]:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "", None
    if not stat.S_ISREG(metadata.st_mode):
        raise SafeAppendError("target must be a regular non-symlink file")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    with os.fdopen(fd, "r", encoding="utf-8-sig", newline="") as handle:
        return handle.read(), metadata


def _publish(parent_fd: int, name: str, text: str, mode: int, before_replace: Callable[[], None] | None) -> None:
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace:
            before_replace()
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _safe_lock_path(target: Path) -> Path:
    lock = target.with_name(f".{target.name}.fleet-safe.lock")
    if lock.exists() and (lock.is_symlink() or not lock.is_file()):
        raise SafeAppendError("lock path must be a regular non-symlink file")
    return lock


@contextmanager
def _locked(target: Path, timeout_seconds: float) -> Iterator[None]:
    """Serialize updates and recover stale *metadata* only after its owner dies.

    Kernel flock ownership is released on process crash, so a stale lock file is
    harmless.  Its PID metadata is retained solely for inspection; after taking
    the kernel lock we replace stale/dead-owner metadata, never a live owner.
    """
    try:
        import fcntl
    except ImportError as exc:
        raise SafeAppendError("fleet-safe append requires POSIX file locking") from exc
    lock = _safe_lock_path(target)
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for fleet-safe lock: {lock}")
                time.sleep(0.01)
        # Kernel flock ownership is authoritative.  Once acquired, any existing
        # metadata is from a prior holder; overwrite it without unlinking the
        # locked inode.
        payload = f"pid={os.getpid()}\nacquired_unix={time.time():.6f}\n".encode(
            "ascii"
        )
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def append_markdown(
    root: str | Path,
    target: str | Path,
    entry: str,
    *,
    dedupe_key: str,
    timeout_seconds: float = 10.0,
    before_replace: Callable[[], None] | None = None,
) -> AppendResult:
    """Append one idempotent Markdown entry with crash-safe publication.

    ``dedupe_key`` is materialized as an HTML comment, making retries safe even
    when a caller crashes after rename but before recording its own receipt.
    ``before_replace`` is a test seam; exceptions leave the original untouched.
    """
    _, safe_target, parent_identity = _validate_target(root, target)
    if not _DEDUPE_KEY.fullmatch(dedupe_key):
        raise SafeAppendError("dedupe key must be 1-160 safe identifier characters")
    if not isinstance(entry, str) or not entry.strip():
        raise SafeAppendError("entry must contain Markdown")
    if "\x00" in entry or _SECRET_VALUE.search(entry):
        raise SafeAppendError(
            "entry appears to contain a secret; refuse durable note write"
        )
    marker = f"{_MARKER_PREFIX}{dedupe_key} -->"
    with _locked(safe_target, timeout_seconds):
        with _target_parent(safe_target, parent_identity) as parent_fd:
            current, previous = _read_target(parent_fd, safe_target.name)
            if marker in current:
                return AppendResult(False, safe_target, dedupe_key)
            document = _normalize_markdown(current) if current else ""
            payload = _normalize_markdown(entry)
            next_text = _normalize_markdown(
                (document.rstrip("\n") + "\n\n" if document else "") + marker + "\n" + payload
            )
            _publish(
                parent_fd,
                safe_target.name,
                next_text,
                (previous.st_mode & 0o777) if previous else 0o600,
                before_replace,
            )
    return AppendResult(True, safe_target, dedupe_key)
