"""A real install served over real smart HTTP, and the real ``hermes update`` against it.

``World`` is one sandboxed machine (``_install_helpers.new_sandbox``: bwrap, empty fake HOME, the
git wrapper that reports the official remote URL) whose ``~/.gitconfig`` rewrites the official
GitHub URL to a loopback ``git http-backend`` origin (``_smart_http``). The install itself comes
from a real ``scripts/install.sh``: the one at ``installer_ref`` for an N-1 install, HEAD's for a
HEAD install (a ``--filter=blob:none`` clone).

The fork carries no release tags, so N-1 is not "the newest tag before HEAD". The upgrade history is
synthetic (``build_upgrade_history``) but made of HEAD's own: one commit per first-parent commit of
the last ``N1_COMMITS_BEHIND`` commits, each carrying that commit's real tree. The tagged ``base`` is the N-1
snapshot, with its own installer and updater; ``target`` is HEAD's tree; ``base..target`` is
``N1_COMMITS_BEHIND`` commits wide, so a cell can stop upstream on distinct commits in between. No
installer in that window clones ``--depth 1``, and each finishes by unshallowing its checkout, so the
shapes users have come from two places: ``preclone`` seeds a manual full clone or a ``blob:none``
fallback clone that the installer then adopts, and ``depth1`` rebuilds the finished install into the
depth-1 clone a stale one keeps (``World.make_depth1``).

Everything a cell asserts is user-visible: the exit code and transcript of ``hermes update``, the
checkout's HEAD / branch / working tree / refs, the update receipt, and whether ``hermes
--version`` still answers. ``diag()`` renders all of it, plus the HTTP request log, for a failure
message that explains itself.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import pytest

from tests.e2e.core.upgrade import _helpers as H
from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.git._smart_http import GitHTTPServer, serve_bare

REPO = "hermes-agent.git"
SUCCESS = "Update complete"
TRACEBACK = I.TRACEBACK

# How far behind HEAD the N-1 snapshot sits, in first-parent commits. It is also the width of the
# ``base..target`` range, which the cells that stop upstream on commits in between rely on.
N1_COMMITS_BEHIND = 12

PYTESTMARK = [
    pytest.mark.platforms("linux"),
    # Every `hermes update` runs inside the bwrap sandbox against a throwaway install in tmp_path.
    pytest.mark.live_system_guard_bypass,
    pytest.mark.skipif(H.sandbox_required_reason() is not None, reason=str(H.sandbox_required_reason())),
    pytest.mark.skipif(shutil.which("git") is None, reason="git required"),
    pytest.mark.skipif(I.real_uv() is None, reason="uv required"),
]


def output(cp: subprocess.CompletedProcess) -> str:
    return (cp.stdout or "") + (cp.stderr or "")


def reported_success(cp: subprocess.CompletedProcess) -> bool:
    return cp.returncode == 0 or SUCCESS in output(cp)


@dataclass(frozen=True)
class UpgradeHistory:
    """A local release -> upgrade ancestry independent of checkout tags or git config."""

    source: Path
    untagged: str
    base: str
    target: str
    tag: str


def _fixture_git_env(date: str) -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Hermes E2E", "GIT_AUTHOR_EMAIL": "e2e@hermes.invalid",
            "GIT_COMMITTER_NAME": "Hermes E2E", "GIT_COMMITTER_EMAIL": "e2e@hermes.invalid",
            "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}


def _commit(source: Path, tree: str, message: str, date: str, parent: str | None = None) -> str:
    cp = subprocess.run(["git", "commit-tree", tree, *(["-p", parent] if parent else [])], cwd=str(source),
                        input=message + "\n", text=True, capture_output=True, env=_fixture_git_env(date))
    assert cp.returncode == 0, f"creating upgrade fixture commit failed: {cp.stderr}"
    return cp.stdout.strip()


def _first_parent_snapshots(count: int) -> list[tuple[str, str]]:
    """``(tree, commit)`` of HEAD and its ``count - 1`` first-parent ancestors, oldest first."""
    rows = I.git("log", "--first-parent", f"--max-count={count}", "--format=%T %H", "HEAD",
                 cwd=H.WORKTREE).splitlines()
    assert len(rows) == count, (
        f"the upgrade fixture reads the last {count} first-parent commits of HEAD's history and found "
        f"{len(rows)}: check out with the full history (actions/checkout fetch-depth: 0)")
    return [(row.split()[0], row.split()[1]) for row in reversed(rows)]


def build_upgrade_history(root: Path, *, behind: int = N1_COMMITS_BEHIND) -> UpgradeHistory:
    """A dated release -> upgrade ancestry made of HEAD's own recent history, with no checkout tags
    or global git config.

    One synthetic commit per first-parent commit of HEAD from ``behind + 1`` back to HEAD, each
    carrying that commit's real tree: ``untagged`` is the snapshot before N-1, ``base`` (tagged) is
    N-1 with its own installer and updater, ``target`` is HEAD, and ``base..target`` is ``behind``
    commits wide. Identities and dates are fixed, so the same history is built wherever it runs."""
    source = root / "history.git"
    root.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(["git", "init", "-q", "--bare", str(source)], capture_output=True, text=True,
                        env=_fixture_git_env("2026-01-01T00:00:00+0000"))
    assert cp.returncode == 0, f"creating upgrade fixture repository failed: {cp.stderr}"
    objects = I.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=H.WORKTREE)
    (source / "objects" / "info" / "alternates").write_text(f"{Path(objects) / 'objects'}\n", encoding="utf-8")
    snapshots = _first_parent_snapshots(behind + 2)
    assert snapshots[1][0] != snapshots[-1][0], (
        f"the N-1 snapshot {snapshots[1][1][:12]} (HEAD~{behind}) and HEAD carry the same tree: the upgrade "
        f"would cross no code")

    def day(index: int) -> str:
        return (dt.date(2026, 1, 1) + dt.timedelta(days=index)).isoformat()

    commits: list[str] = []
    for index, (tree, real) in enumerate(snapshots):
        role = "untagged fixture root" if index == 0 else "release fixture" if index == 1 else "post-release upgrade"
        commits.append(_commit(source, tree, f"e2e: {role} ({real[:12]})", f"{day(index)}T00:00:00+0000",
                               commits[-1] if commits else None))
    untagged, base, target = commits[0], commits[1], commits[-1]
    tag = "v2099.1.1"
    for name, ref, stamp in (("e2e-not-a-release", untagged, f"{day(0)}T00:00:01+0000"),
                             (tag, base, f"{day(1)}T00:00:01+0000")):
        cp = subprocess.run(["git", "tag", "-a", name, ref, "-m", name], cwd=str(source), capture_output=True,
                            text=True, env=_fixture_git_env(stamp))
        assert cp.returncode == 0, f"creating upgrade fixture tag {name} failed: {cp.stderr}"
    I.git("update-ref", "refs/heads/main", target, cwd=source)
    I.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=source)
    return UpgradeHistory(source=source, untagged=untagged, base=base, target=target, tag=tag)


def mid_range_stops(history: UpgradeHistory) -> list[str]:
    """Where a cell stops upstream between N-1 and HEAD, oldest first: the commits a quarter, half and
    three quarters of the way from N-1 to HEAD, so each stop is a later move than the last and a
    passive check at each one grafts a different tip. A range too short for three distinct commits
    fails here rather than repeating one."""
    between = I.git("rev-list", "--first-parent", f"{history.base}..{history.target}", cwd=history.source).split()
    assert len(between) >= 3, (
        f"{len(between)} commit(s) between N-1 and HEAD cannot hold three distinct mid-range stops")
    return [between[k] for k in (len(between) * 3 // 4, len(between) // 2, len(between) // 4)]


def installer_script(ref: str | None, source: Path) -> str:
    """``scripts/install.sh`` as of ``ref`` (None: this worktree's copy)."""
    if ref is None:
        return (H.WORKTREE / "scripts" / "install.sh").read_text(encoding="utf-8")
    return I.git("show", f"{ref}:scripts/install.sh", cwd=source) + "\n"


@dataclass
class World:
    root: Path
    srv: GitHTTPServer
    bare: Path
    sb: I.Sandbox
    transcripts: list[str] = field(default_factory=list)
    install_bytes: int = 0
    install_shape: dict[str, str] = field(default_factory=dict)

    # -- upstream ----------------------------------------------------------------------------
    def set_main(self, sha: str) -> None:
        I.git("update-ref", "refs/heads/main", sha, cwd=self.bare)

    def publish(self, message: str, files: dict[str, str]) -> str:
        """A new upstream commit on origin main (the release the user updates to)."""
        return I.publish_commit(self.bare, self.root, message, files)

    # -- the install ---------------------------------------------------------------------------
    @property
    def checkout(self) -> Path:
        return self.sb.checkout

    def git_env(self) -> dict[str, str]:
        """Host-side git with the sandbox's HOME: its URL rewrite, never the real GitHub."""
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.sb.home),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
                "GIT_AUTHOR_NAME": "user", "GIT_AUTHOR_EMAIL": "user@example.invalid",
                "GIT_COMMITTER_NAME": "user", "GIT_COMMITTER_EMAIL": "user@example.invalid"}

    def git(self, *args: str, check: bool = True) -> str:
        return I.git(*args, cwd=self.checkout, check=check, env=self.git_env())

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD")

    def config(self, key: str) -> str:
        return self.git("config", "--get", key, check=False)

    def shape(self) -> dict[str, str]:
        """The clone's shape: partial-clone filter and shallowness."""
        return {"promisor": self.config("remote.origin.promisor") or "-",
                "filter": self.config("remote.origin.partialclonefilter") or "-",
                "shallow": self.git("rev-parse", "--is-shallow-repository")}

    def status(self) -> str:
        return self.git("status", "--porcelain=v1", "--untracked-files=all")

    def refs(self, *patterns: str) -> list[str]:
        return self.git("for-each-ref", "--format=%(refname)", *patterns).split()

    def refs_containing(self, sha: str) -> list[str]:
        """Refs (branches, stash, backups, tags) that keep ``sha`` reachable; the reflog does not count."""
        return self.git("for-each-ref", "--contains", sha, "--format=%(refname)").split()

    def reset_clean(self) -> None:
        """Per-cell isolation on a shared install: on ``main`` at its current commit, a clean tree,
        no stash, no rescue refs, no git state files left by the previous cell."""
        for leftover in ("rebase-merge", "rebase-apply", "index.lock", "MERGE_HEAD"):
            p = self.checkout / ".git" / leftover
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        self.git("checkout", "-q", "-f", "main")
        self.git("reset", "-q", "--hard", "refs/remotes/origin/main")
        self.git("clean", "-fdq")
        self.git("stash", "clear")
        for ref in self.refs("refs/hermes-update-backups", "refs/tags/pre-update-*"):
            self.git("update-ref", "-d", ref)
        assert not self.status(), f"reset_clean left a dirty tree:\n{self.status()}"

    def bytes_since(self, mark: int) -> int:
        return sum(r.body_bytes for r in self.srv.since(mark) if r.method == "POST")

    def make_depth1(self) -> None:
        """Turn the finished install into the stale one an old installer left: ``git clone --depth 1
        --single-branch`` of origin ``main``, under the same working tree.

        The installers this fixture runs (N-1's and HEAD's) end by unshallowing the checkout into a
        ``blob:none`` clone: the completion tail's release-history refresh, ``fetch_full_commit_graph``
        in ``_run_post_update_maintenance``. A depth-1 clone seeded before the install does not survive
        it, and unsetting the partial-clone flags afterwards would keep the history objects a real
        depth-1 clone never had. The install's ``.git`` is therefore replaced by the one a fresh depth-1
        clone writes and the index is rebuilt from HEAD; the files on disk are not touched, and ``git
        status`` must read the same before and after.

        ``install_bytes`` becomes what that clone downloaded, the "depth-1 install itself" the shallow
        cells bound an update's fetch by, and ``install_shape`` the new shape."""
        head, status = self.head(), self.status()
        fresh = self.root / "depth1-clone"
        mark = self.srv.mark()
        cp = subprocess.run(["git", "clone", "-q", "--no-checkout", "--depth", "1", "--single-branch", "--branch",
                             "main", I.OFFICIAL_HTTPS, str(fresh)],
                            capture_output=True, text=True, env=self.git_env(), timeout=900)
        assert cp.returncode == 0, f"depth-1 clone of origin main failed: {cp.stderr[-2000:]}"
        self.install_bytes = self.bytes_since(mark)
        cloned = I.git("rev-parse", "HEAD", cwd=fresh)
        assert cloned == head, f"origin main is at {cloned[:12]}, the install is at {head[:12]}: not the same tree"
        shutil.rmtree(self.checkout / ".git")
        shutil.move(str(fresh / ".git"), str(self.checkout / ".git"))
        shutil.rmtree(fresh)
        self.git("reset", "-q")
        assert self.status() == status, (
            f"swapping in the depth-1 .git changed the working tree's status:\nbefore: {status!r}\nafter: {self.status()!r}")
        self.install_shape = self.shape()

    def update(self, *extra: str, timeout: float = 1200) -> subprocess.CompletedProcess:
        cp = self.sb.cli("update", "--yes", "--branch", "main", *extra, timeout=timeout)
        self.transcripts.append(f"$ hermes update --yes --branch main {' '.join(extra)} -> rc={cp.returncode}\n"
                                f"{(cp.stdout or '')[-8000:]}\n{(cp.stderr or '')[-4000:]}")
        return cp

    def version(self) -> subprocess.CompletedProcess:
        return self.sb.cli("--version", timeout=180)

    def receipt(self) -> dict:
        path = self.sb.hermes_home / "logs" / "update_receipts" / "latest.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"unreadable": str(path)}

    def diag(self, cp: subprocess.CompletedProcess | None = None, mark: int = 0) -> str:
        out = []
        if cp is not None:
            out.append(H.describe(cp, limit=8000))
        with contextlib.suppress(Exception):
            out.append(f"HEAD={self.head()} branch={self.branch()} shape={self.shape()}")
            out.append("status:\n" + (self.status() or "  (clean)"))
            out.append("refs:\n" + self.git("for-each-ref", "--format=%(refname) %(objectname:short)",
                                            "refs/heads", "refs/stash", "refs/hermes-update-backups",
                                            "refs/remotes"))
        rec = self.receipt()
        if rec:
            out.append("receipt: " + json.dumps({k: rec.get(k) for k in ("outcome", "status", "exit_code", "steps")
                                                 if k in rec}, default=str)[:3000])
        logs = self.sb.hermes_home / "logs"
        for name in ("update.log", "errors.log"):
            p = logs / name
            if p.is_file():
                out.append(f"--- {p} (tail)\n" + p.read_text(encoding="utf-8", errors="replace")[-3000:])
        out.append("http requests:\n" + self.srv.describe(mark))
        return "\n".join(out)


def _preclone(sb: I.Sandbox, args: list[str], env: dict[str, str]) -> None:
    sb.hermes_home.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(["git", "clone", "-q", *args, "--branch", "main", I.OFFICIAL_HTTPS, str(sb.checkout)],
                        capture_output=True, text=True, env=env, timeout=900)
    assert cp.returncode == 0, f"pre-clone {args} failed: {cp.stderr[-2000:]}"


@contextlib.contextmanager
def served(root: Path, *, base: str, source: Path = H.WORKTREE) -> Iterator[World]:
    """Origin ``main`` at ``base`` served over smart HTTP, and an empty sandbox whose git reaches it as
    the official GitHub URL. Nothing is installed yet; the server stops on exit."""
    root.mkdir(parents=True, exist_ok=True)
    srv_root = root / "srv"
    srv_root.mkdir(exist_ok=True)
    bare = serve_bare(srv_root, REPO, source, base)
    with GitHTTPServer(srv_root) as srv:
        sb = I.new_sandbox(root / "sb")
        url = srv.url(REPO)
        (sb.home / ".gitconfig").write_text(
            f'[url "{url}"]\n  insteadOf = {I.OFFICIAL_HTTPS}\n  insteadOf = {I.OFFICIAL_SSH}\n'
            '[user]\n  name = user\n  email = user@example.invalid\n', encoding="utf-8")
        yield World(root=root, srv=srv, bare=bare, sb=sb)


@contextlib.contextmanager
def world(root: Path, *, base: str, installer_ref: str | None = None, preclone: list[str] | None = None,
          depth1: bool = False, source: Path = H.WORKTREE) -> Iterator[World]:
    """Serve origin ``main`` at ``base`` over smart HTTP and install from it with the installer
    of ``installer_ref`` (None: HEAD's). ``preclone`` seeds the checkout the installer adopts;
    ``depth1`` rebuilds the finished install into a stale depth-1 one (``World.make_depth1``).
    Yields the live ``World``; the server stops on exit."""
    with served(root, base=base, source=source) as w:
        sb = w.sb
        if preclone is not None:
            _preclone(sb, preclone, w.git_env())
        script = sb.root / "install.sh"
        script.write_text(installer_script(installer_ref, source), encoding="utf-8")
        cp = sb.run(["bash", str(script), "--non-interactive"], timeout=1800, input="")
        assert cp.returncode == 0, f"install ({installer_ref or 'HEAD'} installer, base {base[:12]}) failed:\n{w.diag(cp)}"
        head = w.head()
        assert head == base, f"installer left HEAD at {head}, expected {base}\n{w.diag(cp)}"
        w.install_bytes = w.bytes_since(0)
        w.install_shape = w.shape()
        if depth1:
            w.make_depth1()
        yield w


def start_world(root: Path, **kw) -> tuple[contextlib.AbstractContextManager, World]:
    """``world()`` entered by hand, for fixtures that build several installs concurrently."""
    cm = world(root, **kw)
    return cm, cm.__enter__()


def in_parallel(jobs: dict[str, Callable[[], object]]) -> dict[str, object]:
    """Run independent scenarios concurrently; each value is the job's result or the exception it
    raised (re-raised by the cell that reads it, so one broken install fails only its own cells)."""
    out: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(job) for name, job in jobs.items()}
        for name, fut in futures.items():
            try:
                out[name] = fut.result()
            except BaseException as exc:  # noqa: BLE001 - handed to the cell
                out[name] = exc
    return out
