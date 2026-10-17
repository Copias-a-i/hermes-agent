"""Configuration contract tests for fleet session lifecycle."""

import os
from unittest.mock import patch

from hermes_cli.config import _validate_config_key, load_config
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_fleet_lifecycle_outbox_is_generic_and_disabled_by_default(tmp_path):
    """The lifecycle plugin exposes only its durable vendor-neutral adapter seam."""
    assert DEFAULT_CONFIG["fleet_session_lifecycle"]["outbox_command"] == []
    assert _validate_config_key("fleet_session_lifecycle.outbox_command") == (True, None)
    assert DEFAULT_CONFIG["fleet_session_lifecycle"]["finalize_lease_wait_seconds"] == 2
    assert _validate_config_key("fleet_session_lifecycle.finalize_lease_wait_seconds") == (True, None)
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        loaded = load_config()["fleet_session_lifecycle"]
        assert loaded["outbox_timeout_seconds"] == 5
        assert loaded["finalize_lease_wait_seconds"] == 2
