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


def test_new_profile_does_not_write_obsolete_lifecycle_marker(profile_env):
    profile_dir = create_profile("lifecycle-child", no_alias=True)
    assert not (profile_dir / "state" / "fleet-session-lifecycle-v1").exists()


def test_clone_all_excludes_profile_runtime_state(profile_env):
    source = create_profile("state-source", no_alias=True)
    (source / "state").mkdir(exist_ok=True)
    (source / "state" / "fleet-session-lifecycle.json").write_text('{"processed_turns": ["source"]}', encoding="utf-8")

    clone = create_profile("state-clone", clone_all=True, clone_from="state-source", no_alias=True)
    assert not (clone / "state" / "fleet-session-lifecycle.json").exists()
