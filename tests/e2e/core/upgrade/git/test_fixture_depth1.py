"""``World.make_depth1`` leaves the stale install the shallow cells update: a real depth-1 clone.

Every installer ends by unshallowing its checkout into a ``blob:none`` clone, so a depth-1 clone seeded
before the install is gone by the time the install finishes (the shallow cells once asserted
``'shallow': 'false'`` against a clone they had seeded shallow). The stale shape is made after the install
instead, and these pin what it must be: no promisor flags, a single-commit ``.git/shallow``, none of the
history's objects in the database, the install's own files untouched, and the bytes of the clone itself as
the size of "the depth-1 install". Pure git, no sandbox and no installer: the install is a ``blob:none``
clone, the layout the installer leaves.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.git import _git_world as G

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    return G.build_upgrade_history(tmp_path_factory.mktemp("depth1-history") / "history")


@pytest.fixture()
def install(tmp_path, history):
    """What an installer run leaves at N-1: a ``blob:none`` clone with the installer's own git config."""
    with G.served(tmp_path / "w", base=history.base, source=history.source) as w:
        w.sb.hermes_home.mkdir(parents=True, exist_ok=True)
        cp = subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--branch", "main", I.OFFICIAL_HTTPS,
                             str(w.checkout)], capture_output=True, text=True, env=w.git_env(), timeout=900)
        assert cp.returncode == 0, f"installing the blob:none clone failed: {cp.stderr[-2000:]}"
        w.git("config", "maintenance.commit-graph.enabled", "false")
        w.install_bytes, w.install_shape = w.bytes_since(0), w.shape()
        assert w.install_shape == {"promisor": "true", "filter": "blob:none", "shallow": "false"}, w.install_shape
        yield w


def _has_object(w: G.World, sha: str) -> bool:
    cp = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=str(w.checkout), env=w.git_env(),
                        capture_output=True)
    return cp.returncode == 0


def test_the_install_becomes_a_depth_1_clone_with_none_of_the_history(install, history):
    assert _has_object(install, history.untagged), "the blob:none install must carry N-1's parent"

    install.make_depth1()

    assert install.shape() == {"promisor": "-", "filter": "-", "shallow": "true"} == install.install_shape
    assert install.head() == history.base
    assert (install.checkout / ".git" / "shallow").read_text(encoding="utf-8").split() == [history.base]
    # Absent, not hidden: a graft over a full object database would still hold the parent.
    assert not _has_object(install, history.untagged), "N-1's parent is still in the object database"
    assert not install.status()


def test_the_install_size_is_what_a_depth_1_clone_downloads(install, history, tmp_path):
    blobless = install.install_bytes
    mark = install.srv.mark()
    cp = subprocess.run(["git", "clone", "-q", "--no-checkout", "--depth", "1", "--single-branch", "--branch", "main",
                         I.OFFICIAL_HTTPS, str(tmp_path / "reference")], capture_output=True, text=True,
                        env=install.git_env(), timeout=900)
    assert cp.returncode == 0, cp.stderr[-2000:]
    reference = install.bytes_since(mark)

    install.make_depth1()

    assert reference > 0 and install.install_bytes != blobless
    assert abs(install.install_bytes - reference) <= reference * 0.05, (
        f"install_bytes {install.install_bytes} is not the size of a depth-1 clone ({reference})")


def test_the_working_tree_is_left_exactly_as_it_was(install):
    (install.checkout / "local-note.txt").write_text("mine\n", encoding="utf-8")
    readme = install.checkout / "README.md"
    edited = readme.read_text(encoding="utf-8") + "\nedited by the user\n"
    readme.write_text(edited, encoding="utf-8")
    before = install.status()
    assert "?? local-note.txt" in before and "M README.md" in before, before

    install.make_depth1()

    assert install.status() == before
    assert readme.read_text(encoding="utf-8") == edited
    assert (install.checkout / "local-note.txt").read_text(encoding="utf-8") == "mine\n"


def test_the_depth_1_clone_still_reaches_origin(install, history):
    install.make_depth1()

    assert install.config("remote.origin.url") == I.OFFICIAL_HTTPS
    install.set_main(history.target)
    install.git("fetch", "-q", "origin", "main")
    assert install.git("rev-parse", "refs/remotes/origin/main") == history.target
