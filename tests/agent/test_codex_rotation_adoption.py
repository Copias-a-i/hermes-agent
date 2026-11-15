"""A Codex token rotated by another process (same account) is adopted on a 401, not skipped (COP-710).

The Codex CLI and every Hermes profile share ONE single-use rotating refresh-token chain. When the CLI or a
sibling rotates it, the singleton read inside ``_try_refresh_codex_client_credentials`` already holds the newer
tokens, so the "singleton differs from the active api_key" guard fired and the 401'd request was never retried
(cron job ``memory-dreaming-promotion`` failed with 401 ``token_expired`` / ``refresh_token_reused``). The guard
exists to stop a swap to a DIFFERENT account; same ``(chatgpt_account_id, sub)`` proves it is not one, and an
unknown identity still fails closed.

Hermetic: synthetic unsigned JWTs, a stand-in agent that runs the real method, no auth store and no Codex CLI
home (``CODEX_HOME`` is pointed at an empty temp dir as a tripwire).
"""
from __future__ import annotations

import base64
import json
import logging

import pytest

from agent.client_lifecycle import ClientLifecycleMixin

CODEX_URL = "https://chatgpt.com/backend-api/codex"
XAI_URL = "https://api.x.ai/v1"
ACCOUNT_A, ACCOUNT_B = "acct-a", "acct-b"
SUB_1, SUB_2 = "user-1", "user-2"


def _jwt(account_id, sub, serial):
    """Unsigned ``header.payload.sig`` carrying the claims Hermes reads; ``serial`` makes tokens distinct."""
    def _b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"serial": serial}
    if sub is not None:
        claims["sub"] = sub
    if account_id is not None:
        claims["https://api.openai.com/auth"] = {"chatgpt_account_id": account_id}
    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}.sig"


class _Agent(ClientLifecycleMixin):
    """Runs the real ``_try_refresh_codex_client_credentials`` / ``_adopt_openai_credentials`` plumbing."""

    api_mode = "codex_responses"
    model = "gpt-5-codex"

    def __init__(self, *, api_key, provider="openai-codex", base_url=CODEX_URL):
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        self._client_kwargs = {"api_key": api_key, "base_url": base_url}
        self.client = object()
        self.built_with = []  # api_key of every client the agent (re)built
        self.retired = []

    def _create_openai_client(self, client_kwargs, *, reason, shared):
        self.built_with.append(client_kwargs["api_key"])
        return object()

    def _retire_shared_openai_client(self, client, *, reason):
        self.retired.append(client)


class _Resolver:
    """Stands in for ``resolve_*_runtime_credentials``: the guard read vs. a forced refresh."""

    def __init__(self, singleton, refreshed=None):
        self.singleton, self.refreshed = singleton, refreshed
        self.reads = self.forced = 0

    def __call__(self, force_refresh=False, refresh_if_expiring=True, **_):
        if force_refresh:
            self.forced += 1
            return self.refreshed
        self.reads += 1
        return self.singleton


@pytest.fixture(autouse=True)
def _no_codex_cli_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home-tripwire"))


def _resolver(monkeypatch, singleton, refreshed=None, name="resolve_codex_runtime_credentials"):
    resolver = _Resolver(singleton, refreshed)
    monkeypatch.setattr(f"hermes_cli.auth.{name}", resolver)
    return resolver


def _assert_nothing_adopted(agent, resolver, old_key):
    assert resolver.forced == 0, "a forced refresh would spend the single-use refresh token of another account"
    assert agent.api_key == old_key
    assert agent.built_with == [] and agent.retired == []


def test_same_principal_rotated_token_is_adopted_without_a_forced_refresh(monkeypatch, caplog):
    old, new = _jwt(ACCOUNT_A, SUB_1, 1), _jwt(ACCOUNT_A, SUB_1, 2)
    agent = _Agent(api_key=old)
    old_client = agent.client
    resolver = _resolver(monkeypatch, {"api_key": new, "base_url": CODEX_URL})

    with caplog.at_level(logging.INFO, logger="run_agent"):
        ok = agent._try_refresh_codex_client_credentials(force=True)

    assert ok is True
    # The singleton read already adopted the rotated chain; forcing again would burn another single-use rotation.
    assert resolver.forced == 0
    assert agent.api_key == new and agent._client_kwargs["api_key"] == new
    assert agent.built_with == [new]
    assert agent.retired == [old_client]
    adopted = [r for r in caplog.records if r.levelno == logging.INFO and "adopting" in r.getMessage()]
    assert len(adopted) == 1
    assert all(old not in r.getMessage() and new not in r.getMessage() for r in caplog.records)


def test_different_chatgpt_account_is_not_adopted(monkeypatch, caplog):
    old, other = _jwt(ACCOUNT_A, SUB_1, 1), _jwt(ACCOUNT_B, SUB_1, 2)
    agent = _Agent(api_key=old)
    resolver = _resolver(monkeypatch, {"api_key": other, "base_url": CODEX_URL})

    with caplog.at_level(logging.DEBUG, logger="run_agent"):
        ok = agent._try_refresh_codex_client_credentials(force=True)

    assert ok is False
    _assert_nothing_adopted(agent, resolver, old)
    assert any("silent account swap" in r.getMessage() for r in caplog.records)


def test_same_account_different_subject_is_not_adopted(monkeypatch):
    # Members of one ChatGPT workspace share chatgpt_account_id but have their own subjects and quotas.
    old, other = _jwt(ACCOUNT_A, SUB_1, 1), _jwt(ACCOUNT_A, SUB_2, 2)
    agent = _Agent(api_key=old)
    resolver = _resolver(monkeypatch, {"api_key": other, "base_url": CODEX_URL})

    assert agent._try_refresh_codex_client_credentials(force=True) is False
    _assert_nothing_adopted(agent, resolver, old)


@pytest.mark.parametrize(
    "old, singleton_key",
    [
        pytest.param("opaque-active-key", _jwt(ACCOUNT_A, SUB_1, 2), id="active-key-opaque"),
        pytest.param(_jwt(ACCOUNT_A, SUB_1, 1), "opaque-rotated-token", id="singleton-token-opaque"),
        pytest.param("opaque-one", "opaque-two", id="both-opaque"),
        pytest.param(_jwt(ACCOUNT_A, None, 1), _jwt(ACCOUNT_A, SUB_1, 2), id="active-key-missing-sub"),
        pytest.param(_jwt(None, SUB_1, 1), _jwt(ACCOUNT_A, SUB_1, 2), id="active-key-missing-account"),
        pytest.param(_jwt(ACCOUNT_A, SUB_1, 1), _jwt(ACCOUNT_A, None, 2), id="singleton-missing-sub"),
    ],
)
def test_unknown_identity_fails_closed(monkeypatch, old, singleton_key):
    agent = _Agent(api_key=old)
    resolver = _resolver(monkeypatch, {"api_key": singleton_key, "base_url": CODEX_URL})

    assert agent._try_refresh_codex_client_credentials(force=True) is False
    _assert_nothing_adopted(agent, resolver, old)


def test_same_principal_without_a_usable_base_url_adopts_nothing(monkeypatch):
    old, new = _jwt(ACCOUNT_A, SUB_1, 1), _jwt(ACCOUNT_A, SUB_1, 2)
    agent = _Agent(api_key=old)
    resolver = _resolver(monkeypatch, {"api_key": new, "base_url": ""})

    assert agent._try_refresh_codex_client_credentials(force=True) is False
    _assert_nothing_adopted(agent, resolver, old)


def test_unchanged_singleton_still_takes_the_forced_refresh_path(monkeypatch):
    old, new = _jwt(ACCOUNT_A, SUB_1, 1), _jwt(ACCOUNT_A, SUB_1, 2)
    agent = _Agent(api_key=old)
    resolver = _resolver(
        monkeypatch,
        singleton={"api_key": old, "base_url": CODEX_URL},
        refreshed={"api_key": new, "base_url": CODEX_URL},
    )

    assert agent._try_refresh_codex_client_credentials(force=True) is True
    assert resolver.forced == 1
    assert agent.api_key == new and agent.built_with == [new]


def test_unchanged_singleton_whose_refresh_returns_the_same_token_is_not_adopted(monkeypatch):
    old = _jwt(ACCOUNT_A, SUB_1, 1)
    agent = _Agent(api_key=old)
    resolver = _resolver(
        monkeypatch,
        singleton={"api_key": old, "base_url": CODEX_URL},
        refreshed={"api_key": old, "base_url": CODEX_URL},
    )

    assert agent._try_refresh_codex_client_credentials(force=True) is False
    assert resolver.forced == 1
    assert agent.built_with == [] and agent.retired == []


def test_xai_oauth_never_adopts_a_differing_singleton_even_with_matching_claims(monkeypatch):
    # The same claims that prove one principal for openai-codex must not unlock the xai-oauth path.
    old, new = _jwt(ACCOUNT_A, SUB_1, 1), _jwt(ACCOUNT_A, SUB_1, 2)
    agent = _Agent(api_key=old, provider="xai-oauth", base_url=XAI_URL)
    resolver = _resolver(
        monkeypatch,
        singleton={"api_key": new, "base_url": XAI_URL},
        refreshed={"api_key": new, "base_url": XAI_URL},
        name="resolve_xai_oauth_runtime_credentials",
    )

    assert agent._try_refresh_codex_client_credentials(force=True) is False
    _assert_nothing_adopted(agent, resolver, old)
