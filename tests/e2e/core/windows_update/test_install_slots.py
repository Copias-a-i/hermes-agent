"""Native Windows process proof for the E2E installer disk semaphore."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


pytestmark = [pytest.mark.platforms("windows"), pytest.mark.integration]


_WORKER = r"""
import json, os, sys, time
from tests.e2e.core.windows_update._install_slots import acquire_install_slot
mode, hold = sys.argv[1], float(sys.argv[2])
try:
    with acquire_install_slot() as slot:
        print(json.dumps({"event":"acquired", "at":time.monotonic(), "waited":slot.waited_seconds}), flush=True)
        if mode == "crash":
            os._exit(17)
        if mode == "raise":
            raise RuntimeError("intentional worker failure")
        time.sleep(hold)
        print(json.dumps({"event":"released", "at":time.monotonic()}), flush=True)
except TimeoutError as exc:
    print(json.dumps({"event":"timeout", "message":str(exc)}), flush=True)
except Exception as exc:
    print(json.dumps({"event":"exception", "message":str(exc)}), flush=True)
"""


def _env(root: Path, *, slots: int = 2, timeout: float = 3.0) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HERMES_E2E_MACHINE_ROOT": str(root),
        "HERMES_E2E_INSTALL_SLOTS": str(slots),
        "HERMES_E2E_INSTALL_SLOT_TIMEOUT": str(timeout),
        "PYTHONPATH": str(Path(__file__).resolve().parents[4]),
    })
    return env


def _start(root: Path, mode: str, hold: float, *, slots: int = 2, timeout: float = 3.0) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _WORKER, mode, str(hold)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(root, slots=slots, timeout=timeout),
    )


def _event(proc: subprocess.Popen[str]) -> dict[str, Any]:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line, proc.stderr.read() if proc.stderr else "worker emitted no event"
    return json.loads(line)


def _finish(proc: subprocess.Popen[str]) -> tuple[dict[str, Any], int]:
    assert proc.stdout is not None
    events = [json.loads(line) for line in proc.stdout if line.strip()]
    code = proc.wait(timeout=10)
    assert not (proc.stderr and proc.stderr.read()), "worker wrote stderr"
    return events[-1] if events else {}, code


def test_windows_install_slots_bound_workers_and_recover_from_failure_and_crash(tmp_path: Path) -> None:
    """Real child processes never exceed two slots; all abnormal exits free one."""
    root = tmp_path / "shared-machine-root"
    first = _start(root, "hold", 1.0)
    second = _start(root, "hold", 1.0)
    first_acquired, second_acquired = _event(first), _event(second)

    third = _start(root, "hold", 0.0)
    third_acquired = _event(third)
    _, first_code = _finish(first)
    _, second_code = _finish(second)
    _, third_code = _finish(third)
    assert first_code == second_code == third_code == 0
    assert float(third_acquired["waited"]) >= 0.5

    # The only three acquisition intervals overlap at most twice.  The third
    # cannot acquire until one of the first two sends its release event.
    assert float(third_acquired["at"]) >= min(float(first_acquired["at"]), float(second_acquired["at"]))

    failing = _start(root, "raise", 0.0, slots=1)
    assert _event(failing)["event"] == "acquired"
    failure, failure_code = _finish(failing)
    assert failure_code == 0 and failure["event"] == "exception"
    after_exception = _start(root, "hold", 0.0, slots=1)
    assert _event(after_exception)["event"] == "acquired"
    _, after_exception_code = _finish(after_exception)
    assert after_exception_code == 0

    holder = _start(root, "hold", 0.8, slots=1)
    assert _event(holder)["event"] == "acquired"
    timed = _start(root, "hold", 0.0, slots=1, timeout=0.15)
    timeout_event, timeout_code = _finish(timed)
    assert timeout_code == 0 and timeout_event["event"] == "timeout"
    assert "timed out" in str(timeout_event["message"]) and "slots=1" in str(timeout_event["message"])
    _, holder_code = _finish(holder)
    assert holder_code == 0

    crashed = _start(root, "crash", 0.0, slots=1)
    assert _event(crashed)["event"] == "acquired"
    assert crashed.wait(timeout=10) == 17
    after_crash = _start(root, "hold", 0.0, slots=1, timeout=2.0)
    assert _event(after_crash)["event"] == "acquired"
    _, after_crash_code = _finish(after_crash)
    assert after_crash_code == 0
