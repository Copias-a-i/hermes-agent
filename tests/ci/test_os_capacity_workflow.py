"""Executable workflow contract for bounded OS-test capacity."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


REPOSITORY = Path(__file__).resolve().parents[2]
CHECK = REPOSITORY / "scripts/ci/check_os_capacity_workflow.py"
WORKFLOW = REPOSITORY / ".github/workflows/tests-os.yml"


def test_os_test_workflow_executes_capacity_policy_for_every_matrix_row() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECK), "--workflow", str(WORKFLOW), "--repository", str(REPOSITORY)],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_os_test_workflow_contract_rejects_unsupported_matrix_runner(tmp_path: Path) -> None:
    workflow = tmp_path / "tests-os.yml"
    workflow.write_text(
        WORKFLOW.read_text(encoding="utf-8").replace("runner: macos-latest", "runner: macos-future", 1),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(CHECK), "--workflow", str(workflow), "--repository", str(REPOSITORY)],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "capacity policy rejected macos-future" in result.stderr
