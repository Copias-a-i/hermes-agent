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


def _validate_target(root: str | Path, target: str | Path) -> tuple[Path, Path, tuple[str, ...]]:
    root_path = Path(os.path.abspath(root))
    if not root_path.exists() or root_path.is_symlink() or not root_path.is_dir():
        raise SafeAppendError("vault root must be an existing non-symlink directory")
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    normalized = Path(os.path.abspath(candidate))
    try:
        relative = normalized.relative_to(root_path)
    except ValueError:
        raise SafeAppendError("target escapes the declared vault root")
    if (
        candidate.suffix.lower() != ".md"
        or candidate.suffix.lower() in _SECRET_DESTINATION_SUFFIXES
    ):
        raise SafeAppendError("fleet-safe append accepts Markdown notes only")
    if normalized.exists() and (normalized.is_symlink() or not normalized.is_file()):
        raise SafeAppendError("target must be a regular non-symlink file")
    return root_path, normalized, relative.parent.parts


@contextmanager
def _root_directory(root: Path) -> Iterator[int]:
    """Open an absolute vault root without following any ancestor symlink."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    try:
        fd = os.open(root.anchor, flags)
        for component in root.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except OSError as exc:
        raise SafeAppendError("vault root must be an existing non-symlink directory") from exc
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _target_parent(root_fd: int, components: tuple[str, ...]) -> Iterator[int]:
    """Traverse and pin every parent component without following symlinks.

    Every component is opened relative to the preceding descriptor with
    ``O_NOFOLLOW``.  Creation uses ``mkdirat`` semantics, so no absolute or
    post-validation pathname is ever followed during the write.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    fd = root_fd
    try:
        for component in components:
            if component in ("", ".", ".."):
                raise SafeAppendError("target escapes the declared vault root")
            try:
                next_fd = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=fd)
                next_fd = os.open(component, flags, dir_fd=fd)
            except OSError as exc:
                raise SafeAppendError("target parent changed after validation") from exc
            if fd != root_fd:
                os.close(fd)
            fd = next_fd
        yield fd
    finally:
        if fd != root_fd:
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


def _lock_name(target_name: str) -> str:
    return f".{target_name}.fleet-safe.lock"


@contextmanager
def _locked(parent_fd: int, target_name: str, timeout_seconds: float) -> Iterator[None]:
    """Serialize updates using a lock opened beneath a pinned parent directory.

    Kernel flock ownership is released on process crash, so a stale lock file is
    harmless. Its PID metadata is retained solely for inspection. The lock name
    is opened through ``parent_fd`` with ``O_NOFOLLOW``: no lock-file creation or
    opening can follow an attacker-controlled absolute target path.
    """
    try:
        import fcntl
    except ImportError as exc:
        raise SafeAppendError("fleet-safe append requires POSIX file locking") from exc
    lock_name = _lock_name(target_name)
    try:
        metadata = os.stat(lock_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SafeAppendError("lock path must be a regular unlinked file")
    except FileNotFoundError:
        pass
    try:
        fd = os.open(
            lock_name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise SafeAppendError("lock path must be a regular non-symlink file") from exc
    try:
        opened_metadata = os.fstat(fd)
        if not stat.S_ISREG(opened_metadata.st_mode) or opened_metadata.st_nlink != 1:
            raise SafeAppendError("lock path must be a regular unlinked file")
        deadline = time.monotonic() + timeout_seconds
    except Exception:
        os.close(fd)
        raise
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for fleet-safe lock: {lock_name}"
                    )
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
    safe_root, safe_target, parent_components = _validate_target(root, target)
    if not _DEDUPE_KEY.fullmatch(dedupe_key):
        raise SafeAppendError("dedupe key must be 1-160 safe identifier characters")
    if not isinstance(entry, str) or not entry.strip():
        raise SafeAppendError("entry must contain Markdown")
    if "\x00" in entry or _SECRET_VALUE.search(entry):
        raise SafeAppendError(
            "entry appears to contain a secret; refuse durable note write"
        )
    marker = f"{_MARKER_PREFIX}{dedupe_key} -->"
    with _root_directory(safe_root) as root_fd:
        with _target_parent(root_fd, parent_components) as parent_fd:
            with _locked(parent_fd, safe_target.name, timeout_seconds):
                current, previous = _read_target(parent_fd, safe_target.name)
                if marker in current:
                    return AppendResult(False, safe_target, dedupe_key)
                document = _normalize_markdown(current) if current else ""
                payload = _normalize_markdown(entry)
                next_text = _normalize_markdown(
                    (document.rstrip("\n") + "\n\n" if document else "")
                    + marker
                    + "\n"
                    + payload
                )
                _publish(
                    parent_fd,
                    safe_target.name,
                    next_text,
                    (previous.st_mode & 0o777) if previous else 0o600,
                    before_replace,
                )
    return AppendResult(True, safe_target, dedupe_key)
