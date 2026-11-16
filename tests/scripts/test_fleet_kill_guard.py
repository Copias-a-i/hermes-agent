"""The incident command must be rejected without spawning any process."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

GUARD = Path(__file__).resolve().parents[2] / "scripts" / "fleet_kill_guard.py"


def _decide(command):
    return subprocess.run([sys.executable, str(GUARD)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, check=False)


@pytest.mark.parametrize("command", [
    "pkill -9 -f python 2>/dev/null; sleep 2",
    "/usr/bin/pkill -f hermes", "killall Python", "env pkill -f node",
    "sudo pkill -9 -f python", "bash -lc 'pkill -f python'",
    "echo ok && pkill -f vault_ingest", "echo $(pkill -f python)",
])
def test_process_name_kills_are_blocked(command):
    assert GUARD.exists(), "The process-name kill guard is not installed in source"
    result = _decide(command)
    assert result.returncode == 2
    assert "verified PID" in result.stderr


@pytest.mark.parametrize("command", [
    # A wrapper option that takes a value, or an operand before the command, used to end the
    # wrapper parse early and admit the wrapped kill (COP-699 review).
    "sudo -u root pkill -f python", "timeout 2s pkill -f python",
    "sudo -nu root pkill -f python", "sudo -uroot pkill -f python",
    "sudo --user root pkill -f python", "sudo --user=root pkill -f python",
    "sudo -- pkill -f python", "sudo FOO=1 pkill -f python", "sudo -E env FOO=1 pkill -f python",
    "timeout -k 5 10s pkill -f python", "timeout --signal KILL 5 pkill -f python",
    "timeout --signal=KILL 5 pkill -f python", "gtimeout 1.5 killall Python",
    "nice -n 10 pkill -f python", "nice -10 pkill -f python", "ionice -c3 -n7 pkill -f python",
    "stdbuf -oL pkill -f python", "stdbuf -o L pkill -f python",
    "env -u PATH pkill -f python", "env -i FOO=1 pkill -f python", "env --unset PATH pkill -f python",
    "command -p pkill -f python", "exec -a scratch pkill -f python", "nohup pkill -f python &",
    "time -p pkill -f python", "setsid -f pkill -f python", "doas -u root pkill -f python",
    "echo python | xargs -n1 pkill -f", "sudo -u root nice -n 5 timeout -k 5 10s killall Python",
    "sudo -u root bash -c 'pkill -f python'", "env FOO=1 sh -c 'killall node'",
    "sudo su -c 'pkill -f python'", "eval pkill -f python", "PKILL -f python",
])
def test_wrapped_process_name_kills_are_blocked(command):
    result = _decide(command)
    assert result.returncode == 2
    assert "verified PID" in result.stderr


@pytest.mark.parametrize("command", [
    # Options this guard cannot parse may hide the wrapped command: refuse on the name it carries.
    "sudo --frobnicate pkill -f python", "timeout --frobnicate 5 pkill -f python",
    "sudo -h localhost pkill -f python", "env - pkill -f python", "env -S 'pkill -f python'",
    "arch -arm64 pkill -f python", "xargs -i{} pkill -f {}",
])
def test_unparseable_wrapper_options_fail_closed(command):
    assert _decide(command).returncode == 2


@pytest.mark.parametrize("command", [
    # The command string is the first operand after the shell's options, not the word after
    # `-c`: an option terminator or another option may sit between them (COP-699 review).
    "bash -c -- 'pkill -9 -f python'", "sh -c -- 'killall node'", "bash -c -e 'pkill -f python'",
    "bash -c -o errexit 'pkill -f python'", "zsh -c - 'pkill -f python'",
    "sudo -u root bash -c -- 'pkill -f python'", "su root -c 'pkill -f python'",
    "su --command='pkill -f python'", "runuser -u root --command 'pkill -f python'",
])
def test_command_string_after_shell_options_is_inspected(command):
    assert _decide(command).returncode == 2


@pytest.mark.parametrize("command", [
    "{ pkill -f python; }", "! pkill -f python", "if true; then pkill -f python; fi",
    "for name in a b; do pkill -f $name; done",
    "ls\npkill -f python", "ls # not a comment for the next line\npkill -f python",
    "cd /tmp;\n\npkill -f python", "echo 'a\nb'\nkillall Python",
    # The shell deletes a backslash-newline pair: it joins lines and the words on either side.
    "sudo -u root \\\n  pkill -f python", "sudo -u \\\nroot pkill -f python",
    "timeout \\\n5 pkill -f python", "sudo \\\npkill -f python", "p\\\nkill -f python",
])
def test_kills_inside_compound_commands_and_later_lines_are_blocked(command):
    assert _decide(command).returncode == 2


@pytest.mark.parametrize("command", [
    "launchctl kill SIGTERM gui/501/ai.hermes.gateway-cfo",
    "kill -TERM 12345", "ps -axo pid,ppid,command", "pgrep -fl python",
    "grep pkill some.log", "echo pkill", "python scratch.py",
    "sudo -u root ls", "sudo -u root kill -TERM 123", "timeout 2s ls", "timeout 5 echo pkill",
    "sudo grep pkill /var/log/system.log", "env FOO=pkill echo hi", "nice -n 5 grep -rn pkill .",
    "xargs -n1 kill -TERM", "sudo -u root bash -c 'launchctl list'", "arch -arm64 python scratch.py",
    "ls\nkill -TERM 1", "if true; then echo ok; fi", "{ echo ok; }", "echo ok # pkill is banned",
    "make 2>&1 | tee build.log", "launchctl kickstart -k system/ai.hermes.gateway-cfo",
])
def test_read_only_probes_and_targeted_stops_are_allowed(command):
    assert GUARD.exists()
    assert _decide(command).returncode == 0


def test_install_preserves_loaded_hook_input_and_is_idempotent(tmp_path):
    installer = GUARD.with_name("install_studio_kill_guard.py")
    hook = tmp_path / "existing.sh"
    original = '#!/usr/bin/env bash\nINPUT="$(cat || true)"\nprintf "%s" "$INPUT"\n'
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
    hook.write_text("#!/usr/bin/env bash\nexit 0\n")
    result = subprocess.run([sys.executable, str(installer), "--hook", str(hook),
        "--destination", str(tmp_path / "guard.py"), "--backup", str(tmp_path / "backup.sh")],
        capture_output=True, text=True)
    assert result.returncode != 0
    assert hook.read_text() == "#!/usr/bin/env bash\nexit 0\n"
