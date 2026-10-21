# COP-751: portable low-token session-start bootstrap — implementation plan

**Status:** plan only. No runtime/config deployment or implementation starts until this exact plan passes independent review. Repair round **1 of 2**.

## Goal and architecture

Deploy a bounded first-request bootstrap across Studio, Worker, Main, and MacBook. It emits a one-line Caveman/SOUL/personality/environment-hint receipt plus a compact Graphify/Pinecone data pack; it never preloads a full skill body. The canonical stdlib-only implementation lives in `$HOME/Agents/fleet-capabilities/` and is release-installed outside worktrees. Native adapters provide only validated request metadata and delivery through a verified first-request seam.

The data path is: adapter metadata → canonical bootstrap → local Graphify reader and partitioned Pinecone query → redaction/budget renderer → adapter delivery → captured model-bound request/receipt. No adapter may claim injection unless its first provider request contains the canary-delimited pack.

## Verified Hermes seam and required minimal core change

`on_session_start` is observer-only: `agent/conversation_loop.py::_restore_or_build_system_prompt` calls it only after `_build_system_prompt` and ignores its result. It is **not** an injection path and remains only an optional lifecycle observer.

Hermes has a context-consuming shell-hook seam: `pre_llm_call` is a supported `hooks:` event (`hermes_cli/plugins.py::VALID_HOOKS`). `agent/turn_context.py::_collect_pre_llm_call_context` consumes a `{"context": "..."}` return and `build_api_messages` appends it to the current user message's API-sidecar before the first provider request. `agent/shell_hooks.py::_parse_response` supports that return shape for shell hooks. This preserves the byte-stable system prompt/cache; the compact pack is first-turn user-context, not a system-prompt section.

The current wire payload uses process-global `Path.cwd()` in `agent/shell_hooks.py::_payload_fields`, while a multiplexed logical workspace is `agent.runtime_cwd.resolve_agent_cwd()`. Therefore COP-755 includes this minimal Hermes-core prerequisite, rather than inventing a hook return injector:

1. Modify `agent/turn_context.py` to pass `cwd=str(resolve_agent_cwd())` when invoking `pre_llm_call`.
2. Modify `agent/shell_hooks.py::_payload_fields` to use a non-empty hook `cwd` kwarg first, then the current `Path.cwd()` fallback; preserve `profile` from `get_active_profile_name()`.
3. Tests: `tests/agent/test_shell_hooks.py` proves the shell payload carries supplied logical CWD and profile; `tests/agent/test_api_content_sidecar.py` captures the actual first `build_api_messages` user request and asserts the exact `OPEN-GATE:<pack-digest>` canary appears there (not merely in hook output), including a logical-CWD Graphify fixture. A negative test proves `on_session_start` output alone does not count as delivery.

Hermes adapter registration is `hooks.pre_llm_call` with an allowlisted command and <=7s timeout. It retains existing `on_session_start` entries unchanged and does not claim they inject context. For Codex and Claude Code, only their documented `SessionStart` return/context surface may be used; their first provider request must contain the same canary. Cursor currently has no verified start hook in the audited configuration, so it stays `unsupported` with no config mutation until a separately documented native first-request seam passes the same request-shape test. Antigravity, Gemini, and Grok follow the same default-deny discovery rule.

## Request, privacy, and namespace contract

Adapters submit JSON on stdin only:

```json
{"schema":2,"runtime":"hermes","host":"studio","tenant":"fleet","profile":"jarvis","session_id":"opaque","cwd":"/absolute/workspace","objective":"bounded redacted objective","branch":"optional"}
```

All fields are untrusted. Canonicalize the CWD, require a Git root, bound objective length before queries, and never shell-interpolate. The rendered/vector query text includes only generic task terms plus a repository digest and branch digest; it excludes raw tenant/profile/host/session identifiers, role, usernames, email, objective identifiers, and retrieved PII.

Default deny uses an explicit non-secret partition tuple:

```
namespace = v1/<sha256(tenant)>/<sha256(profile)>/<sha256(runtime)>/<sha256(host)>
```

A per-install random salt held only in the 0600 local config is included in each digest. The bootstrap passes `--bucket <namespace>` to the already verified `~/.claude/pinecone_memory.py`; that wrapper's `build_query_filter()` produces `{"bucket":{"$eq": namespace}}` in the `/query` request. The namespace is included in cache-key material, cache directory, receipt (digest only), and every fixture. There is no shared-memory mode in this release. `claude-memory` remains usable only because its existing records have `bucket` metadata; records with missing/legacy bucket are ignored, never queried as a fallback. A later migration ticket must backfill from an audited source into the derived namespace using only redacted vector text, with readback counts and rollback; it is not part of startup activation.

Cache key: `schema + release_hash + namespace + runtime + repo_HEAD + branch_digest + objective_digest + graph_digest + query_digest`. Receipts have 0600 mode, contain only namespace digest/availability/timing/counts, and never raw text, role, profile, host, tenant, session ID, credentials, or PII.

## Readers and hard safety boundaries

### Graphify: deterministic local reader only

COP-753 adds `src/fleet_session_bootstrap/graphify_local.py`, using Python stdlib (`json`, `pathlib`, deterministic token overlap/ranking) to parse only `<git-root>/graphify-out/graph.json`. It performs no Graphify CLI invocation, no subprocess, no graph construction/update/build/cluster/export/watch, and no write. Invalid, oversized, symlinked, or absent graph returns a labeled unavailable result.

Transport proof is mandatory: execute the helper in a test subprocess with a `sitecustomize` deny shim that raises and records calls for `socket.socket`, `socket.create_connection`, `socket.getaddrinfo`, `urllib.request.urlopen`, and `http.client.HTTPConnection`; assert zero calls, zero DNS/HTTP attempts, and zero subprocess launches. Static tests reject imports of `socket`, `urllib`, `http`, `requests`, `subprocess`, and `os.system` in this helper. Fixture graph hash/mtime before and after must match.

### Pinecone: partitioned query only

COP-754 invokes only this argv list: `[python, ~/.claude/pinecone_memory.py, "query", query, "--top-k", "3", "--bucket", namespace, "--json"]`. It accepts JSON only, allows one bounded retry for timeout/429/5xx within the global deadline, and never prompts LastPass interactively. The command allowlist rejects `store`, `ingest-file`, `delete`, `fetch`, `stats`, arbitrary flags, and raw endpoints before process launch. Query metadata must show the exact bucket filter; returned metadata must match the namespace or is discarded.

Negative tests cover profile, tenant, runtime, host, and session collision: identical repo/objective with one changed partition component has a different cache key, different query filter, cache miss, and receives no other partition's fixture. Tests also reject missing bucket, legacy unbucketed match, mismatched returned bucket, and vector/rendered text containing profile/role/PII markers. Fake transport asserts zero upsert/delete calls.

## Context, budget, and failure contract

Render only `OPEN-GATE:<digest>`, `style=caveman-full`, compact standing `SOUL/personality/environment_hint` identifiers (not bodies), source availability, and quoted/redacted data excerpts. Total retrieval pack cap is 800 tokens; no full `SKILL.md`, SOUL, personality, or environment file body may be preloaded. Any unavailable/invalid/timeout condition returns a labeled no-retrieval pack and exit 0. Whole startup budget is <=7s; Graphify <=3s and Pinecone uses only remaining time.

## Mandatory measured cost gate

Before any adapter activation, capture a **nonzero** baseline from actual model-bound first requests for every supported runtime/host/profile cohort. Capture system/developer/instructions, retained existing hooks, SOUL, personality, environment hint, tools, user content, and retrieval pack separately and as aggregate bytes/tokens. Baseline absence, zero samples, missing component, unknown tokenizer/version, or request capture failure is a hard fail; it cannot be labeled a saving.

Use the same version-pinned tokenizer/counter and deterministic capture format for before/after. For each supported runtime/host cohort, collect at least 30 synthetic first sessions across at least 3 representative profiles where present; report p50/p95 aggregate first-request tokens and bytes, p95 startup latency, and Pinecone calls/cost per session. Activation requires: p95 aggregate tokens not above baseline; p95 startup latency <=7s; <=1 Pinecone call/session; observed Pinecone cost/session <= the pre-approved budget recorded in COP-757; zero cross-namespace retrieval/cache events; and all request-shape canaries present. No savings percentage is claimed until this receipt exists.

Rollback immediately disables the adapter and restores the previous release/config fragment if aggregate p95 tokens increase, p95 latency exceeds 7s, a second Pinecone call occurs, cost exceeds budget, a namespace mismatch occurs, or any canary/request-shape assertion fails. Implementation owner performs code rollback; Colby Culbertson is rollback/activation owner.

## Task manifest and Linear contract

All tickets are COP team, Todo until dependency gates clear, assigned to **Colby Culbertson** (the verified available accountable Linear user); **Codex** is the implementation agent named in each body; Colby Culbertson owns activation/rollback. Ticket descriptions below are authoritative and must match Linear bodies exactly.

| Order | Ticket | Scope/files | Depends on / blocks | Acceptance and negative controls |
|---|---|---|---|---|
| T1 | COP-752 | fleet-capabilities: `src/fleet_session_bootstrap/{__init__,contract,render,cache}.py`, `bin/fleet-session-bootstrap`, `tests/test_contract.py` | first; blocks 753/754 | validated stdin, salted namespace/cache/receipt; reject bad CWD, PII/role vector text, missing partition, cache collision |
| T2 | COP-753 | `src/fleet_session_bootstrap/graphify_local.py`, `tests/test_graphify_local.py`, `tests/sitecustomize_socket_deny.py` | after 752; blocks 755/757 | local graph result and transport-proof; reject socket/DNS/HTTP/subprocess/imports/writes |
| T3 | COP-754 | `src/fleet_session_bootstrap/pinecone_query.py`, `tests/test_pinecone_query.py`, `tests/fake_pinecone.py` | after 752; blocks 755/757 | exact bucket-filter query and namespace isolation; reject legacy/unbucketed/cross-partition/upsert/delete |
| T4 | COP-755 | Hermes core: `agent/turn_context.py`, `agent/shell_hooks.py`, `tests/agent/test_shell_hooks.py`, `tests/agent/test_api_content_sidecar.py`; fleet adapters/tests | after 753+754; blocks 756/757 | first-request canary + logical CWD; reject observer-only `on_session_start`, unsupported Cursor/Codex/Claude adapter, full-skill preload |
| T5 | COP-756 | `installer/*`, `adapters/*`, `tests/test_install_rollback.py` | after 755; blocks 757 activation | immutable release/install/readback/rollback; reject worktree execution, config loss, unsupported runtime mutation |
| T6 | COP-757 | `measurement/*`, `tests/test_measurement_contract.py`, receipt schema | after 753+754+755+756; sole activation gate | 30x3 cohort aggregate request evidence/cost gate; reject zero/missing baseline and p95 threshold violations |

## Rollout and verification

Inventory Studio, Worker, Main, and MacBook without credentials. Install immutable releases at `$HOME/.fleet/session-bootstrap/releases/<hash>/` and atomically flip `current`; never run a source-worktree artifact. Per runtime, preserve existing hooks/config order and take a redacted config-hash backup. Only a supported adapter with all gates green is enabled. Cursor/Antigravity/Gemini/Grok remain receipt-only `unsupported/not-installed` until their own official start-event and first-request test pass.

Run: canonical fleet-library tests; affected Hermes tests; `python scripts/check`; static plan contract checking ticket/manifest/namespace/transport/cost clauses; Linear readback of all six ticket bodies/assignees/team/state; and `git diff --check`. Commit only this plan, push normally, verify branch and PR API head, reply and resolve each current thread only after its correction is present, confirm zero unresolved threads, then trigger a fresh exact-head independent review. Do not merge or implement.

## Rejected alternatives

1. `on_session_start` context injection: observer-only and post-build.
2. Full skill/SOUL/personality preload: violates the token objective.
3. Graphify CLI/sandbox-by-contract: an external query could network; local stdlib parser is deterministic.
4. Unpartitioned/shared Pinecone or legacy fallback: cross-profile retrieval risk.
5. Cursor UI automation/polling: not a verified first-request seam.

## Completion criteria

- Exact-head independent review has no Critical/Important unresolved finding within the two-round cap.
- COP-752..757 Linear bodies, assignment, team, status, dependencies, rollback owner, and manifest are read back one-to-one.
- No implementation, runtime reload, config activation, credential, or merge action occurs under this plan-only PR.
