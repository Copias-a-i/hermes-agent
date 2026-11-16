"""Stop helpers of ``hermes gateway stop``: the planned-stop marker and the durable-stop wait.

Split out of ``hermes_cli/gateway.py``, which re-exports both so ``patch("hermes_cli.gateway.X")`` keeps
intercepting them. ``gateway.status`` is imported inside the bodies for the same reason.
"""

from __future__ import annotations

import logging
import time

# Log-record parity with the facade.
logger = logging.getLogger("hermes_cli.gateway")

_STOP_TERMINAL_STATE_TIMEOUT = 5.0


def _mark_planned_stop(pid: int | None = None) -> None:
    """Best-effort planned-stop marker for ``pid`` (default: the recorded gateway PID)."""
    try:
        from gateway.status import get_running_pid, write_planned_stop_marker
        if pid is None:
            pid = get_running_pid(cleanup_stale=False)
        if pid is not None:
            write_planned_stop_marker(pid)
    except Exception:
        logger.debug("Could not write the planned-stop marker", exc_info=True)


def _wait_for_terminal_stopped_state(timeout: float = _STOP_TERMINAL_STATE_TIMEOUT) -> bool:
    """Wait boundedly until a fresh runtime-status read proves a clean stop."""
    from gateway.status import get_running_pid, read_runtime_status

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        state = read_runtime_status() or {}
        if state.get("gateway_state") == "stopped" and get_running_pid() is None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
