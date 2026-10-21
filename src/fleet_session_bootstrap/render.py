"""Bounded, data-only rendering for the session-start standing directive."""

from __future__ import annotations

from hashlib import sha256
import re

_POINTER = re.compile(r"(?:SOUL|personality|environment):[a-z0-9._-]{1,64}\Z")
_AVAILABILITY = frozenset({"available", "unavailable", "timeout"})


def render_pack(namespace_digest: str, availability: str, standing_pointers: tuple[str, ...]) -> str:
    """Render a compact Caveman-style directive without loading any source body."""
    if not re.fullmatch(r"[0-9a-f]{64}", namespace_digest):
        raise ValueError("namespace digest is invalid")
    if availability not in _AVAILABILITY or any(not _POINTER.fullmatch(pointer) for pointer in standing_pointers):
        raise ValueError("render input is not an approved redacted pointer")
    canary = sha256((namespace_digest + availability + "\0".join(standing_pointers)).encode()).hexdigest()[:16]
    pack = "\n".join((f"OPEN-GATE:{canary}", "style=caveman-full", *standing_pointers, f"source={availability}"))
    if len(pack.split()) > 800:
        raise ValueError("render budget exceeded")
    return pack
