import contextlib
import threading
import types

from tui_gateway import server


def test_automatic_desktop_finalize_skips_all_effects_when_another_runtime_owns_lease(monkeypatch):
    """A reaper that lost its lease is a viewer: it cannot finalize the replacement's work."""
    effects = []

    @contextlib.contextmanager
    def other_runtime_owns(_session_id, _session):
        effects.append("guard")
        yield True

    class Agent:
        session_id = "durable-session"
        _session_messages = [{"role": "assistant", "content": "in-flight"}]

        def _persist_session(self, _snapshot):
            effects.append("persist")

        def commit_memory_session(self, _history):
            effects.append("memory")

    monkeypatch.setattr(server, "_other_runtime_lease_guard", other_runtime_owns)
    monkeypatch.setattr(server, "_lock_vault_managers", lambda _session: effects.append("vault-lock"))
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args: effects.append("finalize-hook"))
    monkeypatch.setattr(server, "_release_active_session_slot", lambda _session: effects.append("release"))

    session = {
        "agent": Agent(),
        "session_key": "durable-session",
        "source": "desktop",
        "history": [{"role": "user", "content": "question"}],
        "history_lock": threading.Lock(),
    }

    server._finalize_session(session, end_reason="idle_timeout")

    assert effects == ["guard"]
    assert "_finalized" not in session


def test_session_close_finalizes_within_its_profile_runtime_scope(monkeypatch):
    calls = {"hooks": []}
    scope_depth = []

    @contextlib.contextmanager
    def scoped(_session):
        scope_depth.append(True)
        try:
            yield
        finally:
            scope_depth.pop()

    monkeypatch.setattr(server, "_session_profile_runtime_scope", scoped)
    agent = types.SimpleNamespace(session_id="session-key")
    agent.commit_memory_session = lambda history: calls.setdefault("history", history)
    server._sessions["scope-finalize"] = {
        "agent": agent,
        "session_key": "session-key",
        "history": [{"role": "user", "content": "hello"}],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
    }

    def notify(event, session_id, *_args):
        assert scope_depth, "finalize hook must run in its owning profile scope"
        calls["hooks"].append((event, session_id))

    monkeypatch.setattr(server, "_notify_session_boundary", notify)
    try:
        response = server.handle_request(
            {"id": "scope", "method": "session.close", "params": {"session_id": "scope-finalize"}}
        )
        assert response["result"]["closed"] is True
        assert calls["history"] == [{"role": "user", "content": "hello"}]
        assert ("on_session_finalize", "session-key") in calls["hooks"]
    finally:
        server._sessions.pop("scope-finalize", None)


def test_teardown_that_preserves_a_session_another_runtime_owns_never_ends_its_durable_row(tmp_path, monkeypatch):
    """The reaper that lost its lease still closes its agent, and ``agent.close()`` ends the row unless spared."""
    from tests.tui_gateway.test_gateway_owned_session_reap import _make_real_session, _read_real_row

    db_path, session, agent = _make_real_session(
        tmp_path, monkeypatch, source="desktop", session_id="replacement-owned",
    )

    @contextlib.contextmanager
    def other_runtime_owns(_session_id, _session):
        yield True

    monkeypatch.setattr(server, "_other_runtime_lease_guard", other_runtime_owns)

    server._teardown_session(session, end_reason="idle_timeout")

    row = _read_real_row(db_path, "replacement-owned")
    assert (row["ended_at"], row["end_reason"]) == (None, None)
    assert agent._end_session_on_close is False
    assert agent._owns_session_db is False  # close() still ran: only the durable row was spared
