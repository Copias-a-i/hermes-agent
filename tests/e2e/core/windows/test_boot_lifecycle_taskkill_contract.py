"""Portable contracts for the native-Windows boot-lifecycle taskkill seam."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.e2e.core.windows import _helpers


class _Gone(Exception):
    pass


def _fake_psutil(alive: dict[str, bool]) -> SimpleNamespace:
    class Process:
        def __init__(self, pid: int) -> None:
            if not alive["value"]:
                raise _Gone(pid)
            self.pid = pid

        def create_time(self) -> float:
            return 42.0

    return SimpleNamespace(Process=Process, NoSuchProcess=_Gone)


def test_taskkill_exit_255_is_accepted_only_after_the_original_pid_is_gone(monkeypatch) -> None:
    """Windows may report 255 after its own tree traversal kills the root first."""
    alive = {"value": True}
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(alive))

    def kill(argv, **kwargs):
        assert argv == ["taskkill", "/PID", "123", "/T", "/F"]
        alive["value"] = False
        return subprocess.CompletedProcess(argv, 255, b"child terminated", b"root already gone")

    monkeypatch.setattr(_helpers.subprocess, "run", kill)
    outcome = _helpers.taskkill_tree(123, timeout=0.01, interval=0)

    assert outcome.returncode == 255
    assert outcome.target_exited is True


def test_taskkill_never_accepts_a_nonzero_exit_while_the_target_identity_lives(monkeypatch) -> None:
    """A broad ignored-exit rule would hide a failed Desktop backend termination."""
    alive = {"value": True}
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(alive))
    monkeypatch.setattr(
        _helpers.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 255, b"", b"access denied"),
    )

    with pytest.raises(AssertionError, match="taskkill did not terminate original PID 123"):
        _helpers.taskkill_tree(123, timeout=0.001, interval=0)
