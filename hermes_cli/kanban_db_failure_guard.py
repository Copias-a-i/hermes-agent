"""Terminal-precedence guard for kanban failure accounting: a worker handoff beats a late failure observed for the same run.

Split out of ``hermes_cli.kanban_db_dispatch``; origin-resident helpers are
reached late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def late_failure_loses_to_handoff(conn: sqlite3.Connection, task_id: str, row: Any) -> bool:
    """True when a failure for ``task_id`` can only be a stale observation of a finished run.

    ``kanban_complete``, ``kanban_request_review``, ``kanban_request_changes`` and
    ``kanban_block`` end their run with a terminal outcome and park the card in the
    state that outcome produces. A watchdog, reaper or budget fallback that sees the
    worker exit afterwards must not book a retry, a ``timed_out`` event or a breaker
    trip on top of it. The card status alone cannot say so: ``review`` is also the
    phase a crashed reviewer's card is restored to, and that failure must count. So
    the handoff only wins while it is still the card's live state: no open run, the
    newest run ended in a handoff outcome, and the card still sits where that
    outcome left it. A reaper closes its own run with a failure outcome first, and
    ``expected_run_id`` fences a successor's open run, so neither is affected.
    ``row`` is the caller's ``status`` / ``current_run_id`` snapshot of the card.
    """
    if row["status"] == "done":
        return True
    if row["current_run_id"] is not None:
        return False
    run = conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    handoff_status = _kb._RUN_OUTCOME_TERMINAL_STATUS.get(run["outcome"]) if run else None
    return handoff_status is not None and _kb.goal_run_status(conn, task_id) == handoff_status


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db_dispatch`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
