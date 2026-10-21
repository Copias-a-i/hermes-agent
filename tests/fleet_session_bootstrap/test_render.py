"""COP-752 compact standing-style rendering contract."""

from __future__ import annotations


import pytest

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


def test_renderer_rejects_oversized_serialized_pointer_pack() -> None:
    from fleet_session_bootstrap.render import render_pack

    pointers = tuple(f"SOUL:{index:064d}" for index in range(100))
    with pytest.raises(ValueError, match="budget"):
        render_pack("a" * 64, "available", pointers)
