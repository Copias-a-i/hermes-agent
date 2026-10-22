"""Capacity contracts for the native Windows OS-test matrix."""

from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/tests-os.yml"


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
