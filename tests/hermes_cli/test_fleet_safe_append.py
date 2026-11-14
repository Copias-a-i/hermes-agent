"""Process-level proof for fleet-safe Markdown appends."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from hermes_cli.fleet_safe_append import (
    SafeAppendError,
    _root_directory,
    _target_parent,
    _validate_target,
    append_markdown,
)


def _append_worker(root: str, target: str, index: int) -> None:
    append_markdown(
        root, target, f"## event {index}\nbody {index}\n", dedupe_key=f"worker-{index}"
    )


def test_concurrent_processes_keep_every_entry_and_valid_markdown(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    target = root / "jarvis" / "session-history.md"
    processes = [
        multiprocessing.Process(target=_append_worker, args=(str(root), str(target), n))
        for n in range(24)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    body = target.read_text(encoding="utf-8-sig")
    assert body.endswith("\n")
    assert "\r" not in body
    for n in range(24):
        assert body.count(f"<!-- fleet-safe-append:worker-{n} -->") == 1
        assert body.count(f"## event {n}\n") == 1


def test_duplicate_retry_is_a_noop_and_normalizes_newlines(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    target = root / "jarvis" / "working-memory.md"
    first = append_markdown(root, target, "## hello\r\nline\r\n", dedupe_key="turn-1")
    second = append_markdown(root, target, "## changed", dedupe_key="turn-1")
    body = target.read_text(encoding="utf-8-sig")
    assert first.appended is True
    assert second.appended is False
    assert body.count("fleet-safe-append:turn-1") == 1
    assert "changed" not in body
    assert "\r" not in body and body.endswith("\n")


def test_crash_before_rename_preserves_prior_file(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    target = root / "jarvis" / "learnings.md"
    target.parent.mkdir()
    target.write_text("# existing\n", encoding="utf-8")

    def crash() -> None:
        raise RuntimeError("simulated process death before rename")

    with pytest.raises(RuntimeError, match="before rename"):
        append_markdown(
            root, target, "## new", dedupe_key="crash-case", before_replace=crash
        )
    assert target.read_text(encoding="utf-8-sig") == "# existing\n"
    assert not [
        path
        for path in target.parent.glob(f".{target.name}.*")
        if not path.name.endswith(".fleet-safe.lock")
    ]


def test_symlink_attack_and_root_escape_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    outside = tmp_path / "outside.md"
    root.mkdir()
    outside.write_text("outside\n", encoding="utf-8")
    link = root / "jarvis" / "working-memory.md"
    link.parent.mkdir()
    link.symlink_to(outside)
    with pytest.raises(SafeAppendError, match="non-symlink"):
        append_markdown(root, link, "## attack", dedupe_key="attack")
    with pytest.raises(SafeAppendError, match="escapes"):
        append_markdown(
            root, root / ".." / "escape.md", "## attack", dedupe_key="escape"
        )
    assert outside.read_text(encoding="utf-8-sig") == "outside\n"


def test_parent_swap_after_validation_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    parent = root / "jarvis"
    outside = tmp_path / "outside"
    root.mkdir()
    parent.mkdir()
    outside.mkdir()
    safe_root, _, components = _validate_target(root, parent / "working-memory.md")
    moved = root / "moved"
    parent.rename(moved)
    parent.symlink_to(outside, target_is_directory=True)
    with _root_directory(safe_root) as root_fd:
        with pytest.raises(SafeAppendError):
            with _target_parent(root_fd, components):
                pass


def test_secret_payload_and_stale_dead_owner_lock_are_handled(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    target = root / "jarvis" / "mistakes.md"
    target.parent.mkdir()
    lock = target.with_name(f".{target.name}.fleet-safe.lock")
    lock.write_text("pid=99999999\nacquired_unix=0\n", encoding="ascii")
    with pytest.raises(SafeAppendError, match="secret"):
        append_markdown(root, target, "token=not-safe", dedupe_key="secret")
    result = append_markdown(root, target, "## recovered", dedupe_key="dead-owner")
    assert result.appended is True
    assert "pid=" in lock.read_text(encoding="ascii")
    assert "dead-owner" in target.read_text(encoding="utf-8-sig")


def test_lock_symlink_cannot_escape_pinned_parent(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    target = root / "jarvis" / "working-memory.md"
    outside = tmp_path / "outside-lock"
    root.mkdir()
    target.parent.mkdir()
    outside.write_text("do not touch\n", encoding="utf-8")
    target.with_name(f".{target.name}.fleet-safe.lock").symlink_to(outside)

    with pytest.raises(SafeAppendError, match="lock path"):
        append_markdown(root, target, "## attack", dedupe_key="lock-symlink")

    assert outside.read_text(encoding="utf-8") == "do not touch\n"
    assert not target.exists()


def test_lock_hardlink_cannot_overwrite_outside_file(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    target = root / "jarvis" / "working-memory.md"
    outside = tmp_path / "outside-lock"
    root.mkdir()
    target.parent.mkdir()
    outside.write_text("do not touch\n", encoding="utf-8")
    os.link(outside, target.with_name(f".{target.name}.fleet-safe.lock"))

    with pytest.raises(SafeAppendError, match="regular unlinked"):
        append_markdown(root, target, "## attack", dedupe_key="lock-hardlink")

    assert outside.read_text(encoding="utf-8") == "do not touch\n"
    assert not target.exists()


def test_symlinked_root_ancestor_is_rejected(tmp_path: Path) -> None:
    actual_parent = tmp_path / "actual"
    linked_parent = tmp_path / "linked"
    root = linked_parent / "vault"
    actual_parent.mkdir()
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    (actual_parent / "vault").mkdir()

    with pytest.raises(SafeAppendError, match="vault root"):
        append_markdown(root, "jarvis/working-memory.md", "## attack", dedupe_key="root-link")

    assert not (actual_parent / "vault" / "jarvis" / "working-memory.md").exists()
