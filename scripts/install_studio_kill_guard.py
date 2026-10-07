#!/usr/bin/env python3
"""Extend Studio's already-loaded Bash hook, preserving its production-deploy gate."""
import argparse
import json
from pathlib import Path
import shlex
import shutil
import sys

MARKER = "# Studio process-name kill guard (COP-699)"
INPUT_LINE = 'INPUT="$(cat || true)"'


def install(hook: Path, destination: Path, backup: Path, python: str) -> None:
    source = Path(__file__).with_name("fleet_kill_guard.py")
    original = hook.read_text()
    if INPUT_LINE not in original:
        raise ValueError("Existing Bash hook does not have the expected stdin capture; left untouched")
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(hook, backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if MARKER not in original:
        guard = (
            f"\n{MARKER}\n"
            f"printf '%s' \"$INPUT\" | {shlex.quote(python)} {shlex.quote(str(destination))}\n"
            "KILL_GUARD_STATUS=$?\n"
            "if [ \"$KILL_GUARD_STATUS\" -ne 0 ]; then\n"
            "  exit \"$KILL_GUARD_STATUS\"\n"
            "fi\n"
        )
        hook.write_text(original.replace(INPUT_LINE, INPUT_LINE + guard, 1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook", type=Path, default=Path.home() / ".claude/hooks/block-vercel-prod.sh")
    parser.add_argument("--destination", type=Path, default=Path.home() / ".claude/hooks/fleet-kill-guard.py")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--python", default="/usr/bin/python3")
    args = parser.parse_args()
    install(args.hook, args.destination, args.backup, args.python)
    print(json.dumps({"hook": str(args.hook), "guard": str(args.destination), "backup": str(args.backup)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
