"""Crash-safe, profile-scoped session lifecycle maintenance."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import sys
import threading
import types
from pathlib import Path

import pytest

from hermes_cli.plugin_validate import validate_plugin_dir


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
    return mod.LifecycleController(home, {"vault_root": str(vault)}, runner=runner), home, vault


def _checkpoint_file(vault: Path, profile: str) -> Path:
    return next((vault / profile / "daily").glob("*-hermes-lifecycle.md"))


def _hold_profile_lease(home: str, vault: str, ready, release) -> None:
    """Hold the real POSIX profile lease from an independent process."""
    mod = _load_lifecycle()
    controller = mod.LifecycleController(Path(home), {"vault_root": vault})
    with controller._lease() as acquired:
        assert acquired
        ready.set()
        assert release.wait(5)


def test_checkpoint_redacts_secrets_and_never_sends_vault_to_external_outbox(tmp_path):
    mod = _load_lifecycle()
    controller, home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    controller.config["outbox_command"] = ["indexer"]
    controller.after_turn(
        session_id="s1", turn_id="t1", completed=True, interrupted=False,
        user_message="token=super-secret and AKIAABCDEFGHIJKLMNOP", assistant_response="done",
        conversation_history=[], cwd="",
    )
    checkpoint = _checkpoint_file(vault, "jarvis")
    text = checkpoint.read_text(encoding="utf-8")
    assert "super-secret" not in text and "AKIAABCDEFGHIJKLMNOP" not in text
    assert "[REDACTED]" in text
    assert not (home / "state" / "fleet-session-lifecycle-outbox.jsonl").exists()


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
    rows = _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    assert rows.count("turn_id: same") == 1


def test_interrupted_turn_is_checkpointed_and_unavailable_outbox_queues(tmp_path):
    mod = _load_lifecycle()

    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("indexer")

    controller, home, vault = _controller(mod, tmp_path, runner=unavailable)
    controller.config["outbox_command"] = ["indexer"]
    controller.after_turn(session_id="s", turn_id="interrupt", completed=False, interrupted=True,
                          user_message="u", assistant_response="", conversation_history=[], cwd="")
    assert "interrupted: true" in _checkpoint_file(vault, "jarvis").read_text()
    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())
    assert state["outbox"]["write_pending"] is True
    assert state["outbox"]["last_status"] == "unavailable"


def test_profiles_use_separate_vault_and_state_paths(tmp_path):
    mod = _load_lifecycle()
    first, home_a, vault = _controller(mod, tmp_path, profile="alpha", runner=lambda *_a, **_k: 0)
    second, home_b, _ = _controller(mod, tmp_path, profile="beta", runner=lambda *_a, **_k: 0)
    first.after_turn(session_id="s", turn_id="a", completed=True, interrupted=False,
                     user_message="alpha", assistant_response="", conversation_history=[], cwd="")
    second.after_turn(session_id="s", turn_id="b", completed=True, interrupted=False,
                      user_message="beta", assistant_response="", conversation_history=[], cwd="")
    assert "alpha" in _checkpoint_file(vault, "alpha").read_text()
    assert "beta" not in _checkpoint_file(vault, "alpha").read_text()
    assert (home_a / "state" / "fleet-session-lifecycle.json").exists()
    assert (home_b / "state" / "fleet-session-lifecycle.json").exists()


def test_outbox_failure_is_not_reentered_without_new_delivery(tmp_path):
    mod = _load_lifecycle()
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return 75

    controller, home, _vault = _controller(mod, tmp_path, runner=runner)
    controller.config["outbox_command"] = ["indexer"]
    controller.after_turn(session_id="s", turn_id="one", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd="")
    assert calls and calls[0][:1] == ["indexer"]
    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())
    assert state["outbox"]["write_pending"] is True


def test_enabled_requires_an_explicit_boolean_true():
    plugin = _load_plugin()

    assert plugin._enabled({"enabled": True}) is True
    assert plugin._enabled({}) is False
    assert plugin._enabled({"enabled": "true"}) is False
    assert plugin._enabled({"enabled": False}) is False


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

        def admit_turn(self, **_kwargs):
            return None

        def after_turn(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(plugin, "LifecycleController", Controller)
    monkeypatch.setattr(plugin, "_config", lambda: {"enabled": True})
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path / ".hermes")
    registered["pre_llm_call"](session_id="crashed", turn_id="turn", user_message="before crash")
    registered["on_session_end"](
        session_id="crashed", turn_id="turn", completed=False, interrupted=True,
    )
    assert captured["user_message"] == "before crash"
    assert captured["interrupted"] is True


def test_pre_llm_admission_survives_process_loss_before_session_end(monkeypatch, tmp_path):
    plugin = _load_plugin()
    registered = {}

    class Context:
        def register_hook(self, name, callback):
            registered[name] = callback

    home = tmp_path / ".hermes" / "profiles" / "jarvis"
    vault = tmp_path / "vault"
    home.mkdir(parents=True)
    plugin.register(Context())
    monkeypatch.setattr(plugin, "_config", lambda: {"enabled": True, "vault_root": str(vault)})
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: home)

    registered["pre_llm_call"](session_id="crashed", turn_id="turn", user_message="before crash")
    plugin._pending.clear()
    registered["on_session_end"](
        session_id="crashed", turn_id="turn", completed=False, interrupted=True,
    )

    checkpoint = _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    assert "user: before crash" in checkpoint
    assert "interrupted: true" in checkpoint


def test_disabled_session_end_purges_all_pending_snapshots(monkeypatch):
    plugin = _load_plugin()
    plugin._pending.clear()
    plugin._pending.update({
        ("disabled", "first"): {"user_message": "one"},
        ("disabled", "second"): {"user_message": "two"},
        ("other", "third"): {"user_message": "three"},
    })
    monkeypatch.setattr(plugin, "_config", lambda: {"enabled": False})

    plugin._on_session_end(session_id="disabled", turn_id="mismatched")

    assert not any(key[0] == "disabled" for key in plugin._pending)
    assert ("other", "third") in plugin._pending
    plugin._pending.clear()


def test_outbox_adapter_unavailable_keeps_a_durable_local_record(tmp_path):
    mod = _load_lifecycle()

    def unavailable(argv, **_kwargs):
        if argv[0] == "external-outbox-adapter":
            raise FileNotFoundError("external-outbox-adapter")
        raise AssertionError("only the configured outbox adapter may run")

    home = tmp_path / ".hermes" / "profiles" / "jarvis"
    home.mkdir(parents=True)
    controller = mod.LifecycleController(
        home,
        {"vault_root": str(tmp_path / "vault"), "outbox_command": ["external-outbox-adapter"]},
        runner=unavailable,
    )
    controller.after_turn(
        session_id="s", turn_id="adapter-down", completed=True, interrupted=False,
        user_message="safe summary", assistant_response="ok", conversation_history=[], cwd="",
    )
    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())
    assert state["outbox"] == {"last_status": "unavailable", "write_pending": True}
    assert (home / "state" / "fleet-session-lifecycle-outbox.jsonl").exists()
    assert "outbox" in state


def test_final_wrapup_is_idempotent_and_never_runs_a_model_or_returns_a_message(tmp_path):
    mod = _load_lifecycle()
    calls = []
    controller, home, vault = _controller(mod, tmp_path, runner=lambda *args, **kwargs: calls.append((args, kwargs)) or 0)

    assert controller.full_wrapup(session_id="closing", reason="reset") is None
    assert controller.full_wrapup(session_id="closing", reason="reset") is None

    checkpoint = _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    assert checkpoint.count("hermes-lifecycle full-wrapup checkpoint") == 1
    assert "session_id: closing" in checkpoint
    assert "reason: reset" in checkpoint
    assert calls == []
    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())
    assert state["finalized_sessions"]


def test_finalize_hook_runs_full_wrapup_without_response_delivery(monkeypatch, tmp_path):
    plugin = _load_plugin()
    registered = {}

    class Context:
        def register_hook(self, name, callback):
            registered[name] = callback

    plugin.register(Context())
    assert "on_session_finalize" in registered
    captured = {}

    class Controller:
        def __init__(self, *_args, **_kwargs):
            pass

        def full_wrapup(self, **kwargs):
            captured.update(kwargs)
            return "must not deliver"

    monkeypatch.setattr(plugin, "LifecycleController", Controller)
    monkeypatch.setattr(plugin, "_config", lambda: {"enabled": True})
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path / ".hermes")
    assert registered["on_session_finalize"](session_id="closing", reason="quit") is None
    assert captured == {"session_id": "closing", "reason": "quit"}


def test_outbox_queue_is_two_lf_delimited_json_records(tmp_path):
    mod = _load_lifecycle()

    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("external-outbox-adapter")

    controller, home, _vault = _controller(
        mod,
        tmp_path,
        runner=unavailable,
    )
    controller.config["outbox_command"] = ["external-outbox-adapter"]
    for turn_id in ("one", "two"):
        controller.after_turn(
            session_id="s",
            turn_id=turn_id,
            completed=True,
            interrupted=False,
            user_message="safe summary",
            assistant_response="ok",
            conversation_history=[],
            cwd="",
        )

    raw_queue = (home / "state" / "fleet-session-lifecycle-outbox.jsonl").read_bytes()
    assert raw_queue.count(b"\n") == 2
    rows = [json.loads(line) for line in raw_queue.splitlines()]
    assert [row["turn_id"] for row in rows] == ["one", "two"]


def test_bom_prefixed_lifecycle_state_is_preserved(tmp_path):
    mod = _load_lifecycle()
    controller, home, _vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    state_path = home / "state" / "fleet-session-lifecycle.json"
    state_path.parent.mkdir()
    state_path.write_bytes(b'\xef\xbb\xbf{"retained": true}')

    controller.after_turn(
        session_id="s",
        turn_id="one",
        completed=True,
        interrupted=False,
        user_message="u",
        assistant_response="a",
        conversation_history=[],
        cwd="",
    )

    assert json.loads(state_path.read_text(encoding="utf-8-sig"))["retained"] is True



def test_empty_outbox_command_has_no_queue_or_runner_side_effect(tmp_path):
    mod = _load_lifecycle()
    calls = []
    home = tmp_path / ".hermes" / "profiles" / "jarvis"
    home.mkdir(parents=True)
    controller = mod.LifecycleController(
        home,
        {"vault_root": str(tmp_path / "vault"), "outbox_command": []},
        runner=lambda argv, **kwargs: calls.append((argv, kwargs)) or 0,
    )

    controller.after_turn(
        session_id="s", turn_id="empty-outbox", completed=True, interrupted=False,
        user_message="safe summary", assistant_response="ok", conversation_history=[], cwd="",
    )

    assert calls == []
    assert not (home / "state" / "fleet-session-lifecycle-outbox.jsonl").exists()


def test_vault_root_expands_home_before_checkpoint_path_use(tmp_path, monkeypatch):
    mod = _load_lifecycle()
    home = tmp_path / ".hermes" / "profiles" / "jarvis"
    home.mkdir(parents=True)
    configured_home = tmp_path / "configured-home"
    monkeypatch.setenv("HOME", str(configured_home))
    controller = mod.LifecycleController(home, {"vault_root": "~/.agent-vault"}, runner=lambda *_a, **_k: 0)

    controller.after_turn(
        session_id="s", turn_id="tilde-root", completed=True, interrupted=False,
        user_message="u", assistant_response="a", conversation_history=[], cwd="",
    )

    assert list((configured_home / ".agent-vault" / "jarvis" / "daily").glob("*-hermes-lifecycle.md"))
    assert not (Path.cwd() / "~" / ".agent-vault").exists()


def test_stale_dead_lock_is_reclaimed_but_live_lock_is_preserved(tmp_path, monkeypatch):
    mod = _load_lifecycle()
    home = tmp_path / ".hermes" / "profiles" / "jarvis"
    home.mkdir(parents=True)
    controller = mod.LifecycleController(
        home, {"vault_root": str(tmp_path / "vault"), "lock_stale_seconds": 0}, runner=lambda *_a, **_k: 0,
    )
    controller.lock_path.parent.mkdir()
    controller.lock_path.write_text("999999", encoding="ascii")
    os.utime(controller.lock_path, (0, 0))
    controller.after_turn(session_id="s", turn_id="reclaimed", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd="")
    checkpoint = controller._checkpoint_path()
    assert "turn_id: reclaimed" in checkpoint.read_text()

    original_flock = mod.fcntl.flock

    def busy_flock(_fd, flags):
        if flags & mod.fcntl.LOCK_NB:
            raise BlockingIOError
        return original_flock(_fd, flags)

    monkeypatch.setattr(mod.fcntl, "flock", busy_flock)
    controller.after_turn(session_id="s", turn_id="live", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd="")
    assert "turn_id: live" not in checkpoint.read_text()


def test_processed_turns_retains_arrival_order_at_the_recency_bound(tmp_path):
    mod = _load_lifecycle()
    controller, home, _vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    state_path = home / "state" / "fleet-session-lifecycle.json"
    state_path.parent.mkdir()
    old_ids = [f"old-{index}" for index in range(512)]
    state_path.write_text(json.dumps({"processed_turns": old_ids}), encoding="utf-8")
    controller.after_turn(session_id="s", turn_id="newest", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd="")
    processed = json.loads(state_path.read_text())["processed_turns"]
    expected_newest = __import__("hashlib").sha256(b"s\0newest").hexdigest()
    assert processed == old_ids[1:] + [expected_newest]


def test_profile_scoped_process_leases_do_not_serialize_unrelated_profiles(tmp_path):
    mod = _load_lifecycle()
    entered = threading.Event()
    release = threading.Event()
    alpha, _home_a, _vault = _controller(mod, tmp_path / "alpha", profile="alpha", runner=lambda *_a, **_k: 0)
    beta, _home_b, _vault = _controller(mod, tmp_path / "beta", profile="beta", runner=lambda *_a, **_k: 0)
    original_append = alpha._append_checkpoint

    def blocked_append(event):
        entered.set()
        assert release.wait(2)
        original_append(event)

    alpha._append_checkpoint = blocked_append
    first = threading.Thread(target=lambda: alpha.after_turn(
        session_id="s", turn_id="alpha", completed=True, interrupted=False,
        user_message="u", assistant_response="a", conversation_history=[], cwd="",
    ))
    first.start()
    assert entered.wait(2)
    beta_finished = threading.Event()

    def checkpoint_beta():
        beta.after_turn(
            session_id="s", turn_id="beta", completed=True, interrupted=False,
            user_message="u", assistant_response="a", conversation_history=[], cwd="",
        )
        beta_finished.set()

    second = threading.Thread(target=checkpoint_beta)
    second.start()
    assert beta_finished.wait(2.0)
    release.set()
    first.join(2)
    second.join(2)
    assert not second.is_alive()


def test_stale_lease_reclamation_retains_the_current_owner_metadata(tmp_path):
    mod = _load_lifecycle()
    controller, _home, _vault = _controller(
        mod, tmp_path, runner=lambda *_a, **_k: 0,
    )
    controller.lock_path.parent.mkdir(parents=True)
    controller.lock_path.write_text("999999", encoding="ascii")
    os.utime(controller.lock_path, (0, 0))

    with controller._lease() as acquired:
        assert acquired is True
    assert controller.lock_path.read_text(encoding="ascii").startswith(f"{os.getpid()}:")


@pytest.mark.platforms("windows")
def test_windows_stale_exclusive_lock_is_reclaimed_when_owner_is_dead(monkeypatch, tmp_path):
    mod = _load_lifecycle()
    controller, _home, _vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    controller.config["lock_stale_seconds"] = 0
    controller.lock_path.parent.mkdir(parents=True)
    controller.lock_path.write_text("999999:abandoned", encoding="ascii")
    monkeypatch.setattr(mod, "fcntl", None)

    with controller._lease() as acquired:
        assert acquired is True
        assert controller.lock_path.read_text(encoding="ascii").startswith(f"{os.getpid()}:")

    assert not controller.lock_path.exists()


def test_manifest_validation_probes_the_registered_hooks():
    root = Path(__file__).resolve().parents[2]

    report = validate_plugin_dir(root / "plugins" / "fleet-session-lifecycle")

    assert report.ok, report.failures
    assert any(name == "capability probe" and ok for name, ok, _detail in report.checks)


def test_authorization_schemes_redact_the_following_credential(tmp_path):
    mod = _load_lifecycle()
    controller, _home, _vault = _controller(mod, tmp_path)
    exported = controller._append_checkpoint  # keep controller construction at public config seam
    assert exported
    text = mod.redact_for_export("Authorization: Bearer jwt-secret-value")
    assert "jwt-secret-value" not in text
    assert "[REDACTED]" in text


def test_checkpoint_path_is_date_partitioned(monkeypatch, tmp_path):
    mod = _load_lifecycle()
    controller, _home, vault = _controller(mod, tmp_path)
    monkeypatch.setattr(mod.time, "strftime", lambda _format: "2026-10-16")
    assert controller._checkpoint_path() == vault / "jarvis" / "daily" / "2026-10-16-hermes-lifecycle.md"


def test_outbox_failure_is_retried_by_a_later_turn(tmp_path):
    mod = _load_lifecycle()
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        return 1 if len(calls) == 1 else 0

    controller, home, _vault = _controller(mod, tmp_path, runner=runner)
    controller.config["outbox_command"] = ["external-outbox-adapter"]
    for turn_id in ("first", "second"):
        controller.after_turn(
            session_id="s", turn_id=turn_id, completed=True, interrupted=False,
            user_message="u", assistant_response="a", conversation_history=[], cwd="",
        )

    assert calls == [
        ["external-outbox-adapter", "--input", str(home / "state" / "fleet-session-lifecycle-outbox.jsonl")],
        ["external-outbox-adapter", "--input", str(home / "state" / "fleet-session-lifecycle-outbox.jsonl")],
    ]
    assert json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())["outbox"]["last_status"] == "ok"


def test_outbox_adapter_acknowledges_the_durable_queue(tmp_path):
    mod = _load_lifecycle()
    controller, home, _vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    controller.config["outbox_command"] = ["indexer"]

    controller.after_turn(session_id="s", turn_id="ack", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd="")

    assert not (home / "state" / "fleet-session-lifecycle-outbox.jsonl").exists()
    assert json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())["outbox"]["write_pending"] is False


def test_finalized_sessions_keep_most_recent_arrival_order(tmp_path):
    mod = _load_lifecycle()
    controller, home, _vault = _controller(mod, tmp_path)
    state_path = home / "state" / "fleet-session-lifecycle.json"
    old_ids = [f"old-{n}" for n in range(512)]
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"finalized_sessions": old_ids}), encoding="utf-8")

    controller.full_wrapup(session_id="newest")

    finalized = json.loads(state_path.read_text())["finalized_sessions"]
    expected = __import__("hashlib").sha256(b"newest").hexdigest()
    assert finalized == old_ids[1:] + [expected]


@pytest.mark.platforms("windows")
def test_windows_lease_uses_exclusive_create_and_removes_its_owned_lock(monkeypatch, tmp_path):
    mod = _load_lifecycle()
    controller, _home, _vault = _controller(mod, tmp_path)
    monkeypatch.setattr(mod, "fcntl", None)

    with controller._lease() as acquired:
        assert acquired is True
        assert controller.lock_path.exists()

    assert not controller.lock_path.exists()


def test_next_turn_replays_other_durable_pending_events(tmp_path):
    mod = _load_lifecycle()
    controller, home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    old_event_id = "old-event"
    state_path = home / "state" / "fleet-session-lifecycle.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({"pending_turns": {old_event_id: {
        "event_id": old_event_id,
        "session_id": "old-session",
        "turn_id": "old-turn",
        "completed": True,
        "interrupted": False,
        "user": "old-user",
        "response": "old-response",
    }}}), encoding="utf-8")

    controller.after_turn(session_id="new-session", turn_id="new-turn", completed=True, interrupted=False,
                          user_message="new-user", assistant_response="new-response", conversation_history=[], cwd="")

    checkpoint = _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "event_id: old-event" in checkpoint
    assert old_event_id not in state["pending_turns"]


def test_admitted_turn_checks_only_its_recorded_partition(monkeypatch, tmp_path):
    mod = _load_lifecycle()
    controller, _home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    daily = vault / "jarvis" / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-10-15-hermes-lifecycle.md").write_text("- event_id: old\n", encoding="utf-8")

    original_iterdir = type(daily).iterdir

    def no_history_scan(path):
        if path == daily:
            raise AssertionError("ordinary admitted turns must not enumerate daily history")
        return original_iterdir(path)

    monkeypatch.setattr(type(daily), "iterdir", no_history_scan)
    controller.after_turn(session_id="s", turn_id="current", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd="")

    assert "turn_id: current" in controller._checkpoint_path().read_text(encoding="utf-8")


def test_recovery_checks_prior_daily_partition_before_reappending(monkeypatch, tmp_path):
    mod = _load_lifecycle()
    controller, home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    old_event_id = "old-event"
    old_checkpoint = vault / "jarvis" / "daily" / "2026-10-16-hermes-lifecycle.md"
    old_checkpoint.parent.mkdir(parents=True)
    old_checkpoint.write_text(
        f"- event_id: {old_event_id}\n<!-- hermes-lifecycle checkpoint committed: {old_event_id} -->\n",
        encoding="utf-8",
    )
    state_path = home / "state" / "fleet-session-lifecycle.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({"pending_turns": {old_event_id: {
        "event_id": old_event_id, "session_id": "old-session", "turn_id": "old-turn",
        "completed": True, "interrupted": False, "user": "old-user", "response": "old-response",
    }}}), encoding="utf-8")
    monkeypatch.setattr(mod.time, "strftime", lambda _format: "2026-10-17")

    controller.after_turn(session_id="new-session", turn_id="new-turn", completed=True, interrupted=False,
                          user_message="new-user", assistant_response="new-response", conversation_history=[], cwd="")

    today = vault / "jarvis" / "daily" / "2026-10-17-hermes-lifecycle.md"
    assert "event_id: old-event" not in today.read_text(encoding="utf-8")


def test_recovered_pending_events_reach_the_durable_outbox_before_clear(tmp_path):
    mod = _load_lifecycle()
    controller, home, _vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 1)
    controller.config["outbox_command"] = ["external-outbox-adapter"]
    old_event_id = "old-event"
    state_path = home / "state" / "fleet-session-lifecycle.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({"pending_turns": {old_event_id: {
        "event_id": old_event_id, "session_id": "old-session", "turn_id": "old-turn",
        "completed": True, "interrupted": False, "user": "old-user", "response": "old-response",
    }}}), encoding="utf-8")

    controller.after_turn(session_id="new-session", turn_id="new-turn", completed=True, interrupted=False,
                          user_message="new-user", assistant_response="new-response", conversation_history=[], cwd="")

    queue = home / "state" / "fleet-session-lifecycle-outbox.jsonl"
    assert [row["turn_id"] for row in map(json.loads, queue.read_text(encoding="utf-8").splitlines())] == ["old-turn", "new-turn"]
    assert old_event_id not in json.loads(state_path.read_text(encoding="utf-8"))["pending_turns"]


def test_renamed_profile_writes_only_to_its_current_namespace(tmp_path):
    mod = _load_lifecycle()
    vault = tmp_path / "vault"
    beta_home = tmp_path / ".hermes" / "profiles" / "beta"
    beta_home.mkdir(parents=True)
    beta_state = beta_home / "state" / "fleet-session-lifecycle.json"
    beta_state.parent.mkdir()
    beta_state.write_text(json.dumps({"vault_profile": "alpha"}), encoding="utf-8")
    alpha_home = tmp_path / ".hermes" / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    beta = mod.LifecycleController(beta_home, {"vault_root": str(vault)}, runner=lambda *_a, **_k: 0)
    alpha = mod.LifecycleController(alpha_home, {"vault_root": str(vault)}, runner=lambda *_a, **_k: 0)

    beta.after_turn(session_id="s", turn_id="beta", completed=True, interrupted=False,
                    user_message="beta-message", assistant_response="", conversation_history=[], cwd="")
    alpha.after_turn(session_id="s", turn_id="alpha", completed=True, interrupted=False,
                     user_message="alpha-message", assistant_response="", conversation_history=[], cwd="")

    assert "beta-message" in _checkpoint_file(vault, "beta").read_text(encoding="utf-8")
    assert "alpha-message" in _checkpoint_file(vault, "alpha").read_text(encoding="utf-8")
    assert "beta-message" not in _checkpoint_file(vault, "alpha").read_text(encoding="utf-8")


def test_resumed_session_can_write_a_new_full_wrapup_after_later_turn(tmp_path):
    mod = _load_lifecycle()
    controller, _home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)

    controller.full_wrapup(session_id="resumed", reason="first-exit")
    controller.after_turn(session_id="resumed", turn_id="later-turn", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd="")
    controller.full_wrapup(session_id="resumed", reason="second-exit")

    checkpoint = _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    assert checkpoint.count("hermes-lifecycle full-wrapup checkpoint") == 2
    assert "reason: second-exit" in checkpoint


def test_partial_checkpoint_record_is_not_accepted_for_recovery(tmp_path):
    mod = _load_lifecycle()
    controller, _home, vault = _controller(mod, tmp_path)
    event_id = "partial-event"
    path = vault / "jarvis" / "daily" / "2026-10-16-hermes-lifecycle.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"<!-- hermes-lifecycle incremental checkpoint -->\\n- event_id: {event_id}\\n", encoding="utf-8")

    assert controller._checkpoint_has(event_id, path.name) is False


def test_full_wrapup_drains_durable_pending_before_boundary(tmp_path):
    mod = _load_lifecycle()
    controller, home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    event_id = "admitted-before-exit"
    state_path = home / "state" / "fleet-session-lifecycle.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({"pending_turns": {event_id: {
        "event_id": event_id, "session_id": "closing", "turn_id": "interrupted",
        "completed": False, "interrupted": True, "user": "u", "response": "",
    }}}), encoding="utf-8")

    controller.full_wrapup(session_id="closing", reason="quit")

    checkpoint = _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert checkpoint.index(f"event_id: {event_id}") < checkpoint.index("full-wrapup checkpoint")
    assert event_id not in state["pending_turns"]


def test_completed_turn_is_durable_before_checkpoint_side_effect(tmp_path, monkeypatch):
    mod = _load_lifecycle()
    controller, home, _vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    controller.admit_turn(session_id="s", turn_id="t", user_message="question")
    monkeypatch.setattr(controller, "_append_checkpoint", lambda _event: (_ for _ in ()).throw(OSError("disk")))

    controller.after_turn(session_id="s", turn_id="t", completed=True, interrupted=False,
                          user_message="question", assistant_response="answer", conversation_history=[], cwd="")

    event_id = __import__("hashlib").sha256("s\0t".encode()).hexdigest()
    event = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())["pending_turns"][event_id]
    assert event["completed"] is True
    assert event["interrupted"] is False
    assert event["response"] == "answer"
    assert event["outbox_intent"] is True


def test_full_wrapup_retries_busy_lease_without_dropping_boundary(tmp_path, monkeypatch):
    mod = _load_lifecycle()
    controller, _home, vault = _controller(mod, tmp_path)
    original_lease = controller._lease
    attempts = 0

    from contextlib import contextmanager

    @contextmanager
    def busy_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield False
        else:
            with original_lease() as acquired:
                yield acquired

    monkeypatch.setattr(controller, "_lease", busy_once)
    controller.full_wrapup(session_id="closing", reason="quit")

    assert attempts == 2
    assert "full-wrapup checkpoint" in _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")


def test_replay_clears_finalization_marker_for_recovered_event_session(tmp_path):
    mod = _load_lifecycle()
    controller, home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    session_a = "recovered-a"
    final_a = __import__("hashlib").sha256(session_a.encode()).hexdigest()
    state_path = home / "state" / "fleet-session-lifecycle.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({
        "finalized_sessions": [final_a],
        "pending_turns": {"recovered-event": {
            "event_id": "recovered-event", "session_id": session_a, "turn_id": "old-turn",
            "completed": True, "interrupted": False, "user": "u", "response": "a",
        }},
    }), encoding="utf-8")

    controller.after_turn(session_id="callback-b", turn_id="new-turn", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd="")
    controller.full_wrapup(session_id=session_a, reason="resumed-exit")

    checkpoint = _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    assert "reason: resumed-exit" in checkpoint


def test_active_admission_is_not_drained_by_another_session(tmp_path):
    mod = _load_lifecycle()
    first, home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    second = mod.LifecycleController(first.home, {"vault_root": str(vault)}, runner=lambda *_a, **_k: 0)

    first.admit_turn(session_id="active-a", turn_id="turn-a", user_message="question")
    second.after_turn(session_id="finished-b", turn_id="turn-b", completed=True, interrupted=False,
                      user_message="other", assistant_response="answer", conversation_history=[], cwd="")

    active_id = __import__("hashlib").sha256(b"active-a\x00turn-a").hexdigest()
    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text(encoding="utf-8"))
    checkpoint = _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    assert active_id in state["pending_turns"]
    assert "turn_id: turn-a" not in checkpoint


def test_dash_style_secret_is_removed_before_checkpoint_and_outbox(tmp_path):
    mod = _load_lifecycle()
    controller, home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 1)
    controller.config["outbox_command"] = ["external-outbox-adapter"]
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz"

    controller.after_turn(session_id="s", turn_id="secret", completed=True, interrupted=False,
                          user_message=secret, assistant_response=secret, conversation_history=[], cwd="")

    assert secret not in _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    assert secret not in (home / "state" / "fleet-session-lifecycle-outbox.jsonl").read_text(encoding="utf-8")


def test_recovery_appends_pending_event_to_its_recorded_partition(monkeypatch, tmp_path):
    mod = _load_lifecycle()
    controller, home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    state_path = home / "state" / "fleet-session-lifecycle.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({"pending_turns": {"midnight": {
        "event_id": "midnight", "session_id": "old", "turn_id": "turn", "completed": True,
        "interrupted": False, "user": "u", "response": "a", "checkpoint_partition": "2026-10-16-hermes-lifecycle.md",
    }}}), encoding="utf-8")
    monkeypatch.setattr(mod.time, "strftime", lambda _format: "2026-10-17")

    controller.after_turn(session_id="new", turn_id="new", completed=True, interrupted=False,
                          user_message="u", assistant_response="a", conversation_history=[], cwd="")

    old_path = vault / "jarvis" / "daily" / "2026-10-16-hermes-lifecycle.md"
    today_path = vault / "jarvis" / "daily" / "2026-10-17-hermes-lifecycle.md"
    assert "event_id: midnight" in old_path.read_text(encoding="utf-8")
    assert "event_id: midnight" not in today_path.read_text(encoding="utf-8")


def test_admission_retries_a_transient_busy_lease(tmp_path, monkeypatch):
    mod = _load_lifecycle()
    controller, home, _vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    controller.config["finalize_lease_wait_seconds"] = 0.1
    original_lease = controller._lease
    attempts = 0
    from contextlib import contextmanager

    @contextmanager
    def busy_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield False
        else:
            with original_lease() as acquired:
                yield acquired

    monkeypatch.setattr(controller, "_lease", busy_once)
    controller.admit_turn(session_id="retry", turn_id="admit", user_message="u")

    assert attempts == 2
    assert json.loads((home / "state" / "fleet-session-lifecycle.json").read_text())["pending_turns"]


def test_full_wrapup_recovers_after_append_before_finalized_ledger(tmp_path, monkeypatch):
    mod = _load_lifecycle()
    controller, _home, vault = _controller(mod, tmp_path)
    original_append = controller._append_full_wrapup

    def append_then_crash(*args, **kwargs):
        original_append(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(controller, "_append_full_wrapup", append_then_crash)
    with pytest.raises(KeyboardInterrupt):
        controller.full_wrapup(session_id="closing", reason="quit")
    monkeypatch.setattr(controller, "_append_full_wrapup", original_append)

    controller.full_wrapup(session_id="closing", reason="quit")
    checkpoint = _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    assert checkpoint.count("hermes-lifecycle full-wrapup checkpoint") == 1


def test_atomic_json_fsyncs_parent_directory_after_replace(tmp_path, monkeypatch):
    mod = _load_lifecycle()
    path = tmp_path / "state" / "admission.json"
    syncs = []
    original_open = mod.os.open
    original_fsync = mod.os.fsync

    def tracking_open(target, flags, *args):
        fd = original_open(target, flags, *args)
        if Path(target) == path.parent:
            syncs.append(("opened-parent", fd))
        return fd

    def tracking_fsync(fd):
        syncs.append(("fsync", fd))
        return original_fsync(fd)

    monkeypatch.setattr(mod.os, "open", tracking_open)
    monkeypatch.setattr(mod.os, "fsync", tracking_fsync)

    mod._atomic_json(path, {"admitted": True})

    parent_fds = {fd for kind, fd in syncs if kind == "opened-parent"}
    assert parent_fds
    assert any(kind == "fsync" and fd in parent_fds for kind, fd in syncs)


def test_after_turn_busy_lease_persists_completion_intent_for_other_session_replay(tmp_path, monkeypatch):
    mod = _load_lifecycle()
    first, home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    second = mod.LifecycleController(first.home, {"vault_root": str(vault)}, runner=lambda *_a, **_k: 0)
    first.admit_turn(session_id="active", turn_id="turn", user_message="question")
    first.config["finalize_lease_wait_seconds"] = 0

    from contextlib import contextmanager

    @contextmanager
    def busy_lease():
        yield False

    monkeypatch.setattr(first, "_lease", busy_lease)
    first.after_turn(session_id="active", turn_id="turn", completed=True, interrupted=False,
                     user_message="question", assistant_response="answer", conversation_history=[], cwd="")

    event_id = __import__("hashlib").sha256(b"active\x00turn").hexdigest()
    assert (home / "state" / "fleet-session-lifecycle-completions" / f"{event_id}.json").exists()
    second.after_turn(session_id="other", turn_id="next", completed=True, interrupted=False,
                      user_message="other", assistant_response="ok", conversation_history=[], cwd="")
    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text(encoding="utf-8"))
    assert event_id not in state["pending_turns"]
    assert "turn_id: turn" in _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")


def test_disabled_session_end_cancels_durable_admissions(monkeypatch, tmp_path):
    plugin = _load_plugin()
    calls = []

    class Controller:
        def __init__(self, *_args, **_kwargs):
            pass

        def cancel_session(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(plugin, "LifecycleController", Controller)
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path / ".hermes")
    monkeypatch.setattr(plugin, "_config", lambda: {"enabled": False})

    plugin._on_session_end(session_id="disabled", turn_id="ignored")

    assert calls == [{"session_id": "disabled"}]


def test_owner_liveness_rejects_pid_reuse_with_mismatched_birth_time(tmp_path, monkeypatch):
    mod = _load_lifecycle()
    controller, _home, _vault = _controller(mod, tmp_path)

    class Process:
        def create_time(self):
            return 200.0

    monkeypatch.setattr(mod.psutil, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(mod.psutil, "Process", lambda _pid: Process())

    assert controller._owner_is_alive(123, 200.0) is True
    assert controller._owner_is_alive(123, 100.0) is False


def test_cancel_session_removes_durable_pending_admissions(tmp_path):
    mod = _load_lifecycle()
    controller, home, _vault = _controller(mod, tmp_path)
    controller.admit_turn(session_id="disabled", turn_id="turn", user_message="question")

    controller.cancel_session(session_id="disabled")

    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text(encoding="utf-8"))
    assert state["pending_turns"] == {}


def test_cancel_session_persists_busy_lease_cancellation_for_next_process(tmp_path):
    mod = _load_lifecycle()
    if mod.fcntl is None:
        pytest.skip("requires POSIX flock")
    controller, home, vault = _controller(mod, tmp_path)
    controller.config["finalize_lease_wait_seconds"] = 0
    controller.admit_turn(session_id="disabled", turn_id="turn", user_message="question")
    context = multiprocessing.get_context("forkserver")
    ready, release = context.Event(), context.Event()
    holder = context.Process(target=_hold_profile_lease, args=(str(home), str(vault), ready, release))
    holder.start()
    try:
        assert ready.wait(2)
        controller.cancel_session(session_id="disabled")
        intent = home / "state" / "fleet-session-lifecycle-cancellations" / (
            __import__("hashlib").sha256(b"disabled").hexdigest() + ".json"
        )
        assert intent.exists()
        release.set()
        holder.join(3)
        assert holder.exitcode == 0
        next_holder = mod.LifecycleController(home, {"vault_root": str(vault)}, runner=lambda *_a, **_k: 0)
        next_holder.after_turn(session_id="other", turn_id="next", completed=True, interrupted=False,
                               user_message="u", assistant_response="a", conversation_history=[], cwd="")
        state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text(encoding="utf-8"))
        assert state["pending_turns"] == {}
        assert not intent.exists()
    finally:
        release.set()
        holder.join(3)


def test_full_wrapup_persists_busy_lease_intent_for_next_process(tmp_path):
    mod = _load_lifecycle()
    if mod.fcntl is None:
        pytest.skip("requires POSIX flock")
    controller, home, vault = _controller(mod, tmp_path)
    controller.config["finalize_lease_wait_seconds"] = 0
    context = multiprocessing.get_context("forkserver")
    ready, release = context.Event(), context.Event()
    holder = context.Process(target=_hold_profile_lease, args=(str(home), str(vault), ready, release))
    holder.start()
    try:
        assert ready.wait(2)
        controller.full_wrapup(session_id="closing", reason="quit")
        final_id = __import__("hashlib").sha256(b"closing").hexdigest()
        intent = home / "state" / "fleet-session-lifecycle-finalizations" / f"{final_id}.json"
        assert intent.exists()
        release.set()
        holder.join(3)
        assert holder.exitcode == 0
        next_holder = mod.LifecycleController(home, {"vault_root": str(vault)}, runner=lambda *_a, **_k: 0)
        next_holder.after_turn(session_id="other", turn_id="next", completed=True, interrupted=False,
                               user_message="u", assistant_response="a", conversation_history=[], cwd="")
        assert "session_id: closing" in _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
        assert not intent.exists()
    finally:
        release.set()
        holder.join(3)
