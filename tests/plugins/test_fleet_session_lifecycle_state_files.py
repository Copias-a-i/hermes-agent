"""The lifecycle ledger and outbox are trusted only when they are this plugin's own files.

Candidate-executed tools can write the profile ``state/`` directory, so an alias planted at the
outbox path must not redirect a truncate or append, and a ledger that exists but cannot be read
must be preserved rather than replaced by an empty one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.plugins.test_fleet_session_lifecycle_plugin import _checkpoint_file, _controller, _load_lifecycle

_TORN = "precious line\nprecious torn tail without terminator"


def _finish_turn(controller, turn_id: str) -> None:
    controller.after_turn(session_id="s", turn_id=turn_id, completed=True, interrupted=False,
                          user_message="question", assistant_response="answer", conversation_history=[], cwd="")


def _outbox_controller(tmp_path):
    mod = _load_lifecycle()
    controller, home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 75)
    controller.config["outbox_command"] = ["external-outbox-adapter"]
    queue = home / "state" / "fleet-session-lifecycle-outbox.jsonl"
    queue.parent.mkdir(parents=True)
    return controller, home, vault, queue


@pytest.mark.platforms("posix")
def test_outbox_never_truncates_or_appends_through_a_symlink(tmp_path):
    controller, home, vault, queue = _outbox_controller(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text(_TORN, encoding="utf-8")
    queue.symlink_to(victim)

    _finish_turn(controller, "t1")

    assert victim.read_text(encoding="utf-8") == _TORN
    assert queue.is_symlink()
    assert "turn_id: t1" in _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    state = json.loads((home / "state" / "fleet-session-lifecycle.json").read_text(encoding="utf-8"))
    assert state["outbox"]["write_pending"] is True


@pytest.mark.platforms("posix")
def test_outbox_never_writes_through_a_hard_link(tmp_path):
    controller, _home, _vault, queue = _outbox_controller(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text(_TORN, encoding="utf-8")
    os.link(victim, queue)

    _finish_turn(controller, "t1")

    assert victim.read_text(encoding="utf-8") == _TORN


def test_outbox_still_repairs_and_appends_to_its_own_file(tmp_path):
    controller, _home, _vault, queue = _outbox_controller(tmp_path)
    queue.write_bytes(b'{"event_id": "old"}\n{"event_id": "to')

    _finish_turn(controller, "t1")

    lines = queue.read_bytes().splitlines()
    assert json.loads(lines[0]) == {"event_id": "old"}
    assert [json.loads(line)["schema"] for line in lines[1:]] == ["fleet-lifecycle-outbox.v1"]


@pytest.mark.parametrize("ledger", ['{"pending_turns": {', "[]", "\x00\x01 not json"])
def test_unreadable_ledger_is_preserved_and_the_turn_is_kept_as_an_intent(tmp_path, ledger):
    mod = _load_lifecycle()
    controller, home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    controller.state_path.parent.mkdir(parents=True)
    controller.state_path.write_text(ledger, encoding="utf-8")

    controller.admit_turn(session_id="s", turn_id="t1", user_message="question")
    _finish_turn(controller, "t1")
    controller.full_wrapup(session_id="s", reason="done")
    controller.cancel_session(session_id="other")

    assert controller.state_path.read_text(encoding="utf-8") == ledger
    state_dir = home / "state"
    assert list((state_dir / "fleet-session-lifecycle-admissions").glob("*.json"))
    assert list((state_dir / "fleet-session-lifecycle-completions").glob("*.json"))
    assert list((state_dir / "fleet-session-lifecycle-finalizations").glob("*.json"))
    assert list((state_dir / "fleet-session-lifecycle-cancellations").glob("*.json"))

    # Once an operator restores the ledger, the next holder adopts the retained turn.
    controller.state_path.unlink()
    _finish_turn(controller, "t2")
    rows = _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    assert "turn_id: t1" in rows and "turn_id: t2" in rows


def test_absent_ledger_still_starts_empty(tmp_path):
    mod = _load_lifecycle()
    controller, _home, vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)

    _finish_turn(controller, "t1")

    assert json.loads(controller.state_path.read_text(encoding="utf-8"))["processed_turns"]
    assert "turn_id: t1" in _checkpoint_file(vault, "jarvis").read_text(encoding="utf-8")
    assert Path(controller.state_path).is_file()


@pytest.mark.platforms("posix")
def test_adapter_reads_the_validated_queue_even_if_its_path_is_swapped(tmp_path):
    mod = _load_lifecycle()
    secret = tmp_path / "auth.json"
    secret.write_text('{"api_key": "must-not-export"}', encoding="utf-8")
    seen = {}

    def adapter(argv, **kwargs):
        queue = Path(controller.home) / "state" / "fleet-session-lifecycle-outbox.jsonl"
        queue.unlink()
        queue.symlink_to(secret)  # planted between validation and the adapter's read
        seen["argv"] = argv
        seen["input"] = os.read(kwargs["stdin"], 1 << 16)
        return 75

    controller, _home, _vault = _controller(mod, tmp_path, runner=adapter)
    controller.config["outbox_command"] = ["external-outbox-adapter"]

    _finish_turn(controller, "t1")

    assert seen["argv"][-2:] == ["--input", "/dev/stdin"]
    assert b"must-not-export" not in seen["input"]
    assert json.loads(seen["input"].splitlines()[-1])["schema"] == "fleet-lifecycle-outbox.v1"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_checkpoints_never_append_through_an_alias_at_the_partition(tmp_path, alias):
    mod = _load_lifecycle()
    controller, _home, _vault = _controller(mod, tmp_path, runner=lambda *_a, **_k: 0)
    partition = controller._checkpoint_path()
    partition.parent.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious contents", encoding="utf-8")
    if alias == "symlink":
        partition.symlink_to(victim)
    else:
        os.link(victim, partition)

    _finish_turn(controller, "t1")
    controller.full_wrapup(session_id="s", reason="done")

    assert victim.read_text(encoding="utf-8") == "precious contents"
    state = json.loads(controller.state_path.read_text(encoding="utf-8"))
    assert state["pending_turns"]  # the turn stays durable for a later, unaliased checkpoint
