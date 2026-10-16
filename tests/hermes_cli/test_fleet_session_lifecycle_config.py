"""Configuration contract tests for fleet session lifecycle."""

import os
from unittest.mock import patch

from hermes_cli.config import _validate_config_key, load_config
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_fleet_lifecycle_retry_window_is_schema_default_and_loader_value(tmp_path):
    """The plugin retry window is configurable through the supported config seam."""
    assert DEFAULT_CONFIG["fleet_session_lifecycle"]["gbrain_retry_seconds"] == 300
    assert _validate_config_key("fleet_session_lifecycle.gbrain_retry_seconds") == (True, None)
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        assert load_config()["fleet_session_lifecycle"]["gbrain_retry_seconds"] == 300
