import json
import os
import threading
from pathlib import Path

import pytest

from tools.environments import spill_journal
from tools.environments.spill_journal import (
    RollingSpillJournal, migrate_legacy_spills, reconstruct_record, redact_record,
)



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
    journal.append("terminal output sk-proj-" + "a1B2c3D4e5F6g7H8i9J0" * 3)
    closed = journal.close(lambda value: value.replace("a1B2c3D4e5F6g7H8i9J0" * 3, "[redacted]"))
    assert closed is not None
    path, record_id = closed
    shard = Path(path)
    assert "a1B2c3D4e5F6g7H8i9J0" not in shard.read_text()
    assert reconstruct_record(shard, record_id) == "terminal output sk-proj-[redacted]"


def test_redactor_exception_writes_no_raw_residue(tmp_path):
    journal = RollingSpillJournal(tmp_path, now=1_700_000_000)
    journal.append("raw-secret-marker")
    with pytest.raises(RuntimeError, match="redactor exploded"):
        journal.close(lambda _: (_ for _ in ()).throw(RuntimeError("redactor exploded")))
    assert not list(tmp_path.glob("spill-*.jsonl"))
    assert not any("raw-secret-marker" in path.read_text(errors="replace")
                   for path in tmp_path.rglob("*") if path.is_file())


def test_large_record_is_pageable_framed_and_reconstructible(tmp_path):
    output = "page-marker-" + "x" * 125_000
    journal = RollingSpillJournal(tmp_path, now=1_700_000_000)
    journal.append(output)
    closed = journal.close()
    assert closed is not None
    path, record_id = closed
    lines = Path(path).read_bytes().splitlines()
    assert len(lines) > 10
    assert max(map(len, lines)) < 40_000
    assert reconstruct_record(Path(path), record_id) == output


def test_rollover_uses_newest_numbered_shard(tmp_path, monkeypatch):
    monkeypatch.setattr(spill_journal, "_MAX_SHARD_BYTES", 500)
    first = RollingSpillJournal(tmp_path, now=1_700_000_000)
    first.append("a" * 80)
    first.close()
    second = RollingSpillJournal(tmp_path, now=1_700_000_000)
    second.append("b" * 80)
    second.close()
    third = RollingSpillJournal(tmp_path, now=1_700_000_000)
    third.append("c")
    path, _ = third.close() or (None, None)
    assert path is not None and path.endswith("-002.jsonl")


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_atomic_redaction_failure_preserves_old_shard(tmp_path, monkeypatch, failure):
    journal = RollingSpillJournal(tmp_path, now=1_700_000_000)
    journal.append("old secret")
    closed = journal.close()
    assert closed is not None
    path, record_id = closed
    before = Path(path).read_bytes()
    if failure == "write":
        monkeypatch.setattr(spill_journal, "_write_all", lambda *_: (_ for _ in ()).throw(OSError("ENOSPC")))
    else:
        monkeypatch.setattr(spill_journal.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("crash before rename")))
    assert not redact_record(Path(path), record_id, lambda value: value.replace("secret", "[redacted]"))
    assert Path(path).read_bytes() == before


def test_atomic_append_enospc_preserves_old_shard(tmp_path, monkeypatch):
    first = RollingSpillJournal(tmp_path, now=1_700_000_000)
    first.append("old record")
    closed = first.close()
    assert closed is not None
    path, _ = closed
    before = Path(path).read_bytes()
    monkeypatch.setattr(spill_journal, "_write_all", lambda *_: (_ for _ in ()).throw(OSError("ENOSPC")))
    second = RollingSpillJournal(tmp_path, now=1_700_000_000)
    second.append("new record")
    with pytest.raises(OSError, match="ENOSPC"):
        second.close()
    assert Path(path).read_bytes() == before


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


def test_legacy_migration_redacts_before_archiving_and_serializes_writers(tmp_path):
    legacy = tmp_path / "out-1-2-dead.log"
    legacy.write_text("legacy raw-secret-marker")
    errors = []

    def append(i):
        try:
            journal = RollingSpillJournal(tmp_path, now=1_700_000_000)
            journal.append(f"new-{i}")
            journal.close(lambda value: value.replace("raw-secret-marker", "[redacted]"))
        except Exception as exc:  # pragma: no cover - assertion below shows it
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert not legacy.exists()
    assert not any("raw-secret-marker" in path.read_text(errors="replace")
                   for path in tmp_path.rglob("*") if path.is_file())
    shard = next(tmp_path.glob("spill-*.jsonl"))
    assert sorted((record["output"] for record in _records(shard) if record["chunk"] == 0),
                  key=lambda value: int(value.rsplit("-", 1)[1])) == [f"new-{i}" for i in range(12)]


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
