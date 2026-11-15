#!/usr/bin/env python3
"""Claude Bash pre-tool guard: stop by service or verified PID, never process name.

This prevents accidental fleet-wide pkill/killall, not arbitrary-code execution.
It does not execute or log the proposed command and needs no credentials.

Commands wrapped in sudo, timeout, env, nice, ... are followed to the command they run, through
the options and operands each wrapper consumes. An option this guard cannot parse is judged by the
names in the rest of the command, so an unparsed wrapper errs toward blocking.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import sys

_KILLERS = frozenset({"pkill", "killall"})
# macOS volumes are case-insensitive, so `PKILL` runs /usr/bin/pkill there.
_KILLER_WORD = re.compile(r"\b(?:pkill|killall)\b", re.IGNORECASE)
# Shells (and `su`) that run the string after `-c` as a command line of their own.
_COMMAND_STRING_RUNNERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "su", "runuser"})
_SEPARATORS = ";&|()\n"
# Reserved words that run the command written after them.
_COMPOUND_PREFIXES = frozenset({"!", "{", "}", "if", "then", "elif", "else", "do", "while", "until"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class _Wrapper:
    """The argv a command prefix consumes before the command it runs.

    An option missing from these lists is one this guard cannot parse, so a segment that uses it
    is judged by the names it contains (fail closed) instead of by a guessed command position.
    """

    flags: str = ""  # short options without a value
    value: str = ""  # short options with a value: attached to the option, else the next word
    long_flags: str = ""  # space-separated long options without a value
    long_value: str = ""  # long options with a value: `--opt=value`, else the next word
    operands: int = 0  # bare operands that precede the command (`timeout DURATION`)
    assignments: bool = False  # NAME=value words may precede the command
    numeric: bool = False  # `-10` style options (nice)


_TIMEOUT = _Wrapper(
    flags="fpv", value="ks", long_flags="foreground preserve-status verbose",
    long_value="kill-after signal", operands=1,
)
_WRAPPERS = {
    "sudo": _Wrapper(
        flags="AbBEeiKklnPSsVv", value="CDgpRrTtUu", assignments=True,
        long_flags="askpass background bell edit help list login non-interactive preserve-env "
        "preserve-groups remove-timestamp reset-timestamp set-home shell stdin validate version",
        long_value="chdir chroot close-from command-timeout group host other-user prompt role type user",
    ),
    "doas": _Wrapper(flags="ns", value="uC"),
    "env": _Wrapper(
        flags="i0v", value="uC", long_flags="ignore-environment null debug", long_value="unset chdir",
        assignments=True,
    ),
    "timeout": _TIMEOUT,
    "gtimeout": _TIMEOUT,
    "nice": _Wrapper(value="n", long_value="adjustment", numeric=True),
    "ionice": _Wrapper(
        flags="t", value="cnpPu", long_flags="ignore", long_value="class classdata pid pgid uid",
    ),
    "nohup": _Wrapper(),
    "arch": _Wrapper(),
    "command": _Wrapper(flags="pvV"),
    "exec": _Wrapper(flags="cl", value="a"),
    "time": _Wrapper(flags="pl", value="o"),
    "setsid": _Wrapper(flags="cfw", long_flags="ctty fork wait"),
    "stdbuf": _Wrapper(value="ioe", long_value="input output error"),
    "xargs": _Wrapper(
        flags="0oprtx", value="aEIJLnPRSsd", long_flags="null no-run-if-empty open-tty interactive verbose exit",
        long_value="arg-file delimiter max-args max-chars max-procs process-slot-var",
    ),
}


def _command_index(wrapper: _Wrapper, args: list[str]) -> int | None:
    """Index in ``args`` of the command the wrapper runs; ``None`` when an option is not understood."""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        token = args[index]
        index += 1
        if token == "--":
            break
        if token == "-":
            return None
        if token.startswith("--"):
            name, equals, _value = token[2:].partition("=")
            if name in wrapper.long_value.split():
                index += 0 if equals else 1
            elif name not in wrapper.long_flags.split():
                return None
            continue
        for position, letter in enumerate(token[1:], 1):
            if letter in wrapper.value:
                index += 1 if position == len(token) - 1 else 0
                break
            if letter not in wrapper.flags and not (wrapper.numeric and letter.isdigit()):
                return None
    index += wrapper.operands
    while wrapper.assignments and index < len(args) and _ASSIGNMENT.match(args[index]):
        index += 1
    return index


def _runs_killer_string(args: list[str]) -> bool:
    """`sh -c STRING`: the string is a command line of its own."""
    return any(
        token.startswith("-") and "c" in token and process_name_kill(args[index + 1])
        for index, token in enumerate(args[:-1])
    )


def _segment_kills(segment: list[str]) -> bool:
    """One simple command (words between separators): does it run pkill/killall?"""
    words = list(segment)
    while words:
        head = words[0]
        name = Path(head).name.lower()
        if head == "$" or head in _COMPOUND_PREFIXES or _ASSIGNMENT.match(head):
            words = words[1:]
        elif name in _KILLERS:
            return True
        elif name == "eval":
            return process_name_kill(" ".join(words[1:]))
        elif name in _COMMAND_STRING_RUNNERS:
            return _runs_killer_string(words[1:])
        elif name in _WRAPPERS:
            start = _command_index(_WRAPPERS[name], words[1:])
            if start is None:
                return bool(_KILLER_WORD.search(" ".join(words[1:])))
            words = words[1 + start:]
        else:
            return False
    return False


def process_name_kill(command: str) -> bool:
    # Inspect command substitutions separately; shlex otherwise keeps quoted
    # substitutions inside an argument to a harmless command such as echo.
    for substitution in re.findall(r"\$\(([^()]*)\)", command):
        if process_name_kill(substitution):
            return True
    # The shell deletes a backslash-newline pair outright, so `p\<newline>kill` runs the killer.
    # Every other newline separates commands, and a comment ends at its newline, not before it,
    # so shlex must not consume that newline.
    lexer = shlex.shlex(command.replace("\\\n", ""), posix=True, punctuation_chars=_SEPARATORS)
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        # A malformed shell command carrying these names is not safe to admit.
        return bool(_KILLER_WORD.search(command))
    segments = [[]]
    for token in tokens:
        if token and all(character in _SEPARATORS for character in token):
            segments.append([])
        else:
            segments[-1].append(token)
    return any(_segment_kills(segment) for segment in segments)


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
