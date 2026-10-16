"""Builder for the auxiliary-task model blocks in ``config_defaults.DEFAULT_CONFIG``.

``config_defaults.py`` is the user-facing documentation of config.yaml — its comments are
what users read — so code helpers that build repeated blocks live in this sibling instead.
"""

from __future__ import annotations


def aux_block(timeout, *, reasoning_effort=True, **extra):
    """Standard auxiliary-task model block (see DEFAULT_CONFIG["auxiliary"]).

    reasoning_effort=False omits that key (MoA blocks configure depth per slot);
    ``extra`` keys are appended after the standard ones.
    """
    d = {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": timeout, "extra_body": {}}
    if reasoning_effort:
        d["reasoning_effort"] = ""
    d.update(extra)
    return d
