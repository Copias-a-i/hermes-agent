import contextlib
import threading
import types

from tui_gateway import server


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
