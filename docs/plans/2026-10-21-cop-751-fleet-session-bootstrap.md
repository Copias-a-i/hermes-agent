# COP-751: portable low-token session-start bootstrap — implementation plan

**Status:** plan only. No runtime/config deployment or implementation starts until this exact plan passes independent review. The ordinary two-round repair cap is exhausted; this is a scoped architecture-follow-up correction under COP-759, COP-760, and COP-761, not repair round 3. PR #8 and implementation remain blocked until these follow-ups pass a fresh exact-head independent review.

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

Hermes adapter registration is `hooks.pre_llm_call` with an allowlisted command and <=7s timeout. The in-process invocation must carry the authoritative `is_first_turn` value to the adapter; it is fail-closed for bootstrap purposes when absent, malformed, or false. Before cache lookup, rendering, or either reader, every non-first turn returns no context and performs zero Pinecone calls. It retains existing `on_session_start` entries unchanged and does not claim they inject context. For Codex and Claude Code, only their documented `SessionStart` return/context surface may be used; their first provider request must contain the same canary. Cursor currently has no verified start hook in the audited configuration, so it stays `unsupported` with no config mutation until a separately documented native first-request seam passes the same request-shape test. Antigravity, Gemini, and Grok follow the same default-deny discovery rule. (COP-759; `discussion_r4338633670`.)

## Request, privacy, and namespace contract

Adapters submit JSON on stdin only:

```json
{"schema":2,"runtime":"hermes","host":"studio","tenant":"fleet","profile":"jarvis","session_id":"opaque","cwd":"/absolute/workspace","objective":"bounded redacted objective","branch":"optional"}
```

All stdin fields are untrusted assertions. Canonicalize the CWD, require a Git root, bound objective length before queries, and never shell-interpolate. The active profile process supplies a trusted, profile-scoped identity from its owned runtime configuration/environment; supplied tenant/profile/runtime/host must exactly match it or the bootstrap returns unavailable before cache/query. The rendered/vector query text includes only generic task terms plus a repository digest and branch digest; it excludes raw tenant/profile/host/session identifiers, role, usernames, email, objective identifiers, and retrieved PII.

Default deny uses an explicit non-secret partition tuple:

```
namespace = v1/<sha256(tenant)>/<sha256(profile)>/<sha256(runtime)>/<sha256(host)>
```

A per-install random salt held only in the 0600 local config is included in each digest. The bootstrap passes `--bucket <namespace>` to the already verified `~/.claude/pinecone_memory.py`; that wrapper's `build_query_filter()` produces `{"bucket":{"$eq": namespace}}` in the `/query` request. The namespace is included in cache-key material, cache directory, receipt (digest only), and every fixture. `session_id` is receipt/race identity only: changing it must preserve the namespace, query filter, and shared cache key. There is no shared-memory mode in this release. Before any adapter activation, an audited reversible migration must obtain a documented authenticated source-to-trusted-profile mapping, enumerate eligible records, write only redacted vectors with the exact derived bucket, and record a durable migration ledger plus deterministic **pre-migration snapshot** (count, sorted IDs, hashes) for every affected exact bucket. The ledger records each operation ID, source/target bucket, record ID, prior/result metadata/version/hash, release/actor/timestamp, and rollback state. Missing mapping, ledger, snapshot, count/hash match, or readback blocks activation with zero adapter config writes. Records with missing/legacy bucket remain ignored and are never queried as a fallback. (COP-760 and COP-763; `discussion_r4338633688`, `discussion_r4338633699`, `discussion_r4338633728`, `discussion_r4338979663`.)

Cache key: `schema + release_hash + namespace + runtime + repo_HEAD + branch_digest + objective_digest + graph_digest + query_digest`. Receipts have 0600 mode, contain only namespace digest/availability/timing/counts, and never raw text, role, profile, host, tenant, session ID, credentials, or PII.

## Readers and hard safety boundaries

### Graphify: deterministic local reader only

COP-753 adds `src/fleet_session_bootstrap/graphify_local.py`, using Python stdlib (`json`, `pathlib`, deterministic token overlap/ranking) to parse only `<git-root>/graphify-out/graph.json`. It performs no Graphify CLI invocation, no subprocess, no graph construction/update/build/cluster/export/watch, and no write. Invalid, oversized, symlinked, or absent graph returns a labeled unavailable result.

Transport proof is mandatory: execute the helper in a test subprocess with a `sitecustomize` deny shim that raises and records calls for `socket.socket`, `socket.create_connection`, `socket.getaddrinfo`, `urllib.request.urlopen`, and `http.client.HTTPConnection`; assert zero calls, zero DNS/HTTP attempts, and zero subprocess launches. Static tests reject imports of `socket`, `urllib`, `http`, `requests`, `subprocess`, and `os.system` in this helper. Fixture graph hash/mtime before and after must match.

### Pinecone: partitioned query only

COP-754 invokes only a wrapper whose SHA-256 is pinned in the immutable release manifest: `[python, ~/.claude/pinecone_memory.py, "query", query, "--top-k", "3", "--bucket", namespace, "--json"]`. Before every spawn it resolves a regular file, computes SHA-256, compares it to the manifest's audited wrapper digest, and writes only that digest to the receipt; a missing/mismatched wrapper is `unavailable:wrapper-digest` and is never executed. The pin is a read-only trust check, not a copy or modification of the host wrapper. A first-turn bootstrap may make at most two Pinecone **attempts** only when attempt one is timeout/429/5xx and the remaining global deadline permits it; success, non-retryable failure, or deadline exhaustion stops immediately. Receipts distinguish `bootstrap_invocations/session` from `pinecone_attempts/session`; later turns may make neither. It never prompts LastPass interactively. The command allowlist rejects `store`, `ingest-file`, `delete`, `fetch`, `stats`, arbitrary flags, and raw endpoints before process launch. Query metadata must show the exact bucket filter; returned metadata must match the namespace or is discarded. (COP-759; `discussion_r4338633709`.)

Negative tests cover trusted profile identity and tenant/runtime/host collision: identical repo/objective with one changed trusted partition component has a different cache key, different query filter, cache miss, and receives no other partition's fixture; spoofed stdin identity must return unavailable before cache/query. Changing only `session_id` must preserve namespace/query filter/cache key and prove permitted same-partition cache reuse. Tests also reject missing bucket, legacy unbucketed match, mismatched returned bucket, and vector/rendered text containing profile/role/PII markers. Fake transport asserts zero upsert/delete calls.

## Context, budget, and failure contract

Render only `OPEN-GATE:<digest>`, `style=caveman-full`, compact standing `SOUL/personality/environment_hint` identifiers (not bodies), source availability, and quoted/redacted data excerpts. Total retrieval pack cap is 800 tokens; no full `SKILL.md`, SOUL, personality, or environment file body may be preloaded. Any unavailable/invalid/timeout condition returns a labeled no-retrieval pack and exit 0. Whole startup budget is <=7s; Graphify <=3s and Pinecone uses only remaining time.

## Mandatory measured cost gate

Before any adapter activation, capture a **nonzero** baseline from actual model-bound first requests for every supported runtime/host/profile cohort. Capture system/developer/instructions, retained existing hooks, SOUL, personality, environment hint, tools, user content, and retrieval pack separately and as aggregate bytes/tokens. Baseline absence, zero samples, missing component, unknown tokenizer/version, or request capture failure is a hard fail; it cannot be labeled a saving.

Use the same version-pinned tokenizer/counter and deterministic capture format for before/after. For each supported runtime/host cohort, collect at least 30 synthetic first sessions across at least 3 representative profiles where present **for aggregate trend reporting only**. Authorization is never cohort-sampled: every enabled profile (and each runtime/host adapter when its partition differs) must have its own nonzero baseline and independent activation-matrix row. Report p50/p95 aggregate first-request tokens and bytes, p95 startup latency, `bootstrap_invocations/session`, Pinecone attempts/session, and cost/session. Activation for a profile requires: p95 aggregate tokens not above that profile baseline; p95 startup latency <=7s; <=1 bootstrap invocation/session; <=2 Pinecone attempts only under the documented retry reasons/deadline; observed Pinecone cost/session <= the pre-approved budget recorded in COP-757; zero later-turn Pinecone calls; zero cross-namespace retrieval/cache events; completed pre-activation migration receipt; successful deployed-seam exact-bucket query; and all request-shape canaries present. No savings percentage is claimed until that profile receipt exists.

Rollback immediately disables the adapter and restores the previous release/config fragment if aggregate p95 tokens increase, p95 latency exceeds 7s, bootstrap invocation or retry-attempt limits are exceeded, cost exceeds budget, a namespace mismatch occurs, migration evidence is invalid, or any canary/request-shape assertion fails. It must then use the durable migration ledger to reverse every recorded migration write (delete newly partitioned records or restore captured prior metadata/state), idempotently; production-query/read back every affected exact bucket and compare count, sorted IDs, and hashes to its pre-migration snapshot. Injected crash/retry tests must prove convergence without duplicate writes/lost records. Any ledger gap, partial reversal, or snapshot mismatch is `rollback-incomplete`: adapter disabled and activation blocked. It then performs the approved owning-gateway clear/re-registration or targeted restart so the live callback is absent before rollback completion; clear/reload failure leaves the adapter disabled and activation blocked. Implementation owner performs code rollback; Colby Culbertson is rollback/activation owner. (COP-761/COP-763; `discussion_r4338633739`, `discussion_r4338979663`.)

## Task manifest and Linear contract

All tickets are COP team, Todo until dependency gates clear, assigned to **Colby Culbertson** (the verified available accountable Linear user); **Codex** is the implementation agent named in each body; Colby Culbertson owns activation/rollback. Ticket descriptions below are authoritative and must match Linear bodies exactly.

| Order | Ticket | Scope/files | Depends on / blocks | Acceptance and negative controls |
|---|---|---|---|---|
| T1 | COP-752 | fleet-capabilities: `src/fleet_session_bootstrap/{__init__,contract,render,cache}.py`, `bin/fleet-session-bootstrap`, `tests/test_contract.py` | after COP-760 architecture contract; blocks 753/754 | trusted profile identity vs stdin assertion, salted namespace/cache/receipt; reject spoofed/missing partition, bad CWD, PII/role vector text, cache collision |
| T2 | COP-753 | `src/fleet_session_bootstrap/graphify_local.py`, `tests/test_graphify_local.py`, `tests/sitecustomize_socket_deny.py` | after 752; blocks 755/757 | local graph result and transport-proof; reject socket/DNS/HTTP/subprocess/imports/writes |
| T3 | COP-754 | `src/fleet_session_bootstrap/{pinecone_query,wrapper_pin}.py`, `tests/test_pinecone_query.py`, `tests/fake_pinecone.py` | after 752 and COP-759/COP-760/COP-763 architecture contracts; blocks 755/757 | exact pinned-wrapper trusted bucket-filter query and per-profile deployed-seam receipt, durable-ledger migration/rollback; reject wrapper drift, legacy/unbucketed/cross-partition/upsert/delete/admin/mock-only proof |
| T4 | COP-755 | Hermes core: `agent/turn_context.py`, `agent/shell_hooks.py`, `tests/agent/test_shell_hooks.py`, `tests/agent/test_api_content_sidecar.py`; fleet adapters/tests | after 753+754 and COP-759 architecture contract; blocks 756/757 | first-turn-only request canary + logical CWD; reject observer-only `on_session_start`, missing/spoofed first-turn flag, later-turn query, unsupported Cursor/Codex/Claude adapter, full-skill preload |
| T5 | COP-756 | `installer/*`, `adapters/*`, `tests/test_install_rollback.py` | after 755 and COP-761/COP-763 architecture contracts; blocks 757 activation | immutable release/install/readback/live-unregister and per-profile write/reload gates; reject worktree execution, config loss, residual live callback, unsupported runtime mutation, missing profile PASS row |
| T6 | COP-757 | `measurement/*`, `tests/test_measurement_contract.py`, receipt schema | after 753+754+755+756 and COP-759/COP-760/COP-761/COP-763 architecture contracts; sole activation gate | per-enabled-profile matrix, deployed-seam query, cost/migration/rollback gate; reject zero/missing baseline, later-turn query, invalid migration, live-rollback failure, cross-profile inheritance, and p95 threshold violations |

## Rollout and verification

Inventory Studio, Worker, Main, and MacBook without credentials. Complete COP-760/COP-763 authenticated mapping, durable-ledger reversible bucket migration, and exact-bucket readback before any adapter config write or activation. Install immutable releases at `$HOME/.fleet/session-bootstrap/releases/<hash>/` and atomically flip `current`; never run a source-worktree artifact. Per runtime, preserve existing hooks/config order and take a redacted config-hash backup. For Hermes, snapshot the exact per-profile `$HERMES_HOME/shell-hooks-allowlist.json` approval entry before registration; rollback restores the prior allowlist entry state (removes the bootstrap approval when absent before install) as well as release/config fragments, then performs COP-761's owning-gateway clear/re-registration or targeted restart and reads back config, allowlist hashes, and live callback absence from a fresh session. If that manager operation fails, keep the adapter disabled and report rollback incomplete. Only a supported adapter with all gates green is enabled. Cursor/Antigravity/Gemini/Grok remain receipt-only `unsupported/not-installed` until their own official start-event and first-request test pass.

Run: static plan contracts for COP-759/COP-760/COP-761/COP-763 attribution, trusted identity, session-equality, first-turn/no-later-turn behavior, deployed-seam exact-bucket proof, durable-ledger rollback/retry, per-profile matrix, and live callback rollback; affected Hermes tests where plan-only seams are testable; `python scripts/check --base upstream/main`; Linear readback of COP-751/COP-754/COP-755/COP-756/COP-757/COP-763 bodies/assignees/team/state/relations; and `git diff --check`. Commit only this plan, push normally, verify branch and PR API head/files, reply and resolve each current thread only after its correction is present, confirm zero unresolved threads, then request a fresh exact-head independent review. Do not merge or implement.

## Rejected alternatives

1. `on_session_start` context injection: observer-only and post-build.
2. Full skill/SOUL/personality preload: violates the token objective.
3. Graphify CLI/sandbox-by-contract: an external query could network; local stdlib parser is deterministic.
4. Unpartitioned/shared Pinecone or legacy fallback: cross-profile retrieval risk.
5. Cursor UI automation/polling: not a verified first-request seam.

## Post-cap architecture follow-up ledger

| Follow-up | Root-cause/trust boundary | Threads | Plan correction |
|---|---|---|---|
| COP-759 | model-hook lifecycle and external-query attempt accounting | `discussion_r4338633670`, `discussion_r4338633709` | authoritative first-turn gate; no later-turn work; separate logical invocation from allowed retry attempts |
| COP-760 | trusted profile partition authority and data-availability migration | `discussion_r4338633688`, `discussion_r4338633699`, `discussion_r4338633728` | profile-scoped identity verification; session equality; audited reversible pre-activation migration |
| COP-761 | live callback revocation during rollback | `discussion_r4338633739` | targeted clear/re-registration or restart; fresh-session callback-absence receipt |


## COP-763 post-cap architecture follow-up — per-profile Pinecone activation and reversible migration proof

This is one named trust-boundary follow-up, not ordinary repair round 3. It addresses `https://github.com/Copias-a-i/hermes-agent/pull/8#discussion_r4338979651`, `https://github.com/Copias-a-i/hermes-agent/pull/8#discussion_r4338979663`, and `https://github.com/Copias-a-i/hermes-agent/pull/8#discussion_r4338979672`. COP-763 blocks COP-751, COP-754, COP-755, COP-756, and COP-757 activation outcomes.

### Per-profile deployed-seam gate

Before enabling **each** profile, and before that profile's config write or owning runtime reload, run a successful deployed-production-adapter Pinecone query in the exact trusted profile/runtime/host namespace and exact target metadata bucket. This must traverse the production adapter path; unit/mock tests, an admin CLI, direct wrapper invocation, and historic migration receipts are insufficient. The read-only receipt must bind release/wrapper digests, trusted identity/namespace digest, exact filter, returned record IDs/deterministic hash, source and relevance labels, `topK <=3`, read-only classification, latency versus budget, and context/cost. Returned records must satisfy the exact filter and have no cross-profile/runtime-host or legacy/unbucketed leakage.

`empty-bucket` and `no-result` are explicit truthful **non-pass** outcomes, never availability or activation PASS. Seed/test data is permitted only if already approved, non-PII, separately labeled, and never counted as production proof.

### Durable migration ledger and reversible exact-bucket rollback

Before migration, snapshot every affected exact bucket with deterministic count, sorted IDs, and hashes. Record every migration write in durable crash-safe ledger storage. On rollback, idempotently delete newly partitioned records or restore captured prior metadata/state for every ledger entry. Then query/read back every affected exact bucket through the production query path and require counts, IDs, and hashes equal the pre-migration snapshot. Inject crashes before and after ledger phases and retry migration/rollback to prove convergence without duplicates or loss. If reversal is partial, a ledger entry is absent, or any comparison/readback fails, state `rollback-incomplete`, keep the adapter disabled, and block activation.

### Per-profile activation matrix and signed gate

Maintain a matrix keyed by profile and by runtime/host adapter where partition differs. Each enabled row records its baseline; trusted identity/namespace; deployed-seam query receipt and result classification; latency/context/cost; migration state; rollback snapshot proof; release/wrapper digests; and signed Colby approval. A PASS from one profile or cohort never authorizes another. Every profile config write and reload must check its own current PASS row; missing, stale, mismatched, empty/no-result, or unsigned rows cause zero writes/reloads.

### COP-763 static contract and independent proof

Focused static contract must reject mock/admin/direct-wrapper evidence as deployed-seam proof; `topK >3`, write-capable request, wrong/spoofed identity/bucket, legacy or cross-profile result, result-label mismatch, budget breach, PII/unapproved seeds, no-result/empty bucket PASS, missing ledger/snapshot, partial reversal, mismatch, crash retry non-idempotence, and cross-profile activation inheritance. Required proof is `python scripts/check --base upstream/main`, focused static contract, full COP-751/COP-754/COP-755/COP-756/COP-757/COP-763 Linear body/relation readback, and `git diff --check`; then verify PR exact head/file scope, reply and resolve these three threads, prove zero unresolved, and request a fresh exact-head independent review. No implementation, runtime/config activation, credential action, migration mutation, merge, or production probe occurs under this plan-only PR.

## Completion criteria

- Exact-head independent review has no Critical/Important unresolved finding after the scoped COP-759/COP-760/COP-761/COP-763 architecture follow-up gate; no ordinary repair round is reopened.
- COP-752..763 Linear bodies, assignment, team, status, dependencies, rollback owner, and manifest are read back one-to-one.
- No implementation, runtime reload, config activation, credential, or merge action occurs under this plan-only PR.
