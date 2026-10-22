"""Executable capacity-policy contracts for standard hosted runners."""

from __future__ import annotations

from scripts.ci.runner_capacity import capacity


def test_workspace_checks_have_a_two_check_budget() -> None:
    policy = capacity("workspace-checks")
    assert policy.workspace_checks == 2


def test_linux_tests_preserve_workers_and_heartbeat_headroom() -> None:
    policy = capacity("linux-tests")
    assert (policy.workers, policy.file_timeout) == (4, 600)


def test_windows_lanes_keep_separate_measured_budgets() -> None:
    x64 = capacity("windows-os", "windows-latest")
    arm = capacity("windows-os", "windows-11-arm")
    install = capacity("windows-install-update")
    assert (x64.workers, x64.file_timeout) == (3, 600)
    assert (arm.workers, arm.file_timeout, arm.job_timeout) == (2, 600, 75)
    assert (install.workers, install.install_slots, install.file_timeout, install.job_timeout) == (3, 2, 3600, 75)
