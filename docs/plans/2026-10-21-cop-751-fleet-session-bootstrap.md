# COP-751: portable low-token session-start bootstrap — implementation plan

**Status:** plan only; implementation is blocked until exact-head independent security review passes.

## Goal

Deploy one portable session-start bootstrap across the four fleet Macs and every discovered agent runtime. It must set Caveman full style without loading its full skill text and add a compact, read-only Graphify/Pinecone retrieval receipt when available. It must improve measured startup-context cost; no savings claim is valid before baseline evidence exists.

## Decision

Use the audited fleet capability library at `$HOME/Agents/fleet-capabilities/`: canonical skill source is `library/skills/{caveman,graphify,pinecone-memory}` and the current cross-runtime distribution surface is `adapters/{agents,claude,codex,cursor,gemini,grok,hermes,openclaw}/skills/`. Add a fleet-owned canonical Python bootstrap there, rather than add a product-specific integration to Hermes core. The bootstrap owns discovery, query derivation, bounded retrieval, sanitization, cache/receipt handling, timing, and compact rendering. Runtime adapters only translate native session metadata to a normalized request and place the returned receipt/context pack into their supported session surface.

Hermes uses its public `on_session_start` shell-hook/plugin path; it does **not** mutate a discarded hook context. Claude Code and Codex use their existing `SessionStart` command-hook surfaces. Cursor needs an adapter only after a fresh capability check confirms a supported start-event hook; the audited Studio/Worker/Main configs expose only `sessionEnd` and `preCompact`, so no unsupported start hook will be invented. Antigravity, Gemini, and Grok each require an adapter-specific discovery gate: no runtime presence means no config write and an explicit `not-installed` host/runtime receipt.

## Audit evidence and inventory

### Fleet hosts

| Host | Verified transport | Runtime findings | Graphify inventory |
|---|---|---|---|
| Studio | local `Jarviss-Mac-Studio.local` | Hermes (40 profile directories), Claude Code, Codex, Cursor; no verified Antigravity/Gemini/Grok executable | local scan required again during implementation; previous broad scan timed out |
| Worker | `ssh worker` | Claude Code, Codex, 38 Hermes profiles; Cursor config exists but no `cursor-agent` binary | 50 `graphify-out/graph.json` files under `~/Projects`; Pinecone wrapper present |
| Main | `ssh main` | Hermes, Claude Code, Codex, Cursor Agent, Gemini | 22 graph files; Pinecone wrapper present |
| MacBook | `ssh macbook` | Hermes command, Claude Code, Codex, Antigravity (`agy`); no Hermes profiles returned | Graphify graphs in `~/Projects`; no wrapper confirmation because probe exited early |

The fleet topology assigns Hermes gateways to Studio, build/merge work to Worker, orchestration to Main, and mobile work to MacBook. Usernames differ; all implementation paths must use `$HOME`, never a hardcoded `/Users/<name>`.

### Current session-start surfaces

| Runtime | Current verified surface | Adapter decision |
|---|---|---|
| Hermes | profile `config.yaml`: `hooks.on_session_start`; Jarvis currently calls `~/.hermes/scripts/vault-writeback-reminder.sh`; logs show prior script may emit invalid non-JSON output and runs with 60s timeout | add profile-local wrapper registration through supported config/consent flow, retain existing hook, set explicit <=7s adapter timeout, reload gateway and prove loaded state |
| Claude Code | `~/.claude/settings.json` `SessionStart`: comms-style, fleet-session-start, disk-health hooks | append a single short wrapper command after preserving existing entries; wrapper emits supported compact context/status only |
| Codex | `~/.codex/hooks.json` `SessionStart`: fleet session and disk health; plugin hook registry also active | append single wrapper command after existing entries; do not alter plugin states or end hooks |
| Cursor | `~/.cursor/hooks.json` contains `sessionEnd` and `preCompact` only | capability-discovery ticket first. If native session-start hook is unavailable, use supported project/global instruction pointer only and record adapter `unsupported`; never use polling, UI automation, or a fake hook API |
| Antigravity | `agy` binary only observed on MacBook | inspect installed version/config docs; adapter only if canonical start hook exists |
| Gemini | `gemini` binary only observed on Main | inspect installed version/config docs; adapter only if canonical start hook exists |
| Grok | no executable found; fleet directive says xAI API path is inert due to zero credits | mark `not-installed/inert`; do not provision credentials or a new MCP |

### Existing capability facts

- Caveman is already a standing instruction in Claude/Codex/Cursor global instruction documents. Its full SKILL.md is too large to load automatically; inject a one-line policy receipt/pointer only.
- Graphify's `graphify query` reads an existing `graphify-out/graph.json`; startup must never trigger detect/extract/update/build/cluster/export/watch.
- `~/.claude/pinecone_memory.py` is the existing wrapper. Its query subcommand uses `claude-memory`, does embedding plus `/query`, and can output JSON. It also contains store/delete modes, so the bootstrap must allow only the exact `query` argv shape and test that no write endpoint is callable.
- Existing Pinecone wrapper resolves a key from environment, `~/.hermes/.env`, or LastPass. The bootstrap may invoke it only if executable/readable and must never print command stderr that could reveal sensitive material.

## Canonical contract

### Inputs

The adapter provides a JSON request over stdin only:

```json
{
  "schema": 1,
  "runtime": "codex",
  "host": "macbook",
  "session_id": "opaque-or-empty",
  "cwd": "/absolute/path",
  "objective": "bounded, redacted session objective",
  "branch": "optional branch"
}
```

The bootstrap derives project identity only after canonical-path validation: resolve `cwd`, locate Git root, reject worktree escape/symlink ambiguity, derive repo name and branch with bounded subprocess calls. Objective is truncated before any external query. Adapter input is untrusted data; no shell interpolation.

### Graphify path

1. Look only for `<project-root>/graphify-out/graph.json`; outside a Git project, mark `unavailable:not-project`.
2. Validate graph file is a regular file, limited size, UTF-8 JSON, and structurally readable before query. Malformed/oversize graph becomes `unavailable:invalid-graph`.
3. Construct a deterministic, compact local query from role/runtime, repo identity, branch, and bounded objective. Execute the installed Graphify query command with an argv list, no shell, network disabled by contract, `timeout=3s`, and a strict output byte cap.
4. Do not invoke graph construction/update/cluster/export/index mutation commands. A project with no graph records `unavailable:no-graph` and skips the subprocess.

### Pinecone path

1. Build one generic compact query from runtime role, project identity, branch, and bounded objective; remove secrets/identifier-like fields before request.
2. Invoke only `[python, ~/.claude/pinecone_memory.py, "query", query, "--top-k", "3", "--json"]` through an argv list. No bucket unless future requirements add one explicitly.
3. Apply total 5s Pinecone deadline, at most one retry only for transient timeout/429/5xx within the 7s total startup budget. Credential absent becomes `unavailable:no-credential`; never cause an interactive LastPass prompt or a session block.
4. Accept JSON only. Label every kept item `source:pinecone`, relevance score, and origin metadata. Treat retrieved text as untrusted data; strip instruction-shaped content into a quoted data section, redact secret/PII patterns, and truncate before rendering.
5. Never execute `store`, `ingest-file`, `delete`, `fetch`, `stats`, or raw Pinecone calls. The implementation's command allowlist must reject every alternate subcommand before process launch.

### Context pack, cache, and receipts

- Render a stable, non-authoritative compact pack: `style=caveman-full`, `graphify=<available|unavailable:reason>`, `pinecone=<available|unavailable:reason>`, then labeled data excerpts. Include pointers to existing skills/config rather than their contents.
- Cap combined injected retrieval at 800 tokens using a deterministic token/character conservative budget; reserve explicit overhead for status labels. Graphify and Pinecone compete for the same cap; neither has a guaranteed allocation.
- Cache by `schema + bootstrap-hash + runtime + repo git HEAD + branch + normalized objective digest + graph file stat/hash + query digest`, with short TTL and per-host directory under `$HOME/.fleet/session-bootstrap/`. A cache hit must return the identical safe rendered pack and a `cache=hit` receipt.
- Persist receipts and cache atomically with `0600` mode; no secrets, raw PII, full retrieval text, or credential errors. Receipts store hashes, source availability/reason, elapsed milliseconds, byte/token counts, truncation/redaction counters, adapter/version/hash, and cache status.
- Use a per-key lock or atomic create/rename so concurrent sessions coalesce safely; lock failure falls open to a no-retrieval receipt rather than waiting beyond budget.
- All bootstrap cache/receipt writes live outside source worktrees. The bootstrap itself has no session-start graph/Pinecone write side effects except these local receipts/caches.

### Time and failure contract

- Whole adapter deadline: <=7s. Graphify allocation <=3s. Pinecone allocation <=5s, but it receives only remaining whole-budget time.
- Any invalid input, unavailable executable, malformed output, timeout, nonzero exit, lock collision, or adapter integration error returns exit 0 plus a visibly labeled compact `unavailable:<reason>` receipt.
- Adapter wrappers must be idempotent, preserve pre-existing hook order/behavior, and avoid leaking full stdout/stderr to user context or logs.

## Files and implementation tasks

### T1 — canonical bootstrap library and contract

**Target:** canonical fleet skill/library source discovered during implementation; if no existing tracked source owns cross-runtime wrappers, create a dedicated `fleet-session-bootstrap` repository under the organization rather than adding a vendor-specific plugin to Hermes core.

1. Add `src/fleet_session_bootstrap.py` (or equivalent stdlib-only module) and `bin/fleet-session-bootstrap`.
2. Implement stdin JSON validation, project discovery, objective normalization, subprocess allowlists, deadline budget, sanitization, cache/receipt atomic writer, and stable renderer.
3. Add a machine-readable adapter manifest with content hash/version and expected runtime discovery paths.
4. TDD: parser/allowlist before implementation; unit tests first for unsafe argv, invalid CWD, cache key stability, file modes, lock behavior, and fail-open result shape.
5. Commit separately: `feat(fleet): add bounded session bootstrap core`.

### T2 — Graphify reader

1. Add read-only graph existence/validity checks and the bounded Graphify query executor.
2. Prove the only Graphify subprocess is query mode and runs only for a valid existing graph.
3. TDD: absent graph, malformed JSON, symlink/non-regular graph, query timeout, oversized output, malicious result text, and graph query response truncation.
4. Commit separately: `feat(fleet): add bounded graphify startup reader`.

### T3 — Pinecone reader

1. Add the exact Pinecone wrapper query executor and enforce command/subcommand allowlist before spawn.
2. Add credential/noninteractive handling, JSON-only parser, bounded retry classifier, source/relevance labels, redaction, and data-only prompt-injection treatment.
3. TDD with local fake wrapper/server: no key, timeout, 429, 500, malformed JSON, malicious/PII-rich content, oversized output, and assertion that no `/vectors/upsert` or `/vectors/delete` request occurs.
4. Commit separately: `feat(fleet): add guarded pinecone startup reader`.

### T4 — adapters and host inventory

1. Implement adapters only for proven native hook APIs. Each adapter calls canonical bootstrap once and emits/places only the compact pack or pointer its runtime supports.
2. Hermes: per-profile config adapter registration through `hermes config`; test a disposable profile, preserve existing entries, restart/reload affected Studio gateways only after plan gate approval, and read back loaded hook logs.
3. Claude Code/Codex: use the current `SessionStart` JSON hook files, preserve entries and plugin state; test structural JSON and actual local dry run.
4. Cursor/Antigravity/Gemini/Grok: add a discovery manifest and negative `unsupported/not-installed` receipts. Only introduce a write adapter after official/current runtime documentation plus an executable dry run proves a canonical start hook.
5. Add host/profile inventory generator that reports all detected active profiles/runtimes and their adapter state without reading credentials.
6. Commit separately per adapter family to keep rollback scoped.

### T5 — installer, propagation, rollback

1. Package immutable artifact hash/variant and signed-or-hashed manifest. Install to `$HOME/.fleet/session-bootstrap/releases/<hash>/`; flip a `current` pointer atomically, never execute from a worktree.
2. Propagate through canonical SSH aliases in order: Studio, Worker, Main, MacBook. Every remote command proves hostname, artifact SHA, file modes, adapter discovery result, and readback version.
3. Keep adapters disabled until their per-host dry run is green. Gateway reload semantics: Hermes hook registry is process-cached, so restart/reload only the owning profile gateway after a successful install; CLI/Claude/Codex/Cursor require new sessions and a synthetic start test.
4. Roll back atomically to prior release pointer plus exact previous config fragments; restart/reload only affected gateways; re-read adapter/config hash and generate rollback receipt.
5. Never copy credentials, build graphs, populate Pinecone, or alter source worktrees.

### T6 — measurements and acceptance evidence

1. Baseline each runtime/host using existing session-start config and capture: preexisting injected startup bytes/tokens, new compact pack bytes/tokens, elapsed time, cache state, Graphify/Pinecone availability, and excluded full-skill bytes.
2. Measure token count with one version-pinned deterministic counter declared in receipts. Compare per runtime and whole fleet. A pass needs measured injected context lower than baseline (or a transparent `baseline-unavailable`), not a projected percentage.
3. Run host/runtime synthetic dry runs, no-network Graphify negative control, fake Pinecone server controls, concurrent-session race test, adapter reload checks, and independent security review.
4. Produce immutable receipts bound to artifact hash, adapter version, host, runtime, and test fixture digest.

## Test matrix

| Control | Required assertion |
|---|---|
| no graph / cwd outside project | Graphify skipped; receipt gives precise unavailable reason; exit 0 |
| malformed/oversized/symlink graph | no query process; safe unavailable receipt |
| valid graph | one bounded local query; no graph writes/network/build/update/cluster |
| no Pinecone key/LastPass unavailable | no interactive prompt; skip cleanly |
| Pinecone timeout, 429, 500 | bounded retry policy; total <=7s; exit 0 |
| malicious/PII retrieval | treated only as quoted data; injection markers neutralized; secrets/PII redacted |
| oversized result | deterministic truncation under combined <=800-token cap |
| command mutation probe | alternate Pinecone subcommands rejected; fake transport sees zero upsert/delete calls |
| full skill preload probe | pack contains policy pointer/receipt only; no SKILL.md body contents |
| existing config | byte/semantic preservation outside added adapter entry |
| concurrent sessions | singleflight/cache correctness; no corrupt receipt/cache; no deadline breach |
| repeated same session | cache hit, identical safe pack, no duplicate remote Pinecone call |
| hook/bootstrap failure | runtime remains usable; visible unavailable marker; exit success |
| adapter discovery | every installed runtime has an explicit supported/unsupported/not-installed outcome |
| end-to-end dry run | actual adapter invokes current artifact by immutable hash and produces readback receipt |

## Security review gate

A reviewer independent of the builder must review the exact committed plan and, before implementation, the exact implementation head. Review scope: command injection, objective/metadata privacy, retrieval prompt injection, cache path traversal and modes, race safety, Graphify network/write prohibition, Pinecone query-only enforcement, credential handling, output/log redaction, adapter config preservation, gateway reload/rollback, and host propagation integrity.

Repair cap is **two** exact-head plan repair rounds. A third new trust-boundary finding creates a named Linear follow-up (`COP-751-security-boundary-follow-up` unless an existing issue is linked), blocks implementation, and requires a decomposed design rather than further prose patching.

## Rejected alternatives

1. **Preload Caveman/Graphify/Pinecone skill bodies into every system prompt.** Rejected: directly increases startup tokens and defeats objective.
2. **One universal hook file/API.** Rejected: audit shows Hermes, Claude/Codex, Cursor, and Antigravity differ; unsupported APIs are unsafe and brittle.
3. **Build/update/cluster Graphify at session start.** Rejected: nondeterministic, potentially expensive, writeful, and exceeds latency budget.
4. **New Pinecone MCP/provider/credentials.** Rejected: existing wrapper/index suffices; new integration widens attack and cost surface.
5. **Always retrieve from Pinecone.** Rejected: vendor call/cost and sensitive retrieval risk; cache, credential availability, tight budget, and clean skip are required.
6. **Embed code in every existing hook.** Rejected: four-way drift; a single canonical implementation plus shims is auditable and rollbackable.
7. **Cursor UI automation/polling fallback.** Rejected: not a canonical session start API and can make sessions unreliable.
8. **Hermes core change or bundled vendor plugin.** Rejected: Hermes contribution rules require edge capability; fleet-specific Pinecone integration belongs outside core.

## Rollout matrix

| Host | Runtime | Initial state | Install action | Activation proof | Rollback |
|---|---|---|---|---|---|
| Studio | Hermes profiles | active gateway host | immutable release + profile-local adapter after inventory | gateway log/load + dry run receipt | restore config fragment/release pointer + gateway restart |
| Studio | Claude/Codex/Cursor | active configs | wrapper/config only where supported | new-session dry run/readback | restore JSON backup fragment/release pointer |
| Worker | Claude/Codex/Hermes profiles | build host | immutable release; no daemon disruption | dry run, no graph/Pinecone writes | pointer/config revert |
| Main | Hermes/Claude/Codex/Cursor/Gemini | orchestration | immutable release + supported adapters | per-runtime dry run | pointer/config revert |
| MacBook | Hermes CLI/Claude/Codex/Antigravity | roaming | immutable release + supported adapters | local dry run when online | pointer/config revert |
| all | Grok | inert/not installed | none | explicit not-installed receipt | none |

## Completion criteria

- Plan committed alone, pushed, PR opened non-draft and linked to COP-751.
- Independent exact-head plan/security review reports no Critical/Important unresolved findings within two repair rounds.
- No source implementation starts until that gate is terminal pass.
- Implementation tickets are created from T1–T6, each with tests, owner/runtime scope, and rollback acceptance.
