"""Named-profile identity through recognized bootstrap argv (COP-702)."""
from pathlib import Path
import pytest
from gateway import status


def _bootstrap(profile):
    return (
        "python -c import sys, runpy; sys.path.insert(0, '/opt/hermes'); "
        f"sys.argv=['/opt/hermes/hermes_cli/main.py', '--profile', '{profile}', "
        "'gateway', 'run', '--external-supervisor']; "
        "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
    )


def test_assigned_bootstrap_argv_has_profile_identity(tmp_path):
    home = tmp_path / "profiles" / "compliance"
    command = _bootstrap("compliance")
    assert status.looks_like_gateway_runtime_command_line(command)
    assert status.profile_flag_value(command) == "compliance"
    assert status._command_line_belongs_to_profile(command, home)


@pytest.mark.parametrize("command", [
    _bootstrap("compliance-2"),
    "python -c import time; time.sleep(60) --profile compliance gateway run",
    "python -c import subprocess; subprocess.Popen(['hermes', '--profile', 'compliance', 'gateway', 'run'])",
])
def test_foreign_profile_and_inline_watchers_do_not_match(tmp_path, command):
    home = tmp_path / "profiles" / "compliance"
    assert not status._command_line_belongs_to_profile(command, home)


def test_named_bootstrap_does_not_claim_default_home(tmp_path):
    assert not status._command_line_belongs_to_profile(_bootstrap("compliance"), tmp_path)


@pytest.mark.parametrize("flag", ["--profile compliance", "--profile=compliance", "-p compliance"])
def test_ordinary_profile_flags_still_match(tmp_path, flag):
    command = f"python -m hermes_cli.main {flag} gateway run"
    assert status.profile_flag_value(command) == "compliance"
    assert status._command_line_belongs_to_profile(command, tmp_path / "profiles" / "compliance")
