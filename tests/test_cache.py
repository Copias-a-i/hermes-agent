"""COP-752 deterministic local cache and receipt tests."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


def test_cache_reuses_safe_partition_without_storing_session_id(tmp_path: Path) -> None:
    from fleet_session_bootstrap.cache import CacheStore

    cache = CacheStore(tmp_path / "cache")
    key = "shared-safe-partition"
    cache.put(key, {"availability": "available", "items": ["pointer"]})

    assert cache.get(key) == {"availability": "available", "items": ["pointer"]}
    entry = cache.path_for(key)
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    assert "session" not in entry.read_text(encoding="utf-8")


def test_cache_lock_is_private_and_available_to_later_readers(tmp_path: Path) -> None:
    from fleet_session_bootstrap.cache import CacheStore

    cache = CacheStore(tmp_path / "cache")
    with cache.lock_for("shared-safe-partition"):
        lock = cache.lock_path_for("shared-safe-partition")
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_receipt_is_atomic_private_and_redacted(tmp_path: Path) -> None:
    from fleet_session_bootstrap.cache import ReceiptStore

    store = ReceiptStore(tmp_path / "receipts")
    receipt = store.write(
        "attempt-opaque",
        {
            "availability": "unavailable",
            "namespace_digest": "abc123",
            "elapsed_ms": 4,
            "count": 0,
            "profile": "jarvis",
            "email": "person@example.com",
        },
    )

    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    data = json.loads(receipt.read_text(encoding="utf-8"))
    assert data == {
        "availability": "unavailable",
        "count": 0,
        "elapsed_ms": 4,
        "namespace_digest": "abc123",
    }
