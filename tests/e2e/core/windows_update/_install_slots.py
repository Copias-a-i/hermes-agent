"""Cross-process disk budget for native Windows installer journeys.

Each budget slot is an ownership-aware named mutex. Windows marks a mutex
abandoned when its owning process dies, so the next waiter may claim that
specific slot without stale filesystem state. Names are derived from the
machine root and profile scope but never included in diagnostics.
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
_WAIT_ABANDONED_0 = 0x80
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_POLL_MILLISECONDS = 100


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


def _mutex_names(slots: int) -> list[str]:
    root = os.environ.get("HERMES_E2E_MACHINE_ROOT") or os.getcwd()
    profile = os.environ.get("HERMES_PROFILE", "default")
    identity = hashlib.sha256(
        f"{Path(root).resolve()}\0{profile}".encode("utf-8")
    ).hexdigest()[:24]
    # Local covers every process in this interactive Actions job. Every
    # process opens every slot before it waits, preserving the named objects
    # across a crash while another holder or waiter remains alive.
    return [rf"Local\hermes-e2e-install-{identity}-{index}" for index in range(slots)]


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
    create = kernel32.CreateMutexW
    create.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    create.restype = ctypes.c_void_p
    wait = kernel32.WaitForMultipleObjects
    wait.argtypes = (ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_uint32)
    wait.restype = ctypes.c_uint32
    release = kernel32.ReleaseMutex
    release.argtypes = (ctypes.c_void_p,)
    release.restype = ctypes.c_int
    close = kernel32.CloseHandle
    close.argtypes = (ctypes.c_void_p,)
    close.restype = ctypes.c_int

    handles: list[int] = []
    for name in _mutex_names(slots):
        handle = create(None, False, name)
        if not handle:
            error = ctypes.get_last_error()
            for opened in handles:
                close(opened)
            raise ctypes.WinError(error)
        handles.append(handle)

    owned_handle: int | None = None
    started = time.monotonic()
    timeout_seconds = _timeout_seconds()
    try:
        array = (ctypes.c_void_p * len(handles))(*handles)
        while owned_handle is None:
            elapsed = time.monotonic() - started
            remaining_ms = int((timeout_seconds - elapsed) * 1000)
            if remaining_ms <= 0:
                raise TimeoutError(
                    f"harness: install slot timed out after {elapsed:.1f}s "
                    f"(slots={slots}, timeout={timeout_seconds:.1f}s)")
            result = wait(len(handles), array, False, min(_POLL_MILLISECONDS, remaining_ms))
            if _WAIT_OBJECT_0 <= result < _WAIT_OBJECT_0 + len(handles):
                owned_handle = handles[result - _WAIT_OBJECT_0]
            elif _WAIT_ABANDONED_0 <= result < _WAIT_ABANDONED_0 + len(handles):
                owned_handle = handles[result - _WAIT_ABANDONED_0]
            elif result == _WAIT_TIMEOUT:
                continue
            elif result == _WAIT_FAILED:
                raise ctypes.WinError(ctypes.get_last_error())
            else:
                raise RuntimeError("harness: install slot wait returned unexpected status")
        waited = time.monotonic() - started
        yield InstallSlot(slots=slots, waited_seconds=waited)
    finally:
        try:
            if owned_handle is not None and not release(owned_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            for handle in handles:
                close(handle)
