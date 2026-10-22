"""Executable capacity contracts for native Windows lanes."""

from __future__ import annotations

import pytest

from scripts.ci.runner_capacity import capacity


def test_windows_os_capacity_is_runner_specific_and_rejects_unknown_hosts() -> None:
    assert capacity("windows-os", "windows-latest").workers == 3
    assert capacity("windows-os", "windows-11-arm").workers == 2
    with pytest.raises(ValueError, match="unknown Windows runner"):
        capacity("windows-os", "windows-future")


def test_windows_install_capacity_bounds_disk_heavy_journeys() -> None:
    policy = capacity("windows-install-update")
    assert policy.workers == 3
    assert policy.install_slots == 2
    assert policy.job_timeout == 75
