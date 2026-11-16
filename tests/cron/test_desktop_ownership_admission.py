"""A gateway can start after Desktop enumerates stores but before its tick lock."""
import threading


def test_gateway_start_during_lock_acquisition_yields_without_store_writes(tmp_path, monkeypatch):
    from cron import jobs, scheduler, scheduler_ownership
    from cron.scheduler_provider import InProcessCronScheduler

    stopped = threading.Event()
    owner = {"running": False}
    calls = {"gates": 0, "released": 0, "heartbeats": [], "drains": []}

    def gate(_name, _home):
        calls["gates"] += 1
        return not owner["running"]

    def acquire(_path):
        # Startup evidence predates this race; assert no writes AFTER ownership changes.
        calls["heartbeats"].clear()
        owner["running"] = True
        stopped.set()
        return object()

    monkeypatch.setattr(InProcessCronScheduler, "recover_interrupted", lambda self: None)
    monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)
    monkeypatch.setattr(scheduler, "_get_lock_paths", lambda: (tmp_path, tmp_path / ".tick.lock"))
    monkeypatch.setattr(scheduler, "_ensure_cron_dir", lambda path: None)
    monkeypatch.setattr(scheduler, "_acquire_tick_lock", acquire)
    monkeypatch.setattr(scheduler, "_release_tick_lock", lambda fd: calls.__setitem__("released", calls["released"] + 1))
    monkeypatch.setattr(jobs, "record_ticker_heartbeat", lambda **kw: calls["heartbeats"].append(kw))
    monkeypatch.setattr(scheduler_ownership, "register_ticked_homes", lambda homes: None)
    from cron import bot_chat_delivery
    monkeypatch.setattr(bot_chat_delivery, "drain_in_background", lambda: calls["drains"].append(True))

    InProcessCronScheduler()._start_multiplex(
        stopped, profile_homes=[("default", tmp_path)], interval=0, profile_gate=gate,
    )
    assert calls["gates"] >= 2
    assert calls["released"] == 1
    assert calls["drains"] == []
    assert calls["heartbeats"] == []


def test_gated_store_has_no_startup_heartbeat_or_recovery(tmp_path, monkeypatch):
    from cron import jobs
    from cron.scheduler_provider import InProcessCronScheduler

    calls = []
    monkeypatch.setattr(InProcessCronScheduler, "recover_interrupted", lambda self: calls.append("recovery"))
    monkeypatch.setattr(jobs, "record_ticker_heartbeat", lambda **kw: calls.append("heartbeat"))
    stopped = threading.Event()
    stopped.set()
    InProcessCronScheduler()._start_multiplex(
        stopped, profile_homes=[("default", tmp_path)], profile_gate=lambda name, home: False,
    )
    assert calls == []


def test_failing_ownership_recheck_inside_the_lock_yields_without_store_writes(tmp_path, monkeypatch):
    """A probe that raises means ownership is unknown: Desktop must yield, not record an error."""
    from cron import jobs, scheduler, scheduler_ownership
    from cron.scheduler_provider import InProcessCronScheduler

    stopped = threading.Event()
    calls = {"gates": 0, "writes": [], "drains": []}

    def gate(_name, _home):
        calls["gates"] += 1
        if calls["gates"] > 2:  # startup gate, cycle gate, then the recheck inside the lock
            stopped.set()
            raise OSError("gateway status probe failed")
        return True

    def acquire(_path):
        calls["writes"].clear()  # startup evidence predates the probe failure
        return object()

    monkeypatch.setattr(InProcessCronScheduler, "recover_interrupted", lambda self: None)
    monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)
    monkeypatch.setattr(scheduler, "_get_lock_paths", lambda: (tmp_path, tmp_path / ".tick.lock"))
    monkeypatch.setattr(scheduler, "_ensure_cron_dir", lambda path: None)
    monkeypatch.setattr(scheduler, "_acquire_tick_lock", acquire)
    monkeypatch.setattr(scheduler, "_release_tick_lock", lambda fd: None)
    monkeypatch.setattr(jobs, "record_ticker_heartbeat", lambda **kw: calls["writes"].append(("heartbeat", kw)))
    monkeypatch.setattr(jobs, "record_ticker_error", lambda message: calls["writes"].append(("error", message)))
    monkeypatch.setattr(scheduler_ownership, "register_ticked_homes", lambda homes: None)
    from cron import bot_chat_delivery
    monkeypatch.setattr(bot_chat_delivery, "drain_in_background", lambda: calls["drains"].append(True))

    InProcessCronScheduler()._start_multiplex(
        stopped, profile_homes=[("default", tmp_path)], interval=0, profile_gate=gate,
    )
    assert calls["gates"] >= 3
    assert calls["drains"] == []
    assert calls["writes"] == []
