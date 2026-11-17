"""A finished turn's reply is durable before the turn settles, so a crash cannot lose it."""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path


def _load_plugin():
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "fleet-session-lifecycle"
    package = "hermes_plugins.fleet_session_lifecycle_per_turn_save"
    spec = importlib.util.spec_from_file_location(
        package, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    return module


def _enabled_profile(monkeypatch, tmp_path):
    plugin = _load_plugin()
    hooks = {}

    class Context:
        def register_hook(self, name, callback):
            hooks[name] = callback

    home = tmp_path / ".hermes" / "profiles" / "jarvis"
    vault = tmp_path / "vault"
    home.mkdir(parents=True)
    plugin.register(Context())
    monkeypatch.setattr(plugin, "_config", lambda: {"enabled": True, "vault_root": str(vault)})
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: home)
    return plugin, hooks, vault


def _finish_turn_then_crash(plugin, hooks):
    """The core persisted the reply and fired post_llm_call; the process dies before on_session_end."""
    hooks["pre_llm_call"](session_id="crashed", turn_id="turn", user_message="question")
    hooks["post_llm_call"](session_id="crashed", turn_id="turn", user_message="question",
                           assistant_response="the finished reply")
    plugin._pending.clear()  # process memory is gone


def _next_turn(hooks):
    hooks["pre_llm_call"](session_id="next", turn_id="t2", user_message="later")
    hooks["on_session_end"](session_id="next", turn_id="t2", completed=True, interrupted=False)


def _checkpoint(vault: Path) -> str:
    return next((vault / "jarvis" / "daily").glob("*-hermes-lifecycle.md")).read_text(encoding="utf-8")


def test_reply_survives_a_crash_between_post_llm_call_and_turn_end(monkeypatch, tmp_path):
    plugin, hooks, vault = _enabled_profile(monkeypatch, tmp_path)
    _finish_turn_then_crash(plugin, hooks)

    # While the owner is alive the recorded reply does not settle its turn early.
    _next_turn(hooks)
    assert "turn_id: turn\n" not in _checkpoint(vault)

    monkeypatch.setattr(plugin.LifecycleController, "_owner_is_alive", staticmethod(lambda *_a: False))
    hooks["pre_llm_call"](session_id="next", turn_id="t3", user_message="after restart")
    hooks["on_session_end"](session_id="next", turn_id="t3", completed=True, interrupted=False)

    checkpoint = _checkpoint(vault)
    assert "turn_id: turn\n" in checkpoint
    assert "response: the finished reply" in checkpoint


def test_lease_starved_reply_record_is_adopted_by_the_next_lease_holder(monkeypatch, tmp_path):
    plugin, hooks, vault = _enabled_profile(monkeypatch, tmp_path)
    hooks["pre_llm_call"](session_id="crashed", turn_id="turn", user_message="question")

    @contextlib.contextmanager
    def busy(_self):
        yield False

    with monkeypatch.context() as patch:
        patch.setattr(plugin.LifecycleController, "_lease_with_retry", busy)
        hooks["post_llm_call"](session_id="crashed", turn_id="turn", user_message="question",
                               assistant_response="the finished reply")
    plugin._pending.clear()

    monkeypatch.setattr(plugin.LifecycleController, "_owner_is_alive", staticmethod(lambda *_a: False))
    _next_turn(hooks)

    checkpoint = _checkpoint(vault)
    assert "turn_id: turn\n" in checkpoint
    assert "response: the finished reply" in checkpoint
