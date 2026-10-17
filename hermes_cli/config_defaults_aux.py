"""Builders for repeated blocks in ``config_defaults``.

``config_defaults.py`` is the user-facing documentation of config.yaml — its comments are
what users read — so code helpers that build repeated blocks live in this sibling instead.
"""
from __future__ import annotations


def aux_block(timeout, *, reasoning_effort=True, **extra):
    """Standard auxiliary-task model block (see DEFAULT_CONFIG["auxiliary"])."""
    d = {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": timeout, "extra_body": {}}
    if reasoning_effort:
        d["reasoning_effort"] = ""
    d.update(extra)
    return d


def env(description, prompt, **keys):
    """Build one OPTIONAL_ENV_VARS entry preserving keyword order."""
    return {"description": description, "prompt": prompt, **keys}


_OMIT = object()


def category(category, password, advanced):
    """Build an OPTIONAL_ENV_VARS entry factory for one category."""
    def make(description, prompt, url=_OMIT, *, help=_OMIT, tools=_OMIT, password=password,
             advanced=advanced):
        d = {"description": description, "prompt": prompt}
        d.update((k, v) for k, v in (("help", help), ("url", url), ("tools", tools)) if v is not _OMIT)
        if password is not None:
            d["password"] = password
        d["category"] = category
        if advanced:
            d["advanced"] = True
        return d
    return make


def base_url(name, prompt_name=None):
    """Build a provider ``*_BASE_URL`` override entry."""
    prompt = f"{prompt_name or name} base URL (leave empty for default)"
    return provider_entry(f"{name} base URL override", prompt, None, password=False)


provider_entry = category("provider", password=True, advanced=True)
tool_entry = category("tool", password=True, advanced=False)
message_entry = category("messaging", password=False, advanced=False)
skill_entry = category("skill", password=True, advanced=True)
setting_entry = category("setting", password=False, advanced=False)
