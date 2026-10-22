"""Executable capacity-policy contracts for every OS-test runner."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


REPOSITORY = Path(__file__).resolve().parents[2]
POLICY = REPOSITORY / "scripts/ci/runner_capacity.py"


@pytest.mark.parametrize(
    ("runner", "expected"),
    [
        ("windows-latest", {"workers": "3", "file_timeout": "600", "job_timeout": "30"}),
        ("windows-11-arm", {"workers": "2", "file_timeout": "600", "job_timeout": "75"}),
        ("macos-13", {"workers": "2", "file_timeout": "600"}),
        ("macos-14", {"workers": "2", "file_timeout": "600"}),
        ("macos-latest", {"workers": "2", "file_timeout": "600"}),
    ],
)
def test_os_capacity_cli_emits_bounded_github_outputs(
    tmp_path: Path, runner: str, expected: dict[str, str]
) -> None:
    output = tmp_path / "github-output"
    result = subprocess.run(
        [sys.executable, str(POLICY), "os-tests", "--runner", runner, "--github-output"],
        cwd=REPOSITORY,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert values == expected
    assert int(values["workers"]) > 0
    assert int(values["file_timeout"]) > 0


def test_os_capacity_cli_rejects_unknown_runner_before_test_launch(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    result = subprocess.run(
        [sys.executable, str(POLICY), "os-tests", "--runner", "macos-future", "--github-output"],
        cwd=REPOSITORY,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "unsupported OS-test runner: macos-future" in result.stderr
    assert not output.exists()


def test_windows_install_capacity_bounds_disk_heavy_journeys() -> None:
    result = subprocess.run(
        [sys.executable, str(POLICY), "windows-install-update"],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == '{"file_timeout": 3600, "install_slots": 2, "job_timeout": 165, "workers": 2}'
