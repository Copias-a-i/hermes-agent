"""Telegram adapter contracts carried until now as a fleet-local patch (0001).

1. An exhausted getUpdates-conflict ladder is a RETRYABLE fatal, so the runner's reconnect
   watcher rebuilds the adapter instead of leaving Telegram dead until a manual restart.
2. ``/reload-skills`` refreshes the Telegram BotCommands menu through ``refresh_skill_group``.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_exhausted_polling_conflict_is_retryable_fatal():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    fatal_handler = AsyncMock()
    adapter.set_fatal_error_handler(fatal_handler)
    adapter._polling_conflict_count = 5  # MAX_CONFLICT_RETRIES: the next conflict is the fatal one

    conflict = type("Conflict", (Exception,), {})
    await adapter._handle_polling_conflict(conflict("Conflict: terminated by other getUpdates request"))

    assert adapter.fatal_error_code == "telegram_polling_conflict"
    assert adapter.fatal_error_retryable is True
    fatal_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_reload_skills_reregisters_telegram_command_menu(monkeypatch):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = SimpleNamespace(set_my_commands=AsyncMock())
    menus = iter([[("alpha", "first skill")], [("alpha", "first skill"), ("beta", "new skill")]])
    monkeypatch.setattr(
        "hermes_cli.commands_platforms.telegram_menu_commands",
        lambda max_commands: (next(menus), 0),
    )
    # Keep the (name, description) rows as-is: the suite may stub the telegram package.
    monkeypatch.setattr(adapter, "_bot_commands", lambda rows: list(rows))

    await adapter._register_command_menu()
    adapter._bot.set_my_commands.reset_mock()

    # The gateway's /reload-skills orchestrator calls refresh_skill_group on every adapter.
    refresh = getattr(adapter, "refresh_skill_group", None)
    assert callable(refresh)
    result = refresh()
    if asyncio.iscoroutine(result):
        await result

    assert adapter._bot.set_my_commands.await_count == 3  # default, private and group scopes
    registered = adapter._bot.set_my_commands.await_args.args[0]
    assert [name for name, _ in registered] == ["alpha", "beta"]
