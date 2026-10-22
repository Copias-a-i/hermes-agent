"""Executable capacity-policy contracts for standard hosted runners."""

from __future__ import annotations

from scripts.ci.runner_capacity import capacity


def test_workspace_checks_have_a_two_check_budget() -> None:
    policy = capacity("workspace-checks")
    assert policy.workspace_checks == 2


def test_linux_tests_preserve_workers_and_heartbeat_headroom() -> None:
    policy = capacity("linux-tests")
    assert (policy.workers, policy.file_timeout) == (4, 600)


def test_os_test_lanes_keep_separate_measured_budgets() -> None:
    mac_intel = capacity("os-tests", "macos-13")
    mac_arm = capacity("os-tests", "macos-14")
    mac_latest = capacity("os-tests", "macos-latest")
    x64 = capacity("os-tests", "windows-latest")
    arm = capacity("os-tests", "windows-11-arm")
    install = capacity("windows-install-update")
    assert (mac_intel.workers, mac_intel.file_timeout) == (2, 600)
    assert (mac_arm.workers, mac_arm.file_timeout) == (2, 600)
    assert (mac_latest.workers, mac_latest.file_timeout) == (2, 600)
    assert (x64.workers, x64.file_timeout, x64.job_timeout) == (3, 600, 30)
    assert (arm.workers, arm.file_timeout, arm.job_timeout) == (2, 600, 75)
    assert (install.workers, install.install_slots, install.file_timeout, install.job_timeout) == (3, 2, 3600, 75)
