"""COP-752 compact standing-style rendering contract."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


def test_renderer_emits_only_compact_pointers_with_budgeted_canary() -> None:
    from fleet_session_bootstrap.render import render_pack

    rendered = render_pack(
        namespace_digest="a" * 64,
        availability="unavailable",
        standing_pointers=("SOUL:default", "personality:calm", "environment:studio"),
    )

    assert rendered.startswith("OPEN-GATE:")
    assert "style=caveman-full" in rendered
    assert "SOUL:default" in rendered
    assert len(rendered.split()) <= 800
    assert "SKILL.md" not in rendered


@pytest.mark.parametrize("marker", ["person@example.com", "role=admin", "profile=jarvis", "secret-token"])
def test_renderer_rejects_identity_and_pii_markers(marker: str) -> None:
    from fleet_session_bootstrap.render import render_pack

    with pytest.raises(ValueError):
        render_pack("a" * 64, "available", (marker,))
