"""A worker handoff wins over a late failure observed for the same run.

``kanban_complete``, ``kanban_request_review``, ``kanban_request_changes`` and
``kanban_block`` end their run with a terminal outcome and park the card in the
state that outcome produces. A watchdog, reaper or budget fallback that sees the
worker exit afterwards must not book a retry, a ``timed_out`` event or a breaker
trip on top of it, whichever status the handoff left the card in. The card
status alone cannot tell: ``review`` is also where a crashed reviewer's card is
restored, and that failure must still count.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from agent.turn_finalizer import _record_kanban_budget_exhausted
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

FAILURE_EVENT_KINDS = {"timed_out", "crashed", "spawn_failed", "reclaimed", "gave_up"}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    db = kbc.connect()
    try:
        yield db
    finally:
        db.close()


def _claimed(conn, title: str, max_retries: int = 1) -> tuple[str, int]:
    # max_retries=1 makes any booked failure trip the breaker and block the card.
    task_id = kb.create_task(conn, title=title, assignee="builder", max_retries=max_retries)
    claimed = kb.claim_task(conn, task_id, claimer="builder:1")
    assert claimed is not None and claimed.current_run_id is not None
    return task_id, int(claimed.current_run_id)


def _review_requested(conn, max_retries: int = 1) -> tuple[str, str, str]:
    task_id, run_id = _claimed(conn, "implemented, awaiting review", max_retries)
    assert kb.request_review(conn, task_id, summary="handoff", reviewer="reviewer", expected_run_id=run_id)
    return task_id, "review", "review_requested"


def _blocked(conn) -> tuple[str, str, str]:
    task_id, run_id = _claimed(conn, "needs operator input")
    assert kb.block_task(conn, task_id, reason="needs input", expected_run_id=run_id)
    return task_id, "blocked", "blocked"


def _completed(conn) -> tuple[str, str, str]:
    task_id, run_id = _claimed(conn, "delivered")
    assert kb.complete_task(conn, task_id, result="delivered", expected_run_id=run_id)
    return task_id, "done", "completed"


def _changes_requested(conn) -> tuple[str, str, str]:
    task_id, run_id = _claimed(conn, "reviewed, changes requested")
    assert kb.request_review(conn, task_id, summary="handoff", reviewer="reviewer", expected_run_id=run_id)
    review = kb.claim_review_task(conn, task_id)
    assert review is not None
    ok, _implementer = kb.request_changes(conn, task_id, reason="needs fixes", expected_run_id=review.current_run_id)
    assert ok
    return task_id, "ready", "changes_requested"


HANDOFFS = pytest.mark.parametrize(
    "handoff", [_review_requested, _blocked, _completed, _changes_requested],
    ids=["review_requested", "blocked", "completed", "changes_requested"],
)
# The three ways a late failure reaches ``_record_task_failure`` without a run id:
# a spawn/budget fallback that still thinks it owns the claim, a reaper's
# counter-only accounting, and a watchdog that also closes the run.
LATE_FAILURES = pytest.mark.parametrize(
    "late_failure",
    [
        {"release_claim": True, "end_run": True},
        {"release_claim": False, "end_run": False},
        {"release_claim": False, "end_run": True},
    ],
    ids=["release_and_end_run", "counter_only", "end_run_only"],
)


def _assert_handoff_survived(conn, task_id: str, status: str, outcome: str) -> None:
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == status
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert not FAILURE_EVENT_KINDS & {event.kind for event in kb.list_events(conn, task_id)}
    assert kb.list_runs(conn, task_id)[-1].outcome == outcome


@HANDOFFS
@LATE_FAILURES
def test_late_failure_without_a_run_id_cannot_override_a_worker_handoff(conn, handoff, late_failure):
    task_id, status, outcome = handoff(conn)

    booked = kbd._record_task_failure(conn, task_id, "late worker exit", outcome="timed_out", **late_failure)

    assert booked is False
    _assert_handoff_survived(conn, task_id, status, outcome)


def test_budget_fallback_without_a_run_id_cannot_override_a_review_handoff(conn, monkeypatch):
    """A worker that requests review on its last iteration and then hits the budget."""
    task_id, status, outcome = _review_requested(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)

    _record_kanban_budget_exhausted(task_id, 60, 60, logging.getLogger(__name__))

    _assert_handoff_survived(conn, task_id, status, outcome)


def test_crashed_reviewer_restored_to_review_still_books_its_failure(conn):
    """Negative control: ``review`` alone is not a handoff, the newest run decides."""
    task_id, _status, _outcome = _review_requested(conn, max_retries=3)
    assert kb.claim_review_task(conn, task_id) is not None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 1, task_id))

    assert kb.release_stale_claims(conn) == 1

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "review"
    assert task.consecutive_failures == 1
    assert kb.list_runs(conn, task_id)[-1].outcome == "reclaimed"


def test_open_reviewer_run_failure_is_booked_after_an_earlier_handoff(conn):
    """Negative control: a live attempt is judged by its own open run, not the last handoff."""
    task_id, _status, _outcome = _review_requested(conn)
    review = kb.claim_review_task(conn, task_id)
    assert review is not None

    tripped = kbd._record_task_failure(
        conn, task_id, "reviewer cannot start", outcome="spawn_failed",
        release_claim=True, end_run=True, expected_run_id=review.current_run_id,
    )

    assert tripped is True
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert task.consecutive_failures == 1
