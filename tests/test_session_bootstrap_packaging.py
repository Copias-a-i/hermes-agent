"""COP-752 packaging contract for the canonical bootstrap launcher."""
from __future__ import annotations

import tomllib
from pathlib import Path


def test_session_bootstrap_is_a_normal_package_and_console_entrypoint() -> None:
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    include = project["tool"]["setuptools"]["packages"]["find"]["include"]

    assert "fleet_session_bootstrap" in include
    assert "fleet_session_bootstrap.*" in include
    assert project["project"]["scripts"]["fleet-session-bootstrap"] == "fleet_session_bootstrap.cli:main"
