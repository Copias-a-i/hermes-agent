"""Terminal runtime-status handshake of ``GatewayRunner.stop()``.

Split out of ``gateway/run_shutdown.py``. ``start_gateway`` returns as soon as ``wait_for_shutdown``
resolves, so ``stop()`` releases ``_shutdown_event`` only after the terminal ``gateway_state`` is
visible to a fresh reader, on the failure path as well as the normal one. ``gateway.status`` is
imported inside the bodies, so ``patch("gateway.status.X")`` keeps intercepting at call time.
"""

from __future__ import annotations

import logging
from typing import Any

from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


def settle_service_restart_verdict(runner: Any) -> None:
    """Exit 75 + ``RestartForceExitStatus=75``: systemd replaces us without a racing helper."""
    if runner._restart_requested and runner._restart_via_service:
        runner._exit_code = GATEWAY_SERVICE_RESTART_EXIT_CODE
        runner._exit_reason = runner._exit_reason or "Gateway restart requested"


async def persist_terminal_runtime_status(runner: Any, ctx: Any) -> bool:
    """Write the terminal ``gateway_state``, flush the writer, then read it back.

    The state is "stopped", or "running" on an UNEXPECTED signal (docker restart, OOM): container_boot.py
    only auto-starts gateways last seen "running". True only when a fresh reader sees that state;
    otherwise the runner exits non-zero (an explicit verdict such as the service-restart 75 stays).
    """
    terminal_gateway_state = "stopped"
    if getattr(runner, "_signal_initiated_shutdown", False) and not runner._restart_requested:
        terminal_gateway_state = "running"
        logger.info(
            "Gateway stopped by an unexpected signal — persisting "
            "gateway_state=running so container_boot auto-starts on the next boot (issue #42675)"
        )
    runner._update_runtime_status(terminal_gateway_state, runner._exit_reason)
    terminal_state_confirmed = False
    try:
        from gateway.status import flush_runtime_status_async, read_runtime_status
        # Never outlive the launchd exit budget (``_launchd_exit_timeout_s`` is set by the
        # supervised-restart path when it exists; a plain 2 s bound otherwise). The cap only
        # applies to signal-driven stops — a programmatic stop is not racing the supervisor.
        flush_timeout = 2.0
        budget = getattr(runner, "_launchd_exit_timeout_s", None)
        signal_stop = getattr(runner, "_stop_requested_by_signal", False)
        if signal_stop and isinstance(budget, (int, float)) and budget > 0:
            flush_timeout = max(0.0, min(flush_timeout, budget - ctx.elapsed()))
        if not await flush_runtime_status_async(timeout=flush_timeout):
            logger.warning("Timed out flushing terminal gateway runtime status")
        else:
            # A queue flush alone is not the contract: prove the terminal snapshot is visible to a
            # fresh reader before allowing the process main loop to exit.
            terminal_state_confirmed = (
                (read_runtime_status() or {}).get("gateway_state") == terminal_gateway_state
            )
            if not terminal_state_confirmed:
                logger.warning(
                    "Terminal gateway runtime status was not visible after flush (expected %s)",
                    terminal_gateway_state,
                )
    except Exception:
        logger.debug("Failed to flush terminal gateway runtime status", exc_info=True)
    runner._terminal_runtime_status_confirmed = terminal_state_confirmed
    if not terminal_state_confirmed:
        runner._exit_reason = runner._exit_reason or "Terminal gateway runtime status was not durably persisted"
        # Preserve the service-restart verdict (75), which is already a truthful non-zero failure
        # to the service manager.
        if runner._exit_code is None:
            runner._exit_code = 1
    return terminal_state_confirmed


async def release_shutdown_waiter(runner: Any, ctx: Any, *, teardown_completed: bool) -> None:
    """Release ``wait_for_shutdown`` once the terminal runtime status is durable.

    The persist phase writes it on the normal path (``teardown_completed``). When an earlier phase raised,
    or the stop task was cancelled, that phase never got there: fail the exit verdict, then write the
    terminal state here, bounded by the same flush timeout, so the process neither exits 0 over a
    non-terminal ``gateway_state.json`` nor leaves callers stranded behind the event.
    """
    try:
        if not teardown_completed:
            settle_service_restart_verdict(runner)
            runner._exit_reason = runner._exit_reason or "Gateway teardown failed before it completed"
            if runner._exit_code is None:
                runner._exit_code = 1
            await persist_terminal_runtime_status(runner, ctx)
    except Exception:
        logger.warning("Terminal gateway runtime status was not persisted after a failed teardown", exc_info=True)
    finally:
        shutdown_event = getattr(runner, "_shutdown_event", None)
        if shutdown_event is not None:
            shutdown_event.set()
