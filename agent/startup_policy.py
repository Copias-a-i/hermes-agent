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
the block explicitly, and again on the first input of any replacement thread it starts.

The invariant is: the first request of an agent instance's conversation that actually
reaches a provider carries the policy, exactly once, whether or not its caller passes history
on later turns. The prologue's claim is therefore provisional and exclusive: it is committed
only when the turn's first provider request is accepted (``commit_startup_policy``), and a
turn that ends before that — interrupt, budget exit, preflight timeout, a provider call that
raised — hands the exact claim back (``release_startup_policy``), so the next call still owes
the policy. Once committed, a later turn sent without history is not a first request and
carries none — nothing re-claims or synthesizes the policy because a transcript is empty.

Once claimed, the policy stays on the row that carried it: it is replayed with that row and
is never optional context. Its carrier is the ``api_content`` sidecar of one row — the durable
``content`` stays the user's words, and later turns' rows never gain a copy. Two core steps
keep it there: ``carry_policy`` re-stamps the sidecar on a surviving row whenever a compaction
is about to commit a transcript that no longer holds the carrier, and ``ensure_policy_on_wire``
runs after ``ContextEngine.select_context`` and re-attaches the block when a selection left
out a policy the transcript carries or this turn claimed. Both only preserve an existing
carrier (or the claim of the turn in flight); neither creates one.

The policy is a compact paraphrase bound to the SHA256 of its canonical source skills.
Source bodies are hashed, never read into the prompt. Missing or unreadable sources fail
soft to the embedded text and report a non-secret ``caveman=/frontier=`` status.

Idempotence is decided from core-owned state only — the claim states an agent instance carries,
keyed by ``(profile home, session id)``: provisional (one turn holds it), spent, or owed again. Nothing is process-global, so one agent can never
suppress another that happens to share its session id. Transcript text is never consulted: a
user, web page or tool result that contains the marker must not be able to suppress or forge
the policy. A marker a profile-local hook (the transitional CSR-1 Jarvis hook) emitted is
stripped from the hook context, so exactly one marker ever reaches the model.
"""

from __future__ import annotations

import hashlib
import logging
import re
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

# Guards the per-agent claim states below. It holds no claim state of its own.
_CLAIM_LOCK = threading.Lock()
# Attribute on the agent instance mapping each ``(profile home, session id)`` to its claim
# state: a ``_Provisional`` (the turn in flight holds it), ``_SPENT`` or ``_OWED``.
_CLAIM_ATTR = "_startup_policy_claims"
# The policy left on a provider request (or the conversation predates this agent): never again.
_SPENT = "spent"
# A turn claimed the policy and ended before any provider request: the next call owes it.
_OWED = "owed"


class _Provisional:
    """One turn's exclusive, not-yet-committed claim. ``owner`` is the thread running that
    turn — prologue, provider call and turn exit all run on it — so only that turn can settle
    this exact claim, and a concurrent turn on the same agent can neither take nor settle it."""

    __slots__ = ("owner", "policy")

    def __init__(self, policy: str) -> None:
        self.owner = threading.get_ident()
        self.policy = policy  # the exact block this turn owes its first provider request

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


_DIGEST_FIELD = rf"(?:[0-9a-f]{{{_DIGEST_PREFIX_LEN}}}|none)"
_SOURCE_STATE = r"(?:ok|modified|unavailable)"
# A well-formed marker, exactly as ``_marker`` writes it. Anything else that merely starts with
# the prefix (``[CSR-1 notes]``, ``[CSR-10 ...]``, a truncated marker) is ordinary text.
_MARKER_PATTERN = (
    rf"{re.escape(STARTUP_POLICY_MARKER_PREFIX)} caveman:{_DIGEST_FIELD} frontier:{_DIGEST_FIELD}\]"
)
# The payload ``build_policy_payload`` can produce: marker, one of the two clause sets, and the
# optional degraded-source note. Bound to the clause constants, so it follows any future edit.
_PAYLOAD_PATTERN = (
    _MARKER_PATTERN
    + " (?:"
    + "|".join(
        re.escape(" ".join(clauses))
        for clauses in ((_CAVEMAN_CLAUSE, _ROUTING_CLAUSE, _FRONTIER_CLAUSE), (_COMPACT_CLAUSE,))
    )
    + ")"
    + rf"(?: \(policy sources caveman={_SOURCE_STATE} frontier={_SOURCE_STATE}; using built-in text\.\))?"
)
_LEADING_PAYLOAD_RE = re.compile(rf"[ \t]*{_PAYLOAD_PATTERN}")
_LEADING_MARKER_RE = re.compile(rf"[ \t]*{_MARKER_PATTERN}")
_EXACT_PAYLOAD_RE = re.compile(_PAYLOAD_PATTERN)


def _strip_policy_from_line(line: str) -> Optional[str]:
    """*line* without the policy copy it leads with; ``None`` when nothing else is on it.

    Only a line that BEGINS with a well-formed marker is a policy copy. The exact built-in
    payload is cut out precisely, so text a hook wrote after it on the same line survives. A
    marker followed by other wording is a hook's own (older) paraphrase: that marker-led line
    is the copy. A marker quoted mid-line is ordinary text and is never touched.
    """
    if not _LEADING_MARKER_RE.match(line):
        return line
    rest = line
    while True:
        exact = _LEADING_PAYLOAD_RE.match(rest)
        if exact is None:
            break
        rest = rest[exact.end():]
    if _LEADING_MARKER_RE.match(rest):
        return None
    return rest.strip() or None


def strip_policy_markers(text: str) -> str:
    """*text* (hook output) without the CSR-1 policy copies it carries, and nothing else removed.

    A profile-local hook may emit its own CSR-1 copy — during the transitional rollout every
    profile has one registered. Core owns the policy, so the hook's copy is removed and
    exactly one marker reaches the model. Removal is line-scoped and marker-led (see
    ``_strip_policy_from_line``): hook text before or after the copy in the same block, text
    that quotes the marker, and malformed markers are all preserved byte-for-byte. This
    de-duplicates output only; it never decides whether core injects, which is read from the
    claim registry.
    """
    if not text or STARTUP_POLICY_MARKER_PREFIX not in text:
        return text
    blocks = []
    for block in text.split("\n\n"):
        if STARTUP_POLICY_MARKER_PREFIX in block:
            lines = (_strip_policy_from_line(line) for line in block.split("\n"))
            block = "\n".join(line for line in lines if line is not None)
        if block.strip():
            blocks.append(block)
    return "\n\n".join(blocks)


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


def _claim_states(agent: Any) -> Optional[dict]:
    """*agent*'s claim states (created on first use); ``None`` when it refuses attributes.
    Caller holds ``_CLAIM_LOCK``."""
    states = getattr(agent, _CLAIM_ATTR, None)
    if not isinstance(states, dict):
        states = {}
        try:
            setattr(agent, _CLAIM_ATTR, states)
        except (AttributeError, TypeError):
            return None
    return states


def _settle(agent: Any, outcome: str) -> bool:
    """Move the provisional claim the calling turn holds on *agent* to *outcome*.

    Found by owner, not by key: a compaction may rotate the session id between the claim and
    the provider call. ``False`` when this turn holds none (it did not win the claim, or it
    already settled), so settling is idempotent and never touches another turn's claim.
    """
    owner = threading.get_ident()
    with _CLAIM_LOCK:
        states = getattr(agent, _CLAIM_ATTR, None)
        if not isinstance(states, dict):
            return False
        for key, state in states.items():
            if isinstance(state, _Provisional) and state.owner == owner:
                states[key] = outcome
                return True
    return False


def held_startup_policy(agent: Any) -> str:
    """The policy block the calling turn still owes a provider — it holds *agent*'s provisional
    claim — or ``""``. This, not the transcript, is what makes the turn's request owe the policy:
    a history rewrite between the prologue and the request cannot drop the debt."""
    owner = threading.get_ident()
    with _CLAIM_LOCK:
        states = getattr(agent, _CLAIM_ATTR, None)
        for state in (states.values() if isinstance(states, dict) else ()):
            if isinstance(state, _Provisional) and state.owner == owner:
                return state.policy
    return ""


def commit_startup_policy(agent: Any) -> bool:
    """A provider accepted this turn's request: the claim it held is spent, for good.

    Called where a provider request returns (the generic loop's call, an accepted codex
    ``turn/start``). ``True`` only for the one call that commits; every later call is a no-op.
    """
    return _settle(agent, _SPENT)


def release_startup_policy(agent: Any) -> bool:
    """This turn is over: a claim it still holds never reached a provider, so it is owed again.

    Called on every turn exit. A no-op after ``commit_startup_policy``; otherwise the exact
    claim this turn took is handed back and the agent's next call claims the policy afresh.
    """
    return _settle(agent, _OWED)


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

    Taken here, ahead of the hooks, because this is the last point every surface still passes
    through — but only provisionally: the turn that wins holds the claim exclusively until its
    first provider request is accepted (``commit_startup_policy``) or it ends without one
    (``release_startup_policy``). While it is held, and once it is spent, every other call gets
    ``""``, so two turns racing on one agent can never both put the policy on the wire.

    Whether a claim is owed is read from the claim state alone. An agent with no state for
    this conversation decides once, from the core-owned first-request signals; a claim that
    was released is owed whatever the caller passes as history. History is then consulted only
    to avoid a second copy: when it replays the row the aborted turn stamped, the claim is held
    for that carrier's block without adding another, and ``settle_request_policy`` sends it
    from the claim even if the carrier does not survive to the request.
    """
    payload = build_policy_payload()
    if not payload:
        return ""
    first_request = _is_conversation_first_request(agent, conversation_history)
    key = _claim_key(agent)
    with _CLAIM_LOCK:
        states = _claim_states(agent)
        if states is None:  # refuses attributes: nothing to hold, and nothing to repeat
            return payload if first_request else ""
        state = states.get(key)
        if state is None and not first_request:
            states[key] = state = _SPENT
        if state is _SPENT or isinstance(state, _Provisional):
            return ""
        replayed = carried_policy(conversation_history) if state is _OWED else ""
        states[key] = _Provisional(replayed or payload)
    return "" if replayed else payload


def join_policy_and_context(policy: str, context: str) -> str:
    """The user-channel context for one turn: policy first (core is authoritative), then
    whatever the hooks contributed, with the hooks' duplicate marker already stripped."""
    parts = [part for part in (policy, context) if part]
    return "\n\n".join(parts)


def split_policy_context(context: str) -> tuple[str, str]:
    """``(policy, rest)`` of one turn's user-channel context.

    ``join_policy_and_context`` puts the claimed policy first, as its own block. Only a block
    that IS a built-in payload counts — a hook or gateway note that merely begins with the
    marker prefix is context, never the policy.
    """
    first, _, rest = (context or "").partition("\n\n")
    if _EXACT_PAYLOAD_RE.fullmatch(first):
        return first, rest
    return "", context or ""


def policy_block(context: str) -> str:
    """The core policy block inside one turn's user-channel context, or ``""``."""
    return split_policy_context(context)[0]


def attach_policy(user_input: Any, policy: str) -> Any:
    """*user_input* with *policy* attached the way the sidecar appends it.

    A string gains the block after a blank line. List content already carries the turn's
    context as a text part when the prologue composed it; the block is only added if absent,
    and always on a copy so the durable list is never touched.
    """
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


def is_policy_payload(text: Any) -> bool:
    """Whether *text* is exactly one built-in policy payload (either clause set, any digests)."""
    return isinstance(text, str) and _EXACT_PAYLOAD_RE.fullmatch(text) is not None


def _policy_blocks(text: Any) -> list[str]:
    """The blank-line-delimited blocks of *text* that are exactly a built-in payload."""
    if not isinstance(text, str) or STARTUP_POLICY_MARKER_PREFIX not in text:
        return []
    return [block for block in text.split("\n\n") if _EXACT_PAYLOAD_RE.fullmatch(block)]


def _row_policy(message: Any) -> str:
    """The policy core attached to *message*, or ``""``.

    Read from core-owned structure, not from what the row says: the ``api_content`` sidecar
    holds a payload block the row's own ``content`` does not, so text a user typed (which
    appears in both) can neither forge nor hide a carrier. List content takes no sidecar; its
    turn context — the policy included — is the text part core appended.
    """
    if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
        return ""
    content = message.get("content")
    stamped = _policy_blocks(message.get("api_content"))
    if len(stamped) > len(_policy_blocks(content)):
        return stamped[-1]
    if isinstance(content, list) and message.get("role") == "user":
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                blocks = _policy_blocks(part.get("text"))
                if blocks:
                    return blocks[0]
    return ""


def carried_policy(messages: Optional[Iterable[Any]]) -> str:
    """The policy the transcript *messages* carries, or ``""`` when no row is a carrier."""
    for message in messages or ():
        policy = _row_policy(message)
        if policy:
            return policy
    return ""


def conversation_policy(messages: Optional[Iterable[Any]], turn_context: str = "") -> str:
    """The policy this request must carry, or ``""``.

    Owed in exactly two cases, both read from core-owned structure: the transcript being sent
    holds a carrier row (the first-turn row, a row a compaction re-stamped, or either replayed
    from the store in a fresh process), or the prologue claimed the policy for this very turn
    (``turn_context``). Nothing else creates a debt. In particular a later turn whose caller
    passed no history is not a first request and carries none: the claim was spent on this
    agent's first request, and an empty transcript never re-claims or synthesizes it. A
    conversation that predates the policy owes nothing, so its replayed prefix is not altered.
    """
    return carried_policy(messages) or policy_block(turn_context)


def carry_policy(before: Optional[Iterable[Any]], after: list, *, cloned: Any = None) -> bool:
    """Keep the policy on a transcript that is about to replace *before*; ``True`` when stamped.

    *after* is what a compaction is about to commit. When a row of *before* carried the policy
    and no row of *after* does, the block is stamped on the ``api_content`` sidecar of a surviving row — the durable ``content`` is not touched.
    Preference: the first user row written from its dict, then the summary row, then a user
    row the store clones from its durable original (``cloned(row)`` true: live for this
    process, though the clone does not persist the stamp). The policy is never summarized and
    never competes for budget; it is re-stamped at every boundary that would otherwise drop it.
    A transcript that holds no carrier gains none: a compaction preserves, it never claims.
    """
    policy = carried_policy(before)
    if not policy or carried_policy(after):
        return False
    rows = [m for m in after if isinstance(m, dict) and isinstance(m.get("content"), str) and m["content"]]
    is_cloned = cloned or (lambda _row: False)
    for wanted in (
        lambda m: m.get("role") == "user" and not is_cloned(m),
        lambda m: m.get("role") == "assistant" and m.get("_compressed_summary") and not is_cloned(m),
        lambda m: m.get("role") == "user",
    ):
        target = next((m for m in rows if wanted(m)), None)
        if target is not None:
            sent = target.get("api_content")
            base = sent if isinstance(sent, str) and sent else target["content"]
            target["api_content"] = attach_policy(base, policy)
            return True
    for message in after:  # only multimodal user rows survive: the part channel they already use
        if isinstance(message, dict) and message.get("role") == "user" and isinstance(message.get("content"), list):
            message["content"] = attach_policy(message["content"], policy)
            return True
    return False


def carry_policy_into_commit(
    held: Optional[Iterable[Any]], verbatim_tail: Optional[Iterable[Any]], final: list,
    *, cloned_ids: Optional[Iterable[int]] = None,
) -> bool:
    """``carry_policy`` for a compaction commit: keep the policy on *final*, the list committed.

    The built-in startup policy is never compressible: head protection decays and the summarizer
    reads clean ``content``, so a later compaction would drop its only copy. *held* is the history
    the compressor was handed and *verbatim_tail* the exchanges it kept aside; *final* is stamped
    after the anti-growth verdict, immediately before it is stored. An in-place commit clones its
    tail rows from their durable originals — *cloned_ids* names them by ``id()`` — so a row
    written fresh is preferred; a rotation writes every row and passes none.
    """
    cloned = None
    if cloned_ids is not None:
        ids = set(cloned_ids)
        cloned = lambda row: id(row) in ids  # noqa: E731
    return carry_policy([*(held or ()), *(verbatim_tail or ())], final, cloned=cloned)


def _wire_has_policy(message: Any, policy: str) -> bool:
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return policy in content
    if isinstance(content, list):
        return any(
            isinstance(text, str) and policy in text
            for text in (part.get("text") if isinstance(part, dict) else part for part in content)
        )
    return False


def ensure_policy_on_wire(api_messages: list, policy: str) -> list:
    """*api_messages* carrying *policy*: the request as selected, plus the block if it is missing.

    The last word on one request, run after ``ContextEngine.select_context``. Whether the
    request owes the policy was already decided from core state (``conversation_policy``);
    this only checks that the exact bytes are leaving in a user message. A selection that kept
    the carrier is returned untouched (byte-stable prefix). One that dropped it, or handed back
    a clean clone whose sidecar the transport strips, gets the block appended to its first user
    message, on a copy; a selection with no user message at all gets it as one, ahead of the
    conversation. Deterministic for a given selection, so repeated requests send the same bytes.
    """
    if not policy or not isinstance(api_messages, list):
        return api_messages
    if any(_wire_has_policy(message, policy) for message in api_messages):
        return api_messages
    wire = list(api_messages)
    for index, message in enumerate(wire):
        if isinstance(message, dict) and message.get("role") == "user" and isinstance(
                message.get("content"), (str, list)):
            wire[index] = {**message, "content": attach_policy(message["content"], policy)}
            return wire
    lead = 0
    while lead < len(wire) and isinstance(wire[lead], dict) and wire[lead].get("role") == "system":
        lead += 1
    wire.insert(lead, {"role": "user", "content": policy})
    return wire



def settle_request_policy(
    agent: Any, messages: list, api_messages: list, turn_context: str = "", *, static: bool = True,
) -> list:
    """*api_messages* carrying the policy this request owes, exactly once; the assembly's one call.

    The debt is ``conversation_policy`` — a carrier in the transcript, or this turn's claimed
    block — or, failing both, the provisional claim the turn still holds: a released claim
    re-taken over a replayed carrier adds no block of its own, and the alternation repair that
    merges the aborted row with the new one drops that carrier's sidecar. The claim keeps the
    request owing the policy, and the first user row is re-stamped with what was sent so later
    turns replay it. ``static`` is false for a MoA request, whose wire bytes carry per-call
    context: its row is stamped with the policy alone, as the MoA prologue stamps it.
    """
    held = held_startup_policy(agent)
    wire = ensure_policy_on_wire(api_messages, conversation_policy(messages, turn_context) or held)
    if not held or carried_policy(messages):
        return wire
    row = next((m for m in messages if isinstance(m, dict) and m.get("role") == "user"
                and isinstance(m.get("content"), str) and m["content"]), None)
    sent = next((m["content"] for m in wire if _wire_has_policy(m, held)
                 and isinstance(m.get("content"), str)), None)
    if row is not None and sent is not None and sent.startswith(row["content"]):
        row["api_content"] = sent if static else attach_policy(row["content"], held)
    return wire
