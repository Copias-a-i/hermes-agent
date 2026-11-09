import json
import os
import threading
from pathlib import Path

from tools.environments.spill_journal import RollingSpillJournal, migrate_legacy_spills, redact_record
from tools.terminal_tool_result import _redact_spill_file


def _records(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_unchanged_run_creates_zero_files(tmp_path):
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    journal = RollingSpillJournal(tmp_path, now=1_700_000_000)
    assert journal.close() is None
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before


def test_close_is_idempotent_and_uses_one_daily_shard(tmp_path):
    journal = RollingSpillJournal(tmp_path, now=1_700_000_000)
    journal.append("material change")
    closed = journal.close()
    assert closed is not None
    path, record_id = closed
    assert journal.close() is None
    shard = Path(path)
    assert len(list(tmp_path.glob("spill-*.jsonl"))) == 1
    assert _records(shard) == [_records(shard)[0]]
    assert _records(shard)[0]["id"] == record_id
    assert _records(shard)[0]["output"] == "material change"
    assert not list(tmp_path.glob(".spill-*.tmp"))


def test_redacting_one_record_preserves_other_records(tmp_path):
    first = RollingSpillJournal(tmp_path, now=1_700_000_000)
    first.append("first secret")
    first_closed = first.close()
    assert first_closed is not None
    path, first_id = first_closed
    second = RollingSpillJournal(tmp_path, now=1_700_000_000)
    second.append("second secret")
    second_closed = second.close()
    assert second_closed is not None
    _, second_id = second_closed

    assert redact_record(Path(path), first_id, lambda value: value.replace("secret", "[redacted]"))
    records = {record["id"]: record for record in _records(Path(path))}
    assert records[first_id]["output"] == "first [redacted]"
    assert records[second_id]["output"] == "second secret"
    assert not redact_record(Path(path), "missing", lambda value: value)


def test_terminal_result_keeps_record_identity_in_shared_journal(tmp_path):
    journal = RollingSpillJournal(tmp_path, now=1_700_000_000)
    journal.append("terminal output")
    closed = journal.close()
    assert closed is not None
    path, record_id = closed

    metadata = dict(_redact_spill_file(path, len("terminal output"), "printf terminal", record_id))
    assert metadata["full_output_path"] == path
    assert metadata["full_output_record_id"] == record_id
    assert record_id in metadata["truncation_note"]


def test_concurrent_runs_append_complete_records_to_one_shard(tmp_path):
    errors = []

    def append(i):
        try:
            journal = RollingSpillJournal(tmp_path, now=1_700_000_000)
            journal.append(f"changed-{i}")
            journal.close()
        except Exception as exc:  # pragma: no cover - assertion below shows it
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    shards = list(tmp_path.glob("spill-*.jsonl"))
    assert len(shards) == 1
    assert sorted((r["output"] for r in _records(shards[0])), key=lambda value: int(value.rsplit("-", 1)[1])) == [f"changed-{i}" for i in range(12)]


def test_legacy_migration_archives_evidence_before_removing_legacy_files(tmp_path):
    legacy = tmp_path / "out-1-2-dead.log"
    legacy.write_text("legacy evidence")
    record = migrate_legacy_spills(tmp_path)
    assert record is not None
    bundle = tmp_path / "archive" / record["bundle"]
    assert bundle.exists()
    assert not legacy.exists()
    assert migrate_legacy_spills(tmp_path) is None


def test_rotation_archives_immutable_index_before_removing_expired_shard(tmp_path):
    old = tmp_path / "spill-2020-01-01.jsonl"
    old.write_text('{"id":"old","output":"evidence"}\n')
    os.utime(old, (1, 1))
    journal = RollingSpillJournal(tmp_path, now=1_700_000_000)
    journal.append("current")
    journal.close()
    archives = list((tmp_path / "archive").glob("spill-2020-01-01-*.jsonl"))
    assert len(archives) == 1
    assert archives[0].read_text() == '{"id":"old","output":"evidence"}\n'
    index = _records(tmp_path / "archive" / "index.jsonl")
    assert index[-1]["source"] == "spill-2020-01-01.jsonl"
    assert not old.exists()
