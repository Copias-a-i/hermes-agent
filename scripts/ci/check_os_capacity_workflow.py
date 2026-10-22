#!/usr/bin/env python3
"""Validate that every OS-test matrix row executes bounded capacity policy."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

import hermes_yaml as yaml


REQUIRED_OUTPUTS = ("workers", "file_timeout")


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise ValueError("os-tests job has no steps list")
    return [step for step in steps if isinstance(step, dict)]


def _required_outputs(output_path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in output_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or not value.isdecimal() or int(value) <= 0:
            raise ValueError(f"invalid capacity output: {line!r}")
        if key in REQUIRED_OUTPUTS:
            values[key] = int(value)
    missing = set(REQUIRED_OUTPUTS).difference(values)
    if missing:
        raise ValueError(f"missing capacity outputs: {', '.join(sorted(missing))}")
    return values


def validate(workflow: Path, repository: Path) -> None:
    document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("workflow must be a mapping")
    jobs = document.get("jobs")
    if not isinstance(jobs, dict) or not isinstance(jobs.get("os-tests"), dict):
        raise ValueError("workflow has no os-tests job")
    job = jobs["os-tests"]
    matrix = job.get("strategy", {}).get("matrix", {})
    rows = matrix.get("include") if isinstance(matrix, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("os-tests has no matrix rows")
    if not all(isinstance(row, dict) and isinstance(row.get("runner"), str) for row in rows):
        raise ValueError("every os-tests matrix row must name a runner")

    steps = _steps(job)
    capacity_steps = [step for step in steps if step.get("id") == "capacity"]
    if len(capacity_steps) != 1:
        raise ValueError("os-tests must have exactly one capacity step")
    capacity_step = capacity_steps[0]
    if capacity_step.get("if") not in (None, "${{ true }}"):
        raise ValueError("capacity step must run for every matrix row")

    test_steps = [step for step in steps if step.get("name") == "Run ${{ matrix.marker }} tests"]
    if len(test_steps) != 1 or not isinstance(test_steps[0].get("env"), dict):
        raise ValueError("os-tests must have one test step with an environment")
    env = test_steps[0]["env"]
    for name in REQUIRED_OUTPUTS:
        expected = f"${{{{ steps.capacity.outputs.{name} }}}}"
        if env.get(f"HERMES_TEST_{name.upper()}") != expected:
            raise ValueError(f"test step must consume validated {name} output")
    if env.get("HERMES_TEST_CAPACITY_VALIDATED") != (
        "${{ steps.capacity.outputs.workers != '' && steps.capacity.outputs.file_timeout != '' }}"
    ):
        raise ValueError("test step must require nonempty capacity outputs")

    policy = repository / "scripts/ci/runner_capacity.py"
    for row in rows:
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "github-output"
            result = subprocess.run(
                [sys.executable, str(policy), "os-tests", "--runner", row["runner"], "--github-output"],
                cwd=repository,
                env={**os.environ, "GITHUB_OUTPUT": str(output_path)},
                text=True,
                capture_output=True,
            )
            if result.returncode:
                raise ValueError(f"capacity policy rejected {row['runner']}: {result.stderr.strip()}")
            _required_outputs(output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", type=Path, default=Path(".github/workflows/tests-os.yml"))
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    validate(args.workflow.resolve(), args.repository.resolve())


if __name__ == "__main__":
    main()
