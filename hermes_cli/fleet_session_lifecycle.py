"""Profile-local installation markers for the bundled session lifecycle plugin.

The plugin itself is a bundled backend and therefore loads for every profile
without a user configuration opt-in.  These markers make that inheritance
explicit, migrate profile layouts created before the plugin, and never touch a
vault, scheduler, gbrain, or launchd.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

_MARKER_NAME = "fleet-session-lifecycle-v1"
_MARKER_CONTENT = "fleet-session-lifecycle.v1\n"


def install_profile_lifecycle(profile_dir: Path) -> bool:
    """Idempotently mark one profile ready for the bundled lifecycle plugin."""
    marker = Path(profile_dir) / "state" / _MARKER_NAME
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        if marker.is_file() and marker.read_text(encoding="utf-8-sig") == _MARKER_CONTENT:
            return False
        marker.write_text(_MARKER_CONTENT, encoding="utf-8")
        return True
    except OSError:
        return False


def install_profile_lifecycles(profile_dirs: Iterable[Path]) -> list[Path]:
    """Best-effort independent migration for existing profile directories."""
    installed: list[Path] = []
    for profile_dir in profile_dirs:
        if install_profile_lifecycle(profile_dir):
            installed.append(Path(profile_dir))
    return installed
