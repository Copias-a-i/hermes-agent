"""Lifecycle-state profile isolation contracts."""

from pathlib import Path

import pytest

from hermes_cli import profiles
from hermes_cli.profiles import create_profile


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def test_new_profile_seeds_lifecycle_marker(profile_env):
    profile_dir = create_profile("lifecycle-child", no_alias=True)
    marker = profile_dir / "state" / "fleet-session-lifecycle-v1"
    assert marker.read_text(encoding="utf-8") == "fleet-session-lifecycle.v1\n"


def test_lifecycle_marker_backfill_is_idempotent(profile_env):
    default_home = profile_env / ".hermes"
    legacy = create_profile("lifecycle-legacy", no_alias=True)
    for home in (default_home, legacy):
        (home / "state" / "fleet-session-lifecycle-v1").unlink(missing_ok=True)

    changed = profiles.backfill_fleet_session_lifecycles()
    assert {path.name for path in changed} == {".hermes", "lifecycle-legacy"}
    assert profiles.backfill_fleet_session_lifecycles() == []


def test_clone_all_excludes_profile_runtime_state(profile_env):
    source = create_profile("state-source", no_alias=True)
    (source / "state").mkdir(exist_ok=True)
    (source / "state" / "fleet-session-lifecycle.json").write_text('{"processed_turns": ["source"]}', encoding="utf-8")

    clone = create_profile("state-clone", clone_all=True, no_alias=True)
    assert not (clone / "state" / "fleet-session-lifecycle.json").exists()
