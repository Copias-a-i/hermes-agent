"""Offline, fail-open command boundary for fleet session bootstrap."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from hashlib import sha256
from pathlib import Path
from time import time

from .cache import ReceiptStore
from .contract import BootstrapRequest, TrustedIdentity, objective_digest, validate_cwd, validated_partition
from .render import render_pack

_MAX_INPUT_BYTES = 16_384


class _FailOpenParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _unavailable() -> None:
    pack = render_pack(sha256(b"unavailable").hexdigest(), "unavailable", ())
    print(json.dumps({"availability": "unavailable", "pack": pack}, separators=(",", ":")))


def _load_identity(path: Path) -> TrustedIdentity:
    if path.is_symlink() or not path.is_file():
        raise ValueError("trusted identity file is invalid")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("trusted identity file must be mode 0600")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"tenant", "profile", "runtime", "host_or_project", "salt"}:
        raise ValueError("trusted identity schema is invalid")
    return TrustedIdentity(**raw)


def _read_request() -> dict[str, object]:
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("request is too large")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request is invalid")
    return value


def main() -> int:
    parser = _FailOpenParser(add_help=False)
    parser.add_argument("--trusted-identity", type=Path, required=True)
    parser.add_argument("--approved-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    try:
        options = parser.parse_args()
        raw = _read_request()
        request = BootstrapRequest(tenant=raw["tenant"], profile=raw["profile"], runtime=raw["runtime"], host_or_project=raw["host"], session_id=raw["session_id"], cwd=raw["cwd"], objective=raw["objective"])
        if not isinstance(request.objective, str) or len(request.objective) > 1024:
            raise ValueError("objective is invalid")
        objective_digest(request.objective)
        partition = validated_partition(request, _load_identity(options.trusted_identity))
        validate_cwd(request.cwd, options.approved_root)
        ReceiptStore(options.state_root).write(request.session_id, {"cache_state": "unavailable", "manifest_version": "v1", "availability": "unavailable", "count": 0, "elapsed_ms": 0, "timestamp": int(time()), "error": "none", "namespace_digest": sha256(partition.namespace.encode()).hexdigest()})
        pack = render_pack(sha256(partition.namespace.encode()).hexdigest(), "unavailable", ())
        print(json.dumps({"availability": "unavailable", "pack": pack}, separators=(",", ":")))
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        _unavailable()
    return 0
