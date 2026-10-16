"""Bundled profile-local crash-safe lifecycle hook."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
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
    """Explicit profile disable wins over every lifecycle hook."""
    return config.get("enabled") is not False


def _on_pre_llm_call(**kwargs: Any) -> None:
    """Snapshot the raw inbound turn before execution so interrupts retain context."""
    if not _enabled(_config()):
        return
    session_id, turn_id = str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or "")
    if not session_id or not turn_id:
        return
    with _pending_lock:
        _pending[(session_id, turn_id)] = {
            "user_message": kwargs.get("user_message", ""),
            "assistant_response": "",
            "conversation_history": kwargs.get("conversation_history") or [],
        }


def _on_post_llm_call(**kwargs: Any) -> None:
    """Add final output after it is persisted; keep the pre-turn crash snapshot."""
    if not _enabled(_config()):
        return
    session_id, turn_id = str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or "")
    if not session_id or not turn_id:
        return
    with _pending_lock:
        snapshot = _pending.setdefault((session_id, turn_id), {})
        snapshot.update({
            "user_message": kwargs.get("user_message", snapshot.get("user_message", "")),
            "assistant_response": kwargs.get("assistant_response", ""),
            "conversation_history": kwargs.get("conversation_history") or snapshot.get("conversation_history", []),
        })


def _on_session_end(**kwargs: Any) -> None:
    """Checkpoint after core transcript persistence; all errors stay fail-open."""
    try:
        config = _config()
        if not _enabled(config):
            return
        session_id, turn_id = str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or "")
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
            conversation_history=saved.get("conversation_history", []), cwd=str(kwargs.get("cwd") or Path.cwd()),
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
