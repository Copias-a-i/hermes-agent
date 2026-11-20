"""Built-in first-request startup policy (CSR-1).

Claimed by the turn prologue at the ``pre_llm_call`` seam, immediately before
``hermes_cli.lifecycle.invoke_hook`` runs. It is deliberately NOT a hook: plugin hooks are
registered per profile, a hook that raises contributes nothing, and the hook call is skipped
entirely for a persist-disabled agent — so no hook registration can reach every session.
Core sits above all of that, which is why a Kanban worker, the Desktop surface and a
delegated subagent are covered without a hook being installed anywhere. A delegated child is
its own agent, running this same prologue under its own profile home.

The generic loop carries the claimed block inside the turn's composed user content. The
codex app-server runtime is handed the bare user message instead, so its dispatch attaches
the block explicitly (``apply_policy_to_user_input``).

The policy is a compact paraphrase bound to the SHA256 of its canonical source skills.
Source bodies are hashed, never read into the prompt. Missing or unreadable sources fail
soft to the embedded text and report a non-secret ``caveman=/frontier=`` status.

Idempotence is decided from core-owned state only — the claims an agent instance carries,
keyed by ``(profile home, session id)``. Nothing is process-global, so one agent can never
suppress another that happens to share its session id. Transcript text is never consulted: a
user, web page or tool result that contains the marker must not be able to suppress or forge
the policy. A marker a profile-local hook (the transitional CSR-1 Jarvis hook) emitted is
stripped from the hook context, so exactly one marker ever reaches the model.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_constants import get_hermes_home, hermes_home_key

logger = logging.getLogger(__name__)

POLICY_VERSION = "CSR-1"
STARTUP_POLICY_MARKER_PREFIX = f"[{POLICY_VERSION}"

# Hard ceiling on what the injector may add to a turn, in both units the contract names. A
# payload over either budget is a bug; the builder degrades to the compact text and then to
# nothing rather than inflate the first request of every session on the fleet.
MAX_POLICY_BYTES = 1024
MAX_POLICY_TOKENS = 300

# Canonical source skills, by the slugs each is published under.
_CAVEMAN_SLUGS = ("caveman",)
_FRONTIER_SLUGS = ("super-frontier-routing", "super-frontier")

# The canonical sources this policy paraphrases, as the full SHA256 each was published under.
# A resolved source whose bytes hash to anything else is reported ``modified``: the text below
# is still correct policy, but it is no longer provably the paraphrase of THAT file, and the
# marker carries the live digest so the discrepancy is visible in-band.
_CANONICAL_DIGESTS = {
    "caveman": "daf9cec496ebd039809d8236f99f17fa1b4beaadf8ce4e2d532d0da51d70afce",
    "frontier": "a5cea079cee6cb882181b8eaa0cb33c2bee75d68c1d43fa889d481f6dba59be3",
}
_DIGEST_PREFIX_LEN = 12  # what the marker shows; the comparison above is on the full digest

# Guards the per-agent claim sets below. It holds no claim state of its own.
_CLAIM_LOCK = threading.Lock()
# Attribute on the agent instance holding the ``(profile home, session id)`` pairs it has
# already sent the policy for.
_CLAIM_ATTR = "_startup_policy_claims"

_DIGEST_LOCK = threading.Lock()
# path -> (stat signature, digest). Hashed once per process and re-hashed only when the
# file's (mtime_ns, size) changes, so an edited skill rebinds the marker.
_DIGEST_CACHE: dict[str, tuple[tuple[int, int], str]] = {}

_LOGGED_STATUS: set[str] = set()

def _skill_roots() -> tuple[Path, ...]:
    """Skill roots searched for the canonical sources, in precedence order.

    The active profile's own skills directory wins, then the checkout's bundled skills, then
    the sibling agent runtimes this fleet installs the same skills under. Every root is
    read-only here and only ever hashed.
    """
    user_home = Path.home()
    return (
        get_hermes_home() / "skills",
        Path(__file__).resolve().parent.parent / "skills",
        user_home / ".claude" / "skills-src",
        user_home / ".claude" / "skills",
        user_home / ".agents" / "skills",
        user_home / ".codex" / "skills",
    )


def _resolve_source(slugs: Iterable[str]) -> Optional[Path]:
    """First readable ``<root>/<slug>/SKILL.md``, or ``None`` when no root carries one."""
    for root in _skill_roots():
        for slug in slugs:
            candidate = root / slug / "SKILL.md"
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def _digest(path: Optional[Path]) -> Optional[str]:
    """SHA256 of *path*, hashed once per process and refreshed when its bytes change."""
    if path is None:
        return None
    key = str(path)
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    with _DIGEST_LOCK:
        cached = _DIGEST_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    with _DIGEST_LOCK:
        _DIGEST_CACHE[key] = (signature, digest)
    return digest


def _state(name: str, digest: Optional[str]) -> str:
    """``ok`` when *digest* matches the canonical pin, ``modified`` when it does not,
    ``unavailable`` when the source could not be read. Deliberately a closed vocabulary:
    this string reaches the model, so it may never carry a path, a username or file bytes."""
    if digest is None:
        return "unavailable"
    return "ok" if digest == _CANONICAL_DIGESTS[name] else "modified"


def source_digests() -> tuple[Optional[str], Optional[str], str]:
    """``(caveman_sha256, frontier_sha256, status)``.

    ``status`` is a non-secret ``caveman=<state> frontier=<state>`` summary naming only the
    source and how its bytes compare to the canonical pin — never a path, never content.
    """
    caveman = _digest(_resolve_source(_CAVEMAN_SLUGS))
    frontier = _digest(_resolve_source(_FRONTIER_SLUGS))
    status = " ".join(
        f"{name}={_state(name, value)}"
        for name, value in (("caveman", caveman), ("frontier", frontier))
    )
    return caveman, frontier, status


def _marker(caveman: Optional[str], frontier: Optional[str]) -> str:
    """Marker binding this payload to both live source digests (``none`` when unavailable)."""
    def short(digest: Optional[str]) -> str:
        return digest[:_DIGEST_PREFIX_LEN] if digest else "none"

    return f"{STARTUP_POLICY_MARKER_PREFIX} caveman:{short(caveman)} frontier:{short(frontier)}]"


# The policy itself: a paraphrase that states the operating rules, never the skill bodies.
_CAVEMAN_CLAUSE = (
    "Caveman Full stays on until the user explicitly turns it off: lead with a terse TLDR, "
    "then short bullets; no filler, no preamble, no narrating tool calls. Technical substance, "
    "numbers, code and error strings stay exact."
)
_ROUTING_CLAUSE = (
    "Routing: anything privacy-sensitive runs local. Otherwise take the cheapest capable "
    "approved subscription or local lane first, and compress raw context into a briefing "
    "before escalating."
)
_FRONTIER_CLAUSE = (
    "Spend a frontier model only on Taste, Architecture, Strategy, Review or Codify. Prefer a "
    "single call-once pass, ask for explicit approval before any metered frontier or Fable call, "
    "hand the wheel back after a bounded use, and codify reusable judgment so it is paid for once."
)
_COMPACT_CLAUSE = (
    "Caveman Full until explicitly off: terse TLDR, bullets, no filler. Privacy local; cheapest "
    "capable approved lane first; compress raw context. Frontier only for Taste, Architecture, "
    "Strategy, Review, Codify; prefer call-once; explicit approval before metered frontier or "
    "Fable; return after bounded use; codify reusable judgment."
)


def _compose(marker: str, clauses: Iterable[str], degraded_status: str) -> str:
    parts = [marker, *clauses]
    if degraded_status:
        parts.append(f"(policy sources {degraded_status}; using built-in text.)")
    return " ".join(parts)


def _within_budget(payload: str) -> bool:
    """Both ceilings, measured with the shared rough estimator the rest of the loop prices
    context with (``agent.model_metadata.estimate_tokens_rough``)."""
    from agent.model_metadata import estimate_tokens_rough

    return (
        len(payload.encode("utf-8")) <= MAX_POLICY_BYTES
        and estimate_tokens_rough(payload) <= MAX_POLICY_TOKENS
    )


def build_policy_payload() -> str:
    """The policy text for this process, or ``""`` when even the compact text is over budget."""
    caveman, frontier, status = source_digests()
    degraded = "" if status.count("=ok") == 2 else status
    if degraded and status not in _LOGGED_STATUS:
        _LOGGED_STATUS.add(status)
        logger.info("%s startup policy source status: %s", POLICY_VERSION, status)
    marker = _marker(caveman, frontier)
    for clauses in ((_CAVEMAN_CLAUSE, _ROUTING_CLAUSE, _FRONTIER_CLAUSE), (_COMPACT_CLAUSE,)):
        payload = _compose(marker, clauses, degraded)
        if _within_budget(payload):
            return payload
    logger.warning(
        "%s startup policy payload exceeds its %d byte / %d token budget; skipped",
        POLICY_VERSION, MAX_POLICY_BYTES, MAX_POLICY_TOKENS,
    )
    return ""


def strip_policy_markers(text: str) -> str:
    """Drop every marker-bearing block from *text* (hook output).

    A profile-local hook may emit its own CSR-1 block — during the transitional rollout every
    profile has one registered. Core owns the policy, so the hook's copy is removed and
    exactly one marker reaches the model. This de-duplicates output only; it never decides
    whether core injects, which is read from the claim registry.
    """
    if not text or STARTUP_POLICY_MARKER_PREFIX not in text:
        return text
    kept = [block for block in text.split("\n\n") if STARTUP_POLICY_MARKER_PREFIX not in block]
    return "\n\n".join(part for part in kept if part.strip())


def _claim_key(agent: Any) -> tuple[str, str]:
    """Claim identity within one agent: the owning profile home and the session id.

    The profile is part of the key because a delegated child or a multiplexed gateway turn
    runs under its own home and is its own conversation.
    """
    try:
        home = hermes_home_key()
    except (OSError, RuntimeError, ValueError):  # an unresolvable home still gets a claim
        home = ""
    return home, str(getattr(agent, "session_id", "") or "")


def claim_first_request(agent: Any) -> bool:
    """Atomically claim the first request of *agent*'s current conversation; ``True`` only
    for the first caller.

    The claim lives on the agent instance: two agents — even on one session id — each win
    their own, while two threads racing on one agent mean exactly one injection.
    """
    key = _claim_key(agent)
    with _CLAIM_LOCK:
        claims = getattr(agent, _CLAIM_ATTR, None)
        if not isinstance(claims, set):
            claims = set()
            try:
                setattr(agent, _CLAIM_ATTR, claims)
            except (AttributeError, TypeError):  # refuses attributes: cannot repeat a claim either
                return True
        if key in claims:
            return False
        claims.add(key)
        return True


def reset_startup_policy_state() -> None:
    """Drop process caches (digests, logged status). Tests only."""
    with _DIGEST_LOCK:
        _DIGEST_CACHE.clear()
    _LOGGED_STATUS.clear()


def _is_conversation_first_request(agent: Any, conversation_history: Optional[list]) -> bool:
    """Whether this turn is the conversation's own first LLM request.

    Both signals are core-owned. A resumed or replayed conversation arrives with history, and
    the prologue has already hydrated ``_user_turn_count`` from it, so a session that has
    spoken before is past its first request even in a fresh process.
    """
    if conversation_history:
        return False
    try:
        return int(getattr(agent, "_user_turn_count", 0) or 0) <= 1
    except (TypeError, ValueError):
        return True


def claim_startup_policy(agent: Any, conversation_history: Optional[list]) -> str:
    """The policy text to prepend to this turn's user-channel context, or ``""``.

    Non-empty exactly once per conversation, for the caller that won the claim. The claim is
    taken here, ahead of the hooks, because this is the last point every surface still passes
    through.
    """
    if not _is_conversation_first_request(agent, conversation_history):
        return ""
    payload = build_policy_payload()
    if not payload or not claim_first_request(agent):
        return ""
    return payload


def join_policy_and_context(policy: str, context: str) -> str:
    """The user-channel context for one turn: policy first (core is authoritative), then
    whatever the hooks contributed, with the hooks' duplicate marker already stripped."""
    parts = [part for part in (policy, context) if part]
    return "\n\n".join(parts)


def policy_block(context: str) -> str:
    """The core policy block inside one turn's user-channel context, or ``""``.

    ``join_policy_and_context`` puts it first and hook copies are already stripped, so a
    leading marker block is the one core claimed for this turn.
    """
    first = (context or "").split("\n\n", 1)[0]
    return first if first.startswith(STARTUP_POLICY_MARKER_PREFIX) else ""


def apply_policy_to_user_input(user_input: Any, context: str) -> Any:
    """*user_input* with this turn's policy block attached, for a runtime that is handed the
    bare user message instead of the composed ``api_content`` (the codex app-server).

    A string gains the block the way the sidecar appends it. List content already carries the
    turn's context as a text part when the prologue composed it; it is only added if absent.
    """
    policy = policy_block(context)
    if not policy:
        return user_input
    if isinstance(user_input, str):
        return f"{user_input}\n\n{policy}"
    if isinstance(user_input, list):
        for part in user_input:
            text = part.get("text") if isinstance(part, dict) else part
            if isinstance(text, str) and policy in text:
                return user_input
        return [*user_input, {"type": "text", "text": policy}]
    return user_input
