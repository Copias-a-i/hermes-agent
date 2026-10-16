"""Crash-safe, profile-scoped session lifecycle maintenance."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import types
from pathlib import Path


def _load_lifecycle():
    root = Path(__file__).resolve().parents[2]
    plugin_dir = root / "plugins" / "fleet-session-lifecycle"
    package = "hermes_plugins.fleet_session_lifecycle"
    if "hermes_plugins" not in sys.modules:
        parent = types.ModuleType("hermes_plugins")
        parent.__path__ = []
        sys.modules["hermes_plugins"] = parent
    spec = importlib.util.spec_from_file_location(
        package + ".lifecycle", plugin_dir / "lifecycle.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_plugin():
    root = Path(__file__).resolve().parents[2]
    plugin_dir = root / "plugins" / "fleet-session-lifecycle"
    package = "hermes_plugins.fleet_session_lifecycle_runtime"
    spec = importlib.util.spec_from_file_location(
        package, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    return module


def _controller(mod, tmp_path, *, profile="jarvis", runner=None):
    home = tmp_path / ".hermes" / "profiles" / profile
    home.mkdir(parents=True)
    vault = tmp_path / "vault"
    return mod.LifecycleController(home, {"vault_root": str(vault), "debounce_seconds": 0}, runner=runner), home, vault


def test_checkpoint_redacts_secrets_and_never_sends_vault_to_gbrain(tmp_path):
    mod = _load_lifecycle()
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0

    controller, home, vault = _controller(mod, tmp_path, runner=runner)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    controller.after_turn(
        session_id="s1", turn_id="t1", completed=True, interrupted=False,
        user_message="token=super-secret and AKIAABCDEFGHIJKLMNOP", assistant_response="done",
        conversation_history=[], cwd=str(repo),
    )

    checkpoint = vault / "jarvis" / "daily" / "hermes-lifecycle.md"
    text = checkpoint.read_text(encoding="utf-8")
    assert "super-secret" not in text and "AKIAABCDEFGHIJKLMNOP" not in text
    assert "[REDACTED]" in text
    assert calls and calls[0][0] == ["gbrain", "sync"]
    assert str(vault) not in " ".join(calls[0][0])
    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())
    assert state["gbrain"]["last_status"] == "ok"


def test_exact_once_with_concurrent_duplicate_delivery(tmp_path):
    mod = _load_lifecycle()
    controller, _home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)

    def invoke():
        controller.after_turn(session_id="s", turn_id="same", completed=True, interrupted=False,
                              user_message="u", assistant_response="a", conversation_history=[], cwd="")

    threads = [threading.Thread(target=invoke) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    rows = (vault / "jarvis" / "daily" / "hermes-lifecycle.md").read_text(encoding="utf-8")
    assert rows.count("turn_id: same") == 1


def test_interrupted_turn_is_checkpointed_and_unavailable_integrations_queue(tmp_path):
    mod = _load_lifecycle()

    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("gbrain")

    controller, home, vault = _controller(mod, tmp_path, runner=unavailable)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    controller.after_turn(session_id="s", turn_id="interrupt", completed=False, interrupted=True,
                          user_message="u", assistant_response="", conversation_history=[], cwd=str(repo))
    assert "interrupted: true" in (vault / "jarvis" / "daily" / "hermes-lifecycle.md").read_text()
    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())
    assert state["gbrain"]["write_pending"] is True
    assert state["gbrain"]["last_status"] == "unavailable"


def test_gbrain_debounce_coalesces_distinct_turns(tmp_path):
    mod = _load_lifecycle()
    calls = []
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    home = tmp_path / ".hermes" / "profiles" / "jarvis"
    home.mkdir(parents=True)
    controller = mod.LifecycleController(home, {"vault_root": str(tmp_path / "vault"), "debounce_seconds": 3600},
                                         runner=lambda argv, **_kwargs: calls.append(argv) or 0)
    for turn_id in ("one", "two"):
        controller.after_turn(session_id="s", turn_id=turn_id, completed=True, interrupted=False,
                              user_message="u", assistant_response="a", conversation_history=[], cwd=str(root))
    assert calls == [["gbrain", "sync"]]


def test_profiles_use_separate_vault_and_state_paths(tmp_path):
    mod = _load_lifecycle()
    first, home_a, vault = _controller(mod, tmp_path, profile="alpha", runner=lambda *_a, **_k: 0)
    second, home_b, _ = _controller(mod, tmp_path, profile="beta", runner=lambda *_a, **_k: 0)
    first.after_turn(session_id="s", turn_id="a", completed=True, interrupted=False,
                     user_message="alpha", assistant_response="", conversation_history=[], cwd="")
    second.after_turn(session_id="s", turn_id="b", completed=True, interrupted=False,
                      user_message="beta", assistant_response="", conversation_history=[], cwd="")
    assert "alpha" in (vault / "alpha" / "daily" / "hermes-lifecycle.md").read_text()
    assert "beta" not in (vault / "alpha" / "daily" / "hermes-lifecycle.md").read_text()
    assert (home_a / "state" / "fleet-session-lifecycle.json").exists()
    assert (home_b / "state" / "fleet-session-lifecycle.json").exists()


def test_write_pending_gbrain_is_not_reentered_or_run_for_non_repo(tmp_path):
    mod = _load_lifecycle()
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return 75

    controller, home, _vault = _controller(mod, tmp_path, runner=runner)
    controller.after_turn(session_id="s", turn_id="one", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd=str(tmp_path / "not-a-repo"))
    assert calls == []
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    controller.after_turn(session_id="s", turn_id="two", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd=str(repo))
    controller.after_turn(session_id="s", turn_id="three", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd=str(repo))
    assert calls == [["gbrain", "sync"]]
    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())
    assert state["gbrain"]["write_pending"] is True


def test_interruption_uses_pre_llm_snapshot_and_registers_start_capture(monkeypatch, tmp_path):
    plugin = _load_plugin()
    registered = {}

    class Context:
        def register_hook(self, name, callback):
            registered[name] = callback

    plugin.register(Context())
    assert "pre_llm_call" in registered
    captured = {}

    class Controller:
        def __init__(self, *_args, **_kwargs):
            pass

        def after_turn(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(plugin, "LifecycleController", Controller)
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path / ".hermes")
    registered["pre_llm_call"](session_id="crashed", turn_id="turn", user_message="before crash")
    registered["on_session_end"](
        session_id="crashed", turn_id="turn", completed=False, interrupted=True,
    )
    assert captured["user_message"] == "before crash"
    assert captured["interrupted"] is True


def test_pinecone_unavailable_stays_pending_without_touching_gbrain(tmp_path):
    mod = _load_lifecycle()

    def unavailable(argv, **_kwargs):
        if argv[0] == "pinecone-adapter":
            raise FileNotFoundError("pinecone-adapter")
        raise AssertionError("gbrain must not run outside a repository")

    home = tmp_path / ".hermes" / "profiles" / "jarvis"
    home.mkdir(parents=True)
    controller = mod.LifecycleController(
        home,
        {"vault_root": str(tmp_path / "vault"), "pinecone_command": ["pinecone-adapter"]},
        runner=unavailable,
    )
    controller.after_turn(
        session_id="s", turn_id="pinecone-down", completed=True, interrupted=False,
        user_message="safe summary", assistant_response="ok", conversation_history=[], cwd="",
    )
    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())
    assert state["pinecone"] == {"last_status": "unavailable", "write_pending": True}
    assert "gbrain" not in state
