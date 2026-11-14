"""The incident command must be rejected without spawning any process."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

GUARD = Path(__file__).resolve().parents[2] / "scripts" / "fleet_kill_guard.py"


@pytest.mark.parametrize("command", [
    "pkill -9 -f python 2>/dev/null; sleep 2",
    "/usr/bin/pkill -f hermes", "killall Python", "env pkill -f node",
    "sudo pkill -9 -f python", "bash -lc 'pkill -f python'",
    "echo ok && pkill -f vault_ingest", "echo $(pkill -f python)",
])
def test_process_name_kills_are_blocked(command):
    assert GUARD.exists(), "The process-name kill guard is not installed in source"
    result = subprocess.run([sys.executable, str(GUARD)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "verified PID" in result.stderr


@pytest.mark.parametrize("command", [
    "launchctl kill SIGTERM gui/501/ai.hermes.gateway-cfo",
    "kill -TERM 12345", "ps -axo pid,ppid,command", "pgrep -fl python",
    "grep pkill some.log", "echo pkill", "python scratch.py",
])
def test_read_only_probes_and_targeted_stops_are_allowed(command):
    assert GUARD.exists()
    result = subprocess.run([sys.executable, str(GUARD)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, check=False)
    assert result.returncode == 0


def test_install_preserves_loaded_hook_input_and_is_idempotent(tmp_path):
    installer = GUARD.with_name("install_studio_kill_guard.py")
    hook = tmp_path / "existing.sh"
    original = '#!/bin/bash\nINPUT="$(cat || true)"\nprintf "%s" "$INPUT"\n'
    hook.write_text(original)
    backup = tmp_path / "backup.sh"
    destination = tmp_path / "guard.py"
    args = [sys.executable, str(installer), "--hook", str(hook), "--destination", str(destination),
            "--backup", str(backup), "--python", sys.executable]
    subprocess.run(args, check=True, capture_output=True)
    subprocess.run(args, check=True, capture_output=True)
    assert backup.read_text() == original
    assert hook.read_text().count("# Studio process-name kill guard (COP-699)") == 1
    safe = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ps aux"}})
    allowed = subprocess.run(["bash", str(hook)], input=safe, text=True, capture_output=True)
    assert allowed.returncode == 0
    assert allowed.stdout == safe
    denied = subprocess.run(["bash", str(hook)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "pkill -9 -f python"}}),
        text=True, capture_output=True)
    assert denied.returncode == 2
    assert denied.stdout == ""


def test_install_refuses_unknown_stdin_layout(tmp_path):
    installer = GUARD.with_name("install_studio_kill_guard.py")
    hook = tmp_path / "existing.sh"
    hook.write_text("#!/bin/bash\nexit 0\n")
    result = subprocess.run([sys.executable, str(installer), "--hook", str(hook),
        "--destination", str(tmp_path / "guard.py"), "--backup", str(tmp_path / "backup.sh")],
        capture_output=True, text=True)
    assert result.returncode != 0
    assert hook.read_text() == "#!/bin/bash\nexit 0\n"
