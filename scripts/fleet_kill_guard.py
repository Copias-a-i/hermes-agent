#!/usr/bin/env python3
"""Claude Bash pre-tool guard: stop by service or verified PID, never process name.

This prevents accidental fleet-wide pkill/killall, not arbitrary-code execution.
It does not execute or log the proposed command and needs no credentials.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import sys


def process_name_kill(command: str) -> bool:
    # Inspect command substitutions separately; shlex otherwise keeps quoted
    # substitutions inside an argument to a harmless command such as echo.
    for substitution in re.findall(r"\$\(([^()]*)\)", command):
        if process_name_kill(substitution):
            return True
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        # A malformed shell command carrying these names is not safe to admit.
        return bool(re.search(r"\b(?:pkill|killall)\b", command))
    segments = [[]]
    for token in tokens:
        if token and all(character in ";&|()" for character in token):
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        while segment and ("=" in segment[0] or segment[0] == "$" or
                Path(segment[0]).name in {"sudo", "env", "command", "exec", "nohup", "timeout", "gtimeout"}):
            segment.pop(0)
            while segment and (segment[0].startswith("-") or segment[0].isdigit()):
                segment.pop(0)
        if not segment:
            continue
        executable = Path(segment[0]).name
        if executable in {"pkill", "killall"}:
            return True
        if executable in {"sh", "bash", "zsh", "dash", "ksh"}:
            for index, token in enumerate(segment[1:], 1):
                if token.startswith("-") and "c" in token and index + 1 < len(segment):
                    if process_name_kill(segment[index + 1]):
                        return True
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if payload.get("tool_name") != "Bash":
            return 0
        command = payload.get("tool_input", {}).get("command", "")
        if not isinstance(command, str):
            raise ValueError("command must be text")
    except (ValueError, TypeError, AttributeError):
        print("Process-kill guard could not validate hook input; retry with valid input.", file=sys.stderr)
        return 2
    if process_name_kill(command):
        print("Blocked process-name kill. Use launchctl for managed gateways or a verified PID for scratch work.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
