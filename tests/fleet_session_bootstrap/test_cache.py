"""COP-752 deterministic local cache and receipt tests."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

def test_cache_reuses_safe_partition_without_storing_session_id(tmp_path: Path) -> None:
    from fleet_session_bootstrap.cache import CacheStore

    cache = CacheStore(tmp_path / "cache")
    key = "a" * 64
    cache.put(key, {"availability": "available", "count": 1, "items": ["SOUL:pointer"]})

    assert cache.get(key) == {"availability": "available", "count": 1, "items": ["SOUL:pointer"]}
    entry = cache.path_for(key)
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    assert "session" not in entry.read_text(encoding="utf-8")


def test_cache_lock_is_private_and_available_to_later_readers(tmp_path: Path) -> None:
    from fleet_session_bootstrap.cache import CacheStore

    cache = CacheStore(tmp_path / "cache")
    with cache.lock_for("b" * 64):
        lock = cache.lock_path_for("b" * 64)
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_receipt_is_atomic_private_and_redacted(tmp_path: Path) -> None:
    from fleet_session_bootstrap.cache import ReceiptStore

    store = ReceiptStore(tmp_path / "receipts")
    receipt = store.write(
        "attempt-opaque",
        {
            "availability": "unavailable",
            "namespace_digest": "a" * 64,
            "elapsed_ms": 4,
            "count": 0,
            "cache_state": "miss",
            "manifest_version": "v1",
            "timestamp": 1,
            "error": "none",
        },
    )

    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    data = json.loads(receipt.read_text(encoding="utf-8"))
    assert data["availability"] == "unavailable"
    assert data["count"] == 0
    assert data["elapsed_ms"] == 4
    assert data["namespace_digest"] == "a" * 64
    assert data["schema"] == 1
    assert data["attempt_digest"] != "attempt-opaque"


def test_state_stores_reject_git_roots_and_intermediate_symlinks(tmp_path: Path) -> None:
    from fleet_session_bootstrap.cache import CacheStore, ReceiptStore

    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    with pytest.raises(ValueError, match="Git"):
        CacheStore(repository)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ReceiptStore(tmp_path / "link" / "state")


def test_cache_marks_stale_envelopes_as_misses_and_replaces_them(tmp_path: Path) -> None:
    from fleet_session_bootstrap.cache import CacheStore

    cache = CacheStore(tmp_path / "state")
    key = "a" * 64
    manifest = {"schema": 1, "release_hash": "b" * 64, "cache_version": "v1", "readers": ["graphify", "pinecone"]}
    cache.put(key, {"availability": "available", "count": 1, "items": ["SOUL:pointer"]}, partition_identity="c" * 64, manifest=manifest, source_version="v1", now=100)
    assert cache.get(key, partition_identity="c" * 64, manifest=manifest, source_version="v1", now=101, ttl_seconds=5) == {"availability": "available", "count": 1, "items": ["SOUL:pointer"]}
    assert cache.get(key, partition_identity="c" * 64, manifest=manifest, source_version="v2", now=101, ttl_seconds=5) is None
    cache.put(key, {"availability": "unavailable", "count": 0, "items": []}, partition_identity="c" * 64, manifest=manifest, source_version="v2", now=102)
    assert cache.get(key, partition_identity="c" * 64, manifest=manifest, source_version="v2", now=103, ttl_seconds=5) == {"availability": "unavailable", "count": 0, "items": []}


def test_receipts_use_session_digest_for_idempotency_and_separation(tmp_path: Path) -> None:
    from fleet_session_bootstrap.cache import ReceiptStore

    store = ReceiptStore(tmp_path / "state")
    payload = {"cache_state": "miss", "manifest_version": "v1", "availability": "unavailable", "count": 0, "elapsed_ms": 1, "error": "none", "timestamp": 1, "namespace_digest": "a" * 64}
    first = store.write("session-a", payload)
    assert first == store.write("session-a", payload)
    second = store.write("session-b", payload)
    assert first != second
    first_data = json.loads(first.read_text(encoding="utf-8"))
    assert first_data["attempt_digest"] != "session-a"
    assert first_data["cache_state"] == "miss"


def test_multiprocess_readers_never_observe_partial_cache_json(tmp_path: Path) -> None:
    state = tmp_path / "state"
    key = "a" * 64
    manifest = {"schema": 1, "release_hash": "b" * 64, "cache_version": "v1", "readers": ["graphify", "pinecone"]}
    script = """
from fleet_session_bootstrap.cache import CacheStore
from pathlib import Path
import json, sys
state, key, manifest, role = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3]), sys.argv[4]
cache = CacheStore(state)
for index in range(100):
    if role == 'writer':
        cache.put(key, {'availability': 'available', 'count': index, 'items': ['SOUL:pointer']}, partition_identity='c'*64, manifest=manifest, source_version='v1')
    else:
        value = cache.get(key, partition_identity='c'*64, manifest=manifest, source_version='v1')
        if value is not None and not isinstance(value.get('count'), int): raise SystemExit(7)
"""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    args = [sys.executable, "-c", script, str(state), key, json.dumps(manifest)]
    processes = [subprocess.Popen([*args, "writer"], env=environment), *[subprocess.Popen([*args, "reader"], env=environment) for _ in range(3)]]
    assert [process.wait(timeout=30) for process in processes] == [0, 0, 0, 0]


def test_cache_rejects_unbounded_or_session_bearing_shared_payload(tmp_path: Path) -> None:
    from fleet_session_bootstrap.cache import CacheStore

    cache = CacheStore(tmp_path / "state")
    manifest = {"schema": 1, "release_hash": "b" * 64, "cache_version": "v1", "readers": ["graphify", "pinecone"]}
    kwargs = {"partition_identity": "c" * 64, "manifest": manifest, "source_version": "v1", "now": 1}
    for payload in (
        {"availability": "available", "items": ["session-id:private"]},
        {"availability": "available", "items": ["SOUL:" + "x" * 65]},
        {"availability": "available", "items": ["SOUL:ok"], "objective": "private"},
        {"availability": "available", "items": ["SOUL:ok"], "count": True},
    ):
        with pytest.raises(ValueError):
            cache.put("a" * 64, payload, **kwargs)


def test_receipt_requires_exact_redacted_schema_and_bounded_values(tmp_path: Path) -> None:
    from fleet_session_bootstrap.cache import ReceiptStore

    store = ReceiptStore(tmp_path / "state")
    valid = {"cache_state": "miss", "manifest_version": "v1", "availability": "unavailable", "count": 0, "elapsed_ms": 1, "timestamp": 1, "error": "none", "namespace_digest": "a" * 64}
    assert store.write("session", valid).exists()
    for bad in ({**valid, "error": "person@example.com"}, {**valid, "count": True}, {key: value for key, value in valid.items() if key != "timestamp"}, {**valid, "extra": "no"}):
        with pytest.raises(ValueError):
            store.write("session", bad)
