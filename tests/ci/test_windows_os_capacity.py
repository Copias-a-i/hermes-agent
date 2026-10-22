"""Capacity contracts for the native Windows OS-test matrix."""

from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/tests-os.yml"
INSTALL_UPDATE_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/windows-install-update-e2e.yml"
TEST_RUNNER = Path(__file__).resolve().parents[2] / "scripts/run_tests.sh"


def test_windows_install_update_e2e_has_bounded_worker_and_disk_budgets():
    """Three workers meet the deadline while at most two clean toolchains consume disk."""
    workflow = INSTALL_UPDATE_WORKFLOW.read_text(encoding="utf-8")
    runner = TEST_RUNNER.read_text(encoding="utf-8")

    assert "runs-on: windows-latest" in workflow
    assert "timeout-minutes: 75" in workflow
    assert "HERMES_TEST_WORKERS: '3'" in workflow
    assert "HERMES_E2E_INSTALL_SLOTS: '2'" in workflow
    assert "HERMES_TEST_FILE_RETRIES: '0'" in workflow
    assert "HERMES_E2E_INSTALL_SLOTS" in runner


def test_windows_lanes_use_separate_bounded_capacity_budgets():
    """x64 and ARM preserve their individually measured process-tree budgets."""
    source = WORKFLOW.read_text(encoding="utf-8")

    arm_lane = source.index("runner: windows-11-arm")
    assert "timeout: 75" in source[arm_lane:source.index("steps:", arm_lane)]
    assert (
        "HERMES_TEST_WORKERS: ${{ matrix.runner == 'windows-11-arm' && '2' || "
        "matrix.runner == 'windows-latest' && '3' || '' }}"
    ) in source
    assert (
        "HERMES_TEST_FILE_TIMEOUT: ${{ matrix.runner == 'windows-11-arm' && '600' || "
        "matrix.runner == 'windows-latest' && '600' || '' }}"
    ) in source
