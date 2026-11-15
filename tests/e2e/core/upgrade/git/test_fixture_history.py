"""The upgrade-history fixture crosses a real N-1 -> HEAD boundary.

``build_upgrade_history`` once gave every commit HEAD's tree: no N-1 code, HEAD's installer standing in
for N-1's, and a one-commit ``base..target`` range that put every mid-range stop on the same commit.
Pure git, no sandbox and no installer: these run wherever the checkout has the history the fixture reads.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.git import _git_world as G

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


def _checkout(root: Path, installers: list[str]) -> Path:
    """A throwaway checkout, one first-parent commit per entry, each carrying that ``scripts/install.sh``."""
    repo = root / "checkout"
    (repo / "scripts").mkdir(parents=True)
    env = G._fixture_git_env("2026-02-01T00:00:00+0000")

    def git(*args: str) -> None:
        cp = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=env)
        assert cp.returncode == 0, f"git {args} failed: {cp.stderr}"

    git("init", "-q")
    for text in installers:
        (repo / "scripts" / "install.sh").write_text(text, encoding="utf-8")
        git("add", "scripts/install.sh")
        git("commit", "-q", "--allow-empty", "-m", f"installer {text.strip()}")
    return repo


def _tree(ref: str, cwd: Path) -> str:
    return I.git("rev-parse", f"{ref}^{{tree}}", cwd=cwd)


def _is_ancestor(ancestor: str, descendant: str, cwd: Path) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=str(cwd)).returncode == 0


def test_n1_snapshot_is_older_content_with_its_own_installer(tmp_path, monkeypatch):
    repo = _checkout(tmp_path, [f"# installer {i}\n" for i in range(G.N1_COMMITS_BEHIND + 2)])
    monkeypatch.setattr(G.H, "WORKTREE", repo)
    history = G.build_upgrade_history(tmp_path / "history")

    assert _tree(history.target, history.source) == _tree("HEAD", repo)
    assert _tree(history.base, history.source) == _tree(f"HEAD~{G.N1_COMMITS_BEHIND}", repo)
    assert _tree(history.untagged, history.source) == _tree(f"HEAD~{G.N1_COMMITS_BEHIND + 1}", repo)
    assert _tree(history.base, history.source) != _tree(history.target, history.source)
    # N-1 installs with N-1's installer, not the worktree's.
    assert G.installer_script(history.tag, history.source) == "# installer 1\n"
    assert G.installer_script(None, history.source) == f"# installer {G.N1_COMMITS_BEHIND + 1}\n"
    between = I.git("rev-list", "--first-parent", f"{history.base}..{history.target}", cwd=history.source).split()
    assert len(between) == G.N1_COMMITS_BEHIND


def test_mid_range_stops_are_three_distinct_commits_between_n1_and_head(tmp_path, monkeypatch):
    repo = _checkout(tmp_path, [f"# installer {i}\n" for i in range(G.N1_COMMITS_BEHIND + 2)])
    monkeypatch.setattr(G.H, "WORKTREE", repo)
    history = G.build_upgrade_history(tmp_path / "history")

    stops = G.mid_range_stops(history)
    assert len(set(stops)) == 3
    assert history.base not in stops and history.target not in stops
    # Upstream moves forward: each stop is a later commit than the one before it, all between N-1 and HEAD.
    oldest, middle, newest = stops
    assert _is_ancestor(history.base, oldest, history.source)
    assert _is_ancestor(oldest, middle, history.source)
    assert _is_ancestor(middle, newest, history.source)
    assert _is_ancestor(newest, history.target, history.source)


def test_a_range_too_short_for_three_stops_fails_instead_of_repeating_one(tmp_path, monkeypatch):
    repo = _checkout(tmp_path, [f"# installer {i}\n" for i in range(4)])
    monkeypatch.setattr(G.H, "WORKTREE", repo)
    history = G.build_upgrade_history(tmp_path / "history", behind=2)

    with pytest.raises(AssertionError, match="three distinct"):
        G.mid_range_stops(history)


def test_a_checkout_without_the_history_fails_loudly(tmp_path, monkeypatch):
    repo = _checkout(tmp_path, [f"# installer {i}\n" for i in range(3)])
    monkeypatch.setattr(G.H, "WORKTREE", repo)

    with pytest.raises(AssertionError, match="fetch-depth: 0"):
        G.build_upgrade_history(tmp_path / "history")


def test_n1_and_head_with_the_same_tree_fail_instead_of_crossing_no_code(tmp_path, monkeypatch):
    repo = _checkout(tmp_path, ["# installer\n"] * (G.N1_COMMITS_BEHIND + 2))
    monkeypatch.setattr(G.H, "WORKTREE", repo)

    with pytest.raises(AssertionError, match="same tree"):
        G.build_upgrade_history(tmp_path / "history")


def test_release_tag_sits_on_n1_and_the_root_before_it_has_none(tmp_path, monkeypatch):
    repo = _checkout(tmp_path, [f"# installer {i}\n" for i in range(G.N1_COMMITS_BEHIND + 2)])
    monkeypatch.setattr(G.H, "WORKTREE", repo)
    history = G.build_upgrade_history(tmp_path / "history")
    match = ("--match", "v20[0-9][0-9].*")

    assert I.git("describe", "--tags", "--abbrev=0", *match, f"{history.target}~1", cwd=history.source) == history.tag
    # The tag sits on N-1 itself, so the release is reachable from every commit after it and from none before.
    assert I.git("describe", "--tags", "--abbrev=0", *match, history.base, cwd=history.source) == history.tag
    described = subprocess.run(["git", "describe", "--tags", "--abbrev=0", *match, history.untagged],
                               cwd=str(history.source), capture_output=True, text=True)
    assert described.returncode != 0, f"the root before N-1 must not describe a release: {described.stdout!r}"


def test_this_checkouts_own_history_builds_a_real_boundary(tmp_path):
    history = G.build_upgrade_history(tmp_path / "history")

    assert _tree(history.target, history.source) == _tree("HEAD", G.H.WORKTREE)
    assert _tree(history.base, history.source) == _tree(f"HEAD~{G.N1_COMMITS_BEHIND}", G.H.WORKTREE)
    assert _tree(history.base, history.source) != _tree(history.target, history.source)
    assert len(set(G.mid_range_stops(history))) == 3
