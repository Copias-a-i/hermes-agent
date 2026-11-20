"""Desktop cron ownership: is a gateway already ticking this profile's cron store?

The Desktop backend's in-process ticker stands down while a gateway owns the store and takes over
once that gateway stops (#126822). Every probe here fails closed: when absence cannot be positively
established, Desktop defers the tick.
"""

import logging
from pathlib import Path

# Same logger the code used before extraction (record parity). It is named ``logger`` because
# ruff's BLE001 only credits an ``exc_info=True`` traceback logged through a logger-looking name.
logger = logging.getLogger("hermes_cli.web_server")


def _desktop_gateway_runtime_lock_owned(home) -> bool:
    """Observe kernel ownership without creating or unlinking another store's lock."""
    from gateway.status import _probe_lock_file

    try:
        handle = (Path(home) / "gateway.lock").open("r+")
    except FileNotFoundError:
        return False
    return _probe_lock_file(handle)


def _gateway_owns_cron(name: str, home) -> bool:
    """Defer Desktop unless gateway absence is positively established."""
    from hermes_cli.profiles import _check_gateway_running
    from hermes_cli.gateway import named_profile_served_by_running_multiplexer
    from gateway.status import get_running_pid, resolve_gateway_liveness
    try:
        # A held per-home runtime lock is ownership even when an older identity
        # matcher cannot recognize the gateway's bootstrap command line.
        if _desktop_gateway_runtime_lock_owned(home):
            return True
        if _check_gateway_running(Path(home)):
            return True
        # The shared status resolver catches individual rung failures. Its
        # Boolean helper drops probe_error, so absence needs this strict check.
        liveness = resolve_gateway_liveness(
            profile_dir=Path(home), use_cache=False,
            pid_probe=lambda path: get_running_pid(path, cleanup_stale=False),
        )
        if liveness.running or liveness.probe_error:
            return True
        return (
            name != "default" and named_profile_served_by_running_multiplexer(name))
    except Exception:
        logger.warning(
            "Desktop cron: gateway-ownership probe failed for %s; deferring tick",
            name, exc_info=True,
        )
        return True
