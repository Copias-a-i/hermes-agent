"""Executable standard-runner capacity policy consumed by CI workflows."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Capacity:
    workers: int
    file_timeout: int
    install_slots: int | None = None
    job_timeout: int | None = None
    workspace_checks: int | None = None


def capacity(lane: str, runner: str = "") -> Capacity:
    if lane == "workspace-checks":
        return Capacity(workers=0, file_timeout=0, workspace_checks=2)
    if lane == "linux-tests":
        return Capacity(workers=4, file_timeout=600)
    if lane == "os-tests":
        # The marked macOS suite is split in two and runs under the same
        # max-parallel: 2 cap as Windows. Two file workers leave capacity for
        # pytest collection and host services on both Intel (macos-13) and
        # ARM (macos-14/latest) hosted runners; 600 seconds retains the
        # measured Windows per-file ceiling instead of falling back to CPU count.
        if runner in {"macos-13", "macos-14", "macos-latest"}:
            return Capacity(workers=2, file_timeout=600)
        if runner == "windows-11-arm":
            return Capacity(workers=2, file_timeout=600, job_timeout=75)
        if runner == "windows-latest":
            return Capacity(workers=3, file_timeout=600, job_timeout=30)
        raise ValueError(f"unsupported OS-test runner: {runner}")
    if lane == "windows-install-update":
        # Hosted evidence: two disk-safe install slots completed the first two
        # heavy files in 25–28 minutes, while a third worker only waited on a
        # slot. Match workers to slots; ten heavy files then need five waves
        # (~140 minutes) plus setup, so 165 minutes is bounded completion
        # headroom without allowing a third simultaneous disk expansion.
        return Capacity(workers=2, file_timeout=3600, install_slots=2, job_timeout=165)
    raise ValueError(f"unknown capacity lane: {lane}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("lane")
    parser.add_argument("--runner", default="")
    parser.add_argument("--github-output", action="store_true")
    args = parser.parse_args()
    values = {key: value for key, value in asdict(capacity(args.lane, args.runner)).items() if value is not None}
    if args.github_output:
        import os
        output = os.environ["GITHUB_OUTPUT"]
        with open(output, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    else:
        print(json.dumps(values, sort_keys=True))


if __name__ == "__main__":
    main()
