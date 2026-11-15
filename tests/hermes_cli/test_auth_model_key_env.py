"""Regression tests for #106336: Desktop-saved registry-provider credentials.

The Desktop settings UI persists a registry provider's API key as a credential
pointer — ``model.key_env`` in config.yaml → the env var in ``$HERMES_HOME/.env``
(e.g. ``HERMES_CUSTOM_LMSTUDIO_API_KEY``) — while keeping ``model.provider`` on
the registry id. Before the fix, ``_resolve_api_key_provider_secret`` consulted
only ``PROVIDER_REGISTRY[...].api_key_env_vars``, silently ignored the UI-saved
key, and lmstudio fell through to its no-auth placeholder (``dummy-lm-api-key``),
producing an opaque 401 against auth-enabled LM Studio servers.
"""

from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("LM_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_CUSTOM_LMSTUDIO_API_KEY", raising=False)
    return home


def _write(home: Path, config: str, env: str) -> None:
    (home / "config.yaml").write_text(config, encoding="utf-8")
    (home / ".env").write_text(env, encoding="utf-8")


def test_model_key_env_pointer_is_honored_for_registry_provider(hermes_home):
    _write(
        hermes_home,
        "model:\n  default: some-model\n  provider: lmstudio\n"
        "  base_url: http://127.0.0.1:1234/v1\n"
        "  key_env: HERMES_CUSTOM_LMSTUDIO_API_KEY\n",
        "HERMES_CUSTOM_LMSTUDIO_API_KEY=sk-lm-desktop-saved\n",
    )
    from hermes_cli.auth import resolve_api_key_provider_credentials

    creds = resolve_api_key_provider_credentials("lmstudio")
    assert creds["api_key"] == "sk-lm-desktop-saved"
    assert creds["source"] == "HERMES_CUSTOM_LMSTUDIO_API_KEY"


def test_model_key_env_does_not_leak_across_providers(hermes_home):
    # model targets a DIFFERENT provider: its key_env must not be consulted for
    # lmstudio, which falls back to its documented no-auth placeholder.
    _write(
        hermes_home,
        "model:\n  default: gpt-x\n  provider: openai\n"
        "  key_env: HERMES_CUSTOM_LMSTUDIO_API_KEY\n",
        "HERMES_CUSTOM_LMSTUDIO_API_KEY=sk-lm-should-not-leak\n",
    )
    from hermes_cli.auth import LMSTUDIO_NOAUTH_PLACEHOLDER, resolve_api_key_provider_credentials

    creds = resolve_api_key_provider_credentials("lmstudio")
    assert creds["api_key"] == LMSTUDIO_NOAUTH_PLACEHOLDER


_COMMANDCODE_KEY_ENV = "HERMES_CUSTOM_COMMANDCODE_API_KEY"


def _commandcode_home(home: Path, monkeypatch, provider_id: str, aliases: str, *, key_env: bool = True) -> None:
    """Desktop-saved CommandCode key (``model.key_env``) beside the given alias lines in ``.env``."""
    for name in ("COMMANDCODE_API_KEY", "CMD_API_KEY", _COMMANDCODE_KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    pointer = f"  key_env: {_COMMANDCODE_KEY_ENV}\n" if key_env else ""
    _write(
        home,
        f"model:\n  default: some-model\n  provider: {provider_id}\n{pointer}",
        f"{_COMMANDCODE_KEY_ENV}=sk-desktop-saved\n{aliases}",
    )


@pytest.mark.parametrize("provider_id", ["commandcode", "commandcode-anthropic"])
def test_model_key_env_cannot_mask_conflicting_commandcode_aliases(hermes_home, monkeypatch, provider_id):
    """Conflicting COMMANDCODE_API_KEY / CMD_API_KEY values fail closed even when model.key_env
    names a usable key: the pointer used to return first, so the conflict was never evaluated."""
    _commandcode_home(
        hermes_home, monkeypatch, provider_id, "COMMANDCODE_API_KEY=canonical-secret\nCMD_API_KEY=legacy-secret\n"
    )
    from hermes_cli.auth import resolve_api_key_provider_credentials
    from hermes_cli.providers import ProviderCredentialConflictError

    with pytest.raises(ProviderCredentialConflictError, match="CommandCode API key environment variables disagree") as exc:
        resolve_api_key_provider_credentials(provider_id)
    for value in ("sk-desktop-saved", "canonical-secret", "legacy-secret"):
        assert value not in str(exc.value)


@pytest.mark.parametrize("provider_id", ["commandcode", "commandcode-anthropic"])
@pytest.mark.parametrize(
    "aliases",
    [
        "COMMANDCODE_API_KEY=same-secret\nCMD_API_KEY=same-secret\n",
        "COMMANDCODE_API_KEY=canonical-secret\n",
        "CMD_API_KEY=legacy-secret\n",
        "",
    ],
    ids=["identical-aliases", "canonical-only", "legacy-only", "no-aliases"],
)
def test_model_key_env_still_wins_when_commandcode_aliases_do_not_conflict(hermes_home, monkeypatch, provider_id, aliases):
    _commandcode_home(hermes_home, monkeypatch, provider_id, aliases)
    from hermes_cli.auth import resolve_api_key_provider_credentials

    creds = resolve_api_key_provider_credentials(provider_id)
    assert creds["api_key"] == "sk-desktop-saved"
    assert creds["source"] == _COMMANDCODE_KEY_ENV


@pytest.mark.parametrize("provider_id", ["commandcode", "commandcode-anthropic"])
def test_conflicting_commandcode_aliases_fail_closed_without_model_key_env(hermes_home, monkeypatch, provider_id):
    _commandcode_home(
        hermes_home, monkeypatch, provider_id,
        "COMMANDCODE_API_KEY=canonical-secret\nCMD_API_KEY=legacy-secret\n", key_env=False,
    )
    from hermes_cli.auth import resolve_api_key_provider_credentials
    from hermes_cli.providers import ProviderCredentialConflictError

    with pytest.raises(ProviderCredentialConflictError):
        resolve_api_key_provider_credentials(provider_id)
