"""Partial and full clones updated N-1 -> HEAD -> next release through the real ``hermes update``.

Users' checkouts come in several shapes: HEAD's installer makes a ``--filter=blob:none`` clone,
installers from late Sep 2026 made ``--filter=tree:0`` ones, and plenty of users cloned the repository
themselves (a full clone). Each shape is seeded as that clone of N-1 over smart HTTP, adopted by
N-1's own ``scripts/install.sh``, then updated twice: to HEAD (N-1's updater hands off to HEAD's),
and to one more upstream release (HEAD's updater end to end). The three installs run concurrently.

Property: both updates land and the install runs, and the clone keeps its shape: a partial clone
keeps its filter, and a full clone stays full. The one conversion is treeless -> blobless (#129712):
a treeless checkout re-downloads whole directory snapshots on every checkout. Converting a full clone to ``tree:0`` re-arms the
``pack-objects ... should_include_obj`` fetch crash for every later update (#124272, #124323).
"""

from __future__ import annotations

import pytest

from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.git import _git_world as G

pytestmark = G.PYTESTMARK

SHAPES = {
    "tree0": ["--filter=tree:0"],
    "blobless": ["--filter=blob:none"],
    "full": [],
}


def test_fixture_release_tag_is_reachable_from_the_upgrade_target(tmp_path):
    history = G.build_upgrade_history(tmp_path / "history")
    assert I.git("describe", "--tags", "--abbrev=0", "--match", "v20[0-9][0-9].*", f"{history.target}~1",
                 cwd=history.source) == history.tag


def test_fixture_history_has_no_matching_tag_when_requested(tmp_path):
    history = G.build_upgrade_history(tmp_path / "history")
    with pytest.raises(AssertionError, match=r"(?:No tags can describe|No names found, cannot describe anything)"):
        I.git("describe", "--tags", "--abbrev=0", "--match", "v20[0-9][0-9].*", history.untagged, cwd=history.source)


def _scenario(root, shape: str, history: G.UpgradeHistory) -> dict:
    cm, w = G.start_world(root / shape, base=history.base, installer_ref=history.tag, preclone=SHAPES[shape],
                          source=history.source)
    run = {"cm": cm, "w": w, "shape0": w.install_shape, "target1": history.target}
    w.set_main(history.target)
    run["cp1"], run["head1"], run["shape1"] = w.update(), w.head(), w.shape()
    run["target2"] = w.publish(f"release: e2e partial {shape}", {f"e2e-partial-{shape}.txt": "release\n"})
    run["cp2"], run["head2"], run["shape2"] = w.update(), w.head(), w.shape()
    run["version"] = w.version()
    return run


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("git-partial")
    history = G.build_upgrade_history(root / "history")
    results = G.in_parallel({shape: (lambda s=shape: _scenario(root, s, history)) for shape in SHAPES})
    yield results
    for res in results.values():
        if isinstance(res, dict):
            res["cm"].__exit__(None, None, None)


@pytest.mark.parametrize("shape", list(SHAPES))
def test_updates_land_and_the_clone_keeps_its_shape(runs, shape):
    run = runs[shape]
    if isinstance(run, BaseException):
        raise run
    w: G.World = run["w"]
    cp1, cp2 = run["cp1"], run["cp2"]
    expected = {**run["shape0"], "filter": "blob:none"} if shape == "tree0" else run["shape0"]
    assert run["shape0"]["shallow"] == "false" and (shape == "full") == (run["shape0"]["filter"] == "-"), \
        f"seeded the wrong clone shape for {shape}: {run['shape0']}"
    assert cp1.returncode == 0 and run["head1"] == run["target1"], f"N-1 -> HEAD update failed:\n{w.diag(cp1)}"
    assert G.TRACEBACK not in G.output(cp1), f"N-1 -> HEAD update printed a traceback:\n{w.diag(cp1)}"
    assert run["shape1"] == expected, (
        f"{'the full clone was converted to a partial clone' if shape == 'full' else 'the clone changed shape'} "
        f"by the N-1 -> HEAD update: {run['shape0']} -> {run['shape1']} (expected {expected})\n{w.diag(cp1)}")
    assert cp2.returncode == 0 and run["head2"] == run["target2"], f"HEAD -> next update failed:\n{w.diag(cp2)}"
    assert run["shape2"] == expected, f"the clone changed shape: {expected} -> {run['shape2']}\n{w.diag(cp2)}"
    version = run["version"]
    assert version.returncode == 0 and G.TRACEBACK not in G.output(version), w.diag(version)
