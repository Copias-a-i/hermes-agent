"""Cross-process disk budget for native Windows installer journeys.

The Windows kernel owns named semaphore counts.  Consequently a killed pytest
worker releases its slot automatically; no on-disk owner record can strand the
suite after a crash.  The semaphore name is derived from the shared machine
root but is never included in diagnostics.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SLOTS_ENV = "HERMES_E2E_INSTALL_SLOTS"
TIMEOUT_ENV = "HERMES_E2E_INSTALL_SLOT_TIMEOUT"
DEFAULT_TIMEOUT_SECONDS = 30 * 60
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF


@dataclass(frozen=True)
class InstallSlot:
    """An acquired cross-process install budget slot."""

    slots: int
    waited_seconds: float

    @property
    def diagnostic(self) -> str:
        return f"install slot acquired (slots={self.slots}, waited={self.waited_seconds:.1f}s)"


def _positive_int(name: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"harness: {name} must be a positive integer") from exc
    if parsed < 1:
        raise RuntimeError(f"harness: {name} must be a positive integer")
    return parsed


def _slot_count() -> int | None:
    value = os.environ.get(SLOTS_ENV)
    return _positive_int(SLOTS_ENV, value) if value else None


def _timeout_seconds() -> float:
    value = os.environ.get(TIMEOUT_ENV)
    if not value:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(value)
    except ValueError as exc:
        raise RuntimeError(f"harness: {TIMEOUT_ENV} must be a positive number of seconds") from exc
    if timeout <= 0:
        raise RuntimeError(f"harness: {TIMEOUT_ENV} must be a positive number of seconds")
    return timeout


def _semaphore_name() -> str:
    root = os.environ.get("HERMES_E2E_MACHINE_ROOT") or os.getcwd()
    identity = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:24]
    # Local is visible to every process in this interactive GitHub Actions job;
    # unlike a file lock, Windows reclaims its count if a worker crashes.
    return rf"Local\hermes-e2e-install-{identity}"


@contextlib.contextmanager
def acquire_install_slot() -> Iterator[InstallSlot | None]:
    """Bound an install when configured; preserve the unbounded default otherwise."""

    slots = _slot_count()
    if slots is None:
        yield None
        return
    if os.name != "nt":
        raise RuntimeError(f"harness: {SLOTS_ENV} is supported only on Windows")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateSemaphoreW
    create.argtypes = (ctypes.c_void_p, ctypes.c_long, ctypes.c_long, ctypes.c_wchar_p)
    create.restype = ctypes.c_void_p
    wait = kernel32.WaitForSingleObject
    wait.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    wait.restype = ctypes.c_uint32
    release = kernel32.ReleaseSemaphore
    release.argtypes = (ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p)
    release.restype = ctypes.c_int
    close = kernel32.CloseHandle
    close.argtypes = (ctypes.c_void_p,)
    close.restype = ctypes.c_int

    handle = create(None, slots, slots, _semaphore_name())
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    acquired = False
    started = time.monotonic()
    timeout_seconds = _timeout_seconds()
    timeout_ms = min(int(timeout_seconds * 1000), 0xFFFFFFFE)
    try:
        result = wait(handle, timeout_ms)
        waited = time.monotonic() - started
        if result == _WAIT_TIMEOUT:
            raise TimeoutError(
                f"harness: install slot timed out after {waited:.1f}s "
                f"(slots={slots}, timeout={timeout_seconds:.1f}s)")
        if result == _WAIT_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
        if result != _WAIT_OBJECT_0:
            raise RuntimeError(f"harness: install slot wait returned unexpected status {result}")
        acquired = True
        yield InstallSlot(slots=slots, waited_seconds=waited)
    finally:
        if acquired and not release(handle, 1, None):
            raise ctypes.WinError(ctypes.get_last_error())
        close(handle)
