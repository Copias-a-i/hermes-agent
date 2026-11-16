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


def test_picker_propagates_git_listing_failure_instead_of_no_candidates(tmp_path):
    # An empty .git directory makes `git tag --list` exit 128. This is a
    # broken checkout, not the successful-empty-list bootstrap condition.
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").mkdir()

    picked = _pick(broken)

    assert picked.returncode == 128
    assert "no release tags found" not in picked.stderr


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


def test_picker_orders_calendar_versions_by_numeric_month_and_day_components(tmp_path):
    # GNU sort reads `-k1.2n` (no end field) through the end of the line and
    # parses "2026.10" as the decimal 2026.10, so v2026.10.1 landed before
    # v2026.4.1 and "newest" resolved to April. Each component must compare as
    # its own integer.
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.invalid")
    tags = ("v2026.4.23.1", "v2026.12.5", "v2025.12.31", "v2026.10.1", "v2026.4.1", "v2026.9.30", "v2026.1.5", "v2026.4.23")
    for tag in tags:
        _git(tmp_path, "commit", "--allow-empty", "-qm", tag)
        _git(tmp_path, "tag", tag)

    every_tag = _pick(tmp_path, count=len(tags))
    newest_tag = _pick(tmp_path, count=1)

    assert every_tag.returncode == 0, every_tag.stderr
    assert every_tag.stdout == (
        '["v2025.12.31","v2026.1.5","v2026.4.1","v2026.4.23","v2026.4.23.1","v2026.9.30","v2026.10.1","v2026.12.5"]\n'
    )
    assert newest_tag.returncode == 0, newest_tag.stderr
    assert newest_tag.stdout == '["v2026.12.5"]\n'
