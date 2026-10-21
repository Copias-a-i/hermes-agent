"""Release-tag picker behavior for shallow install/update E2E checkouts."""
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PICKER = ROOT / "scripts" / "sandbox" / "pick-release-tags.sh"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _pick(repo: Path, count: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(PICKER), "--repo", str(repo), "--count", str(count)], capture_output=True, text=True)


def test_picker_reports_typed_no_candidates_for_a_checkout_without_release_tags(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.invalid")
    _git(tmp_path, "commit", "--allow-empty", "-qm", "fixture")

    picked = _pick(tmp_path)

    assert picked.returncode == 3
    assert picked.stdout == ""
    assert "no release tags found" in picked.stderr
    assert "fetch-depth: 0" in picked.stderr


def test_picker_errors_do_not_use_the_no_candidates_exit_code(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")

    picked = _pick(tmp_path, count=0)

    assert picked.returncode == 1
    assert "--count must be at least 1" in picked.stderr


def test_picker_selects_latest_valid_release_after_tags_are_fetched_into_shallow_clone(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    for tag in ("v2026.6.9", "backup/before-release", "v2026.6.10"):
        _git(source, "commit", "--allow-empty", "-qm", tag)
        _git(source, "tag", tag)

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True, capture_output=True, text=True)
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--depth=1", "--no-tags", f"file://{remote}", str(shallow)], check=True, capture_output=True, text=True)
    assert _git(shallow, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    assert _pick(shallow).returncode != 0

    _git(shallow, "fetch", "--depth=1", "--tags", "origin")
    picked = _pick(shallow)

    assert picked.returncode == 0, picked.stderr
    assert picked.stdout == '["v2026.6.10"]\n'
