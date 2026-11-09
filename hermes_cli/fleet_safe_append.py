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
import tempfile
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


def _validate_target(root: str | Path, target: str | Path) -> tuple[Path, Path]:
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
    return real_root, normalized


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
    _, safe_target = _validate_target(root, target)
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
        current = ""
        if safe_target.exists():
            current = safe_target.read_text(encoding="utf-8-sig")
        if marker in current:
            return AppendResult(False, safe_target, dedupe_key)
        document = _normalize_markdown(current) if current else ""
        payload = _normalize_markdown(entry)
        next_text = (
            (document.rstrip("\n") + "\n\n" if document else "")
            + marker
            + "\n"
            + payload
        )
        next_text = _normalize_markdown(next_text)
        safe_target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = (safe_target.stat().st_mode & 0o777) if safe_target.exists() else 0o600
        fd, temporary = tempfile.mkstemp(
            prefix=f".{safe_target.name}.", dir=safe_target.parent
        )
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(next_text)
                handle.flush()
                os.fsync(handle.fileno())
            if before_replace:
                before_replace()
            os.replace(temporary, safe_target)
            directory_fd = os.open(
                safe_target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return AppendResult(True, safe_target, dedupe_key)
