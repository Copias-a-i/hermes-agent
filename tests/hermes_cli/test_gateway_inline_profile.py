"""CLI process scans bind recognized inline bootstraps to their named profile."""
from types import SimpleNamespace
import hermes_cli.gateway as gateway


def test_inline_bootstrap_scan_accepts_only_current_profile(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "compliance"
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "_profile_arg", lambda _home: "--profile compliance")
    monkeypatch.setattr(gateway, "_get_ancestor_pids", lambda: set())
    monkeypatch.setattr(gateway, "is_windows", lambda: False)
    original_isdir = gateway.os.path.isdir
    monkeypatch.setattr(gateway.os.path, "isdir", lambda path: False if path == "/proc" else original_isdir(path))
    from hermes_cli import dashboard_procs
    monkeypatch.setattr(dashboard_procs, "_hermes_home_for_pid", lambda pid: None)
    from gateway import status
    monkeypatch.setattr(status, "_pid_exists", lambda pid: False)
    def command(profile):
        return (
            "python -c import sys, runpy; sys.path.insert(0, '/opt/hermes'); "
            f"sys.argv=['/opt/hermes/hermes_cli/main.py', '--profile', '{profile}', 'gateway', 'run']; "
            "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
        )
    listing = f"8611 {command('compliance')}\n8612 {command('compliance-2')}\n"
    listing += "8613 python -c import time; time.sleep(60) --profile compliance gateway run\n"
    monkeypatch.setattr(gateway.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=listing))
    assert gateway._scan_gateway_pids(set()) == [8611]
