"""Bundled profile-local crash-safe lifecycle hook."""

from __future__ import annotations

import logging
import threading
from typing import Any

from hermes_constants import get_hermes_home

from .lifecycle import LifecycleController

logger = logging.getLogger(__name__)

_pending: dict[tuple[str, str], dict[str, Any]] = {}
_pending_lock = threading.Lock()


def _config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        root = load_config_readonly() or {}
        value = root.get("fleet_session_lifecycle")
        return value if isinstance(value, dict) else {}
    except Exception:
        logger.exception("fleet-session-lifecycle: config unreadable; using built-in defaults")
        return {}


def _enabled(config: dict[str, Any]) -> bool:
    """Run hooks only when the profile explicitly opts into lifecycle maintenance."""
    return config.get("enabled") is True


def _drop_pending(session_id: str, turn_id: str) -> None:
    """Release in-memory turn snapshots a disabled profile will never checkpoint.

    A profile that opts out mid-session would otherwise retain the last inbound
    message for the lifetime of the process.
    """
    with _pending_lock:
        if turn_id:
            _pending.pop((session_id, turn_id), None)
        elif session_id:
            for key in [key for key in _pending if key[0] == session_id]:
                _pending.pop(key, None)


def _on_pre_llm_call(**kwargs: Any) -> None:
    """Snapshot the raw inbound turn before execution so interrupts retain context."""
    try:
        config = _config()
        session_id, turn_id = str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or "")
        if not _enabled(config):
            _drop_pending(session_id, turn_id)
            return
        controller = LifecycleController(get_hermes_home(), config)
        # Durable before the model call: an interrupt after this point is recoverable.
        controller.admit_turn(session_id=session_id, turn_id=turn_id, user_message=kwargs.get("user_message", ""))
        with _pending_lock:
            _pending[(session_id, turn_id)] = {
                "user_message": kwargs.get("user_message", ""),
                "assistant_response": "",
            }
    except Exception:
        logger.exception("fleet-session-lifecycle: pre-llm admission failed")


def _on_post_llm_call(**kwargs: Any) -> None:
    """Add final output after it is persisted; keep the pre-turn crash snapshot."""
    config = _config()
    session_id, turn_id = str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or "")
    if not _enabled(config):
        _drop_pending(session_id, turn_id)
        return
    if not session_id or not turn_id:
        return
    with _pending_lock:
        snapshot = _pending.setdefault((session_id, turn_id), {})
        snapshot.update({
            "user_message": kwargs.get("user_message", snapshot.get("user_message", "")),
            "assistant_response": kwargs.get("assistant_response", ""),
        })


def _on_session_end(**kwargs: Any) -> None:
    """Checkpoint after core transcript persistence; all errors stay fail-open."""
    try:
        config = _config()
        session_id, turn_id = str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or "")
        if not _enabled(config):
            # Session end is terminal for its cached snapshots; do not retain a
            # mismatched or omitted turn identifier after an opt-out.
            _drop_pending(session_id, "")
            return
        with _pending_lock:
            if not turn_id and session_id:
                # Shutdown paths that omit turn_id still need the pre-turn identity.
                matching = [key for key in _pending if key[0] == session_id]
                if matching:
                    turn_id = matching[-1][1]
            saved = _pending.pop((session_id, turn_id), {})
        controller = LifecycleController(get_hermes_home(), config)
        controller.after_turn(
            session_id=session_id, turn_id=turn_id,
            completed=bool(kwargs.get("completed")), interrupted=bool(kwargs.get("interrupted")),
            user_message=saved.get("user_message", ""), assistant_response=saved.get("assistant_response", ""),
        )
    except Exception:
        logger.exception("fleet-session-lifecycle: turn checkpoint failed; turn stays deliverable")
        return


def _on_session_finalize(**kwargs: Any) -> None:
    """Flush a durable full wrap-up checkpoint without producing a user response."""
    try:
        config = _config()
        if not _enabled(config):
            return
        LifecycleController(get_hermes_home(), config).full_wrapup(
            session_id=str(kwargs.get("session_id") or ""),
            reason=str(kwargs.get("reason") or ""),
        )
    except Exception:
        logger.exception("fleet-session-lifecycle: full wrap-up flush failed; session still ends")
        return


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
