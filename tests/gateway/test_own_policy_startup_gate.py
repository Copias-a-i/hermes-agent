"""Regression tests for own-policy open startup gate in gateway/run.py."""

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner


def _open_yuanbao_runner(tmp_path):
    config = GatewayConfig(
        platforms={
            Platform.YUANBAO: PlatformConfig(
                enabled=True,
                extra={"dm_policy": "open"},
            ),
        },
        sessions_dir=tmp_path / "sessions",
    )
    return GatewayRunner(config)


@pytest.mark.asyncio
async def test_unrelated_allow_all_does_not_bypass_yuanbao_open_gate(
    monkeypatch, tmp_path,
):
    """TELEGRAM_ALLOW_ALL_USERS must not satisfy Yuanbao's open-policy opt-in."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("YUANBAO_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")

    runner = _open_yuanbao_runner(tmp_path)

    ok = await runner.start()

    assert ok is True
    assert runner.should_exit_cleanly is True
    assert "yuanbao" in (runner.exit_reason or "").lower()


@pytest.mark.asyncio
async def test_open_policy_refusal_exits_with_the_fatal_config_code(monkeypatch, tmp_path):
    """A refused open policy exits 78 (EX_CONFIG), never 0.

    A supervisor restarts a gateway that exits 0 (the stderr_timestamp wrapper reports 0 as 75), so
    a policy refusal that exited 0 would crash-loop until someone edits the config; 78 parks it.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("YUANBAO_ALLOW_ALL_USERS", raising=False)

    runner = _open_yuanbao_runner(tmp_path)

    assert await runner.start() is True

    assert runner.exit_code == 78
