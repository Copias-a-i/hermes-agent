"""Which route an explicit ``--provider`` pick of a custom endpoint ends on.

``/model <name> --provider custom`` and ``--provider custom:<entry>`` both reach the
credential step with configured ``custom_providers`` in play, and the two must not resolve the
same way: a named entry binds its own endpoint and key, while bare ``custom`` on a live
bare-custom session names that session and keeps its route across consecutive picks.

Split out of ``test_model_switch_custom_providers.py``, which covers catalog/picker behavior.
"""

import pytest

from hermes_cli.model_switch import switch_model


_MOCK_VALIDATION = {
    "accepted": True,
    "persist": True,
    "recognized": True,
    "message": None,
}


@pytest.fixture(autouse=True)
def _disable_live_custom_provider_model_probe(monkeypatch):
    """Keep these routing cases independent of local model servers."""
    monkeypatch.setattr("hermes_cli.models.fetch_api_models", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "hermes_cli.models.cached_provider_model_ids", lambda *_a, **_kw: []
    )
    monkeypatch.setattr(
        "hermes_cli.models.provider_model_ids", lambda *_a, **_kw: []
    )
    monkeypatch.setattr(
        "hermes_cli.models_local.fetch_ollama_local_models", lambda *_a, **_kw: None
    )


def test_switch_to_legacy_custom_provider_binds_configured_endpoint_and_key(monkeypatch):
    """An explicit legacy custom provider must not inherit the prior session's route."""
    monkeypatch.setattr("hermes_cli.models_validate.validate_requested_model", lambda *a, **k: _MOCK_VALIDATION)
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None)

    result = switch_model(
        raw_input="model-legacy",
        current_provider="custom",
        current_model="model-main",
        current_base_url="https://main.example.test/v1",
        current_api_key="main-key",
        explicit_provider="custom:legacy-host",
        user_providers={},
        custom_providers=[{
            "name": "legacy-host",
            "base_url": "https://legacy.example.test/v1",
            "api_key": "legacy-key",
            "model": "model-legacy",
        }],
    )

    assert result.success is True
    assert (result.target_provider, result.new_model) == ("custom:legacy-host", "model-legacy")
    assert (result.base_url, result.api_key) == ("https://legacy.example.test/v1", "legacy-key")


@pytest.mark.parametrize(
    ("current_provider", "current_base_url", "current_api_key", "expected_url", "expected_key"),
    [
        ("custom", "https://session.example.test/v1", "session-key", "https://session.example.test/v1", "session-key"),
        ("openrouter", "https://openrouter.example.test/v1", "openrouter-key", "https://legacy.example.test/v1", "legacy-key"),
    ],
)
def test_explicit_bare_custom_with_configured_entries_preserves_or_binds_route(
    monkeypatch, current_provider, current_base_url, current_api_key, expected_url, expected_key,
):
    """Bare custom preserves its live route, otherwise self-heals to a configured entry."""
    monkeypatch.setattr("hermes_cli.models_validate.validate_requested_model", lambda *a, **k: _MOCK_VALIDATION)
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None)

    result = switch_model(
        raw_input="model-legacy",
        current_provider=current_provider,
        current_model="model-main",
        current_base_url=current_base_url,
        current_api_key=current_api_key,
        explicit_provider="custom",
        user_providers={},
        custom_providers=[{
            "name": "legacy-host",
            "base_url": "https://legacy.example.test/v1",
            "api_key": "legacy-key",
            "model": "model-legacy",
        }],
    )

    assert result.success is True
    assert (result.base_url, result.api_key) == (expected_url, expected_key)
    assert result.target_provider == ("custom" if current_provider == "custom" else "custom:legacy-host")


def test_explicit_bare_custom_with_configured_entries_preserves_route_across_consecutive_picks(monkeypatch):
    """A live bare-custom session keeps its identity and route after applying a picker result."""
    monkeypatch.setattr("hermes_cli.models_validate.validate_requested_model", lambda *a, **k: _MOCK_VALIDATION)
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None)
    custom_providers = [{
        "name": "legacy-host",
        "base_url": "https://legacy.example.test/v1",
        "api_key": "legacy-key",
        "model": "model-legacy",
    }]

    first = switch_model(
        raw_input="model-legacy", current_provider="custom", current_model="model-main",
        current_base_url="https://session.example.test/v1", current_api_key="session-key",
        explicit_provider="custom", user_providers={}, custom_providers=custom_providers,
    )
    second = switch_model(
        raw_input="model-legacy", current_provider=first.target_provider, current_model=first.new_model,
        current_base_url=first.base_url, current_api_key=first.api_key,
        explicit_provider="custom", user_providers={}, custom_providers=custom_providers,
    )

    for result in (first, second):
        assert result.success is True
        assert result.target_provider == "custom"
        assert (result.base_url, result.api_key) == ("https://session.example.test/v1", "session-key")
