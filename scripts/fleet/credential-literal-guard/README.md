# COP-911 / COP-552 credential-literal guard — canonical source

Versioned home for the fleet credential-literal guard (COP-552 lock, COP-911 false-positive repair).

## Paths

| Role | Path |
|---|---|
| Scanner | `no-credential-literals-synced.sh` |
| Known-fake allowlist | `known-fake-hashes.tsv` |
| Tests | `tests/credential-literal-allowlist-summary.test.sh` |
| COP-911 lock + negative control | `tests/cop911-credential-literal-guard.test.sh` |
| Live install (Studio) | `~/bin/tests/no-credential-literals-synced.sh` + `~/.fleet/credential-literal-guard/known-fake-hashes.tsv` |
| Wrapper | `~/bin/credential-literal-guard.sh` (not shipped here; host-local) |

## COP-911 root cause (2026-11-26)

Guard RED on Studio with two file classes (values never logged):

1. **False positive (akia sha12 `457643f44d19`)** in
   `~/.claude/plugins/cache/claude-plugins-official/security-guidance/*/tests/test_patterns_file_shape.py`.
   Same demo fixture already EXEMPT-pinned to gstack brain-sync tests; path pin was too narrow.
   Fix: add `_SEC_GUIDANCE` EXEMPT pin + full-sha allowlist row (absent from LastPass + env).
2. **False positive (xox sha12 `c3f6af301323`)** in
   `~/.claude/plugins/data/agent-memory-synced/memory.db`.
   Proven absent from LastPass export and env stores. Fix: full-sha allowlist row.
3. **Real hit (xox sha12 `6f1ec2d089f4`)** in the same `memory.db`.
   Scanner-shaped literal present in LastPass export (field + shaped hit). **Not allowlisted.**
   Host cleanup: same-length redact via `~/Sandbox/scripts/cop526_redact.py` after LastPass confirmation.
   Prior exhausted attempt used padded fake full-hashes (`…1111`); those are rejected by the loader and by tests.

## Fail-closed rules

- Allowlist row requires full 64-hex sha256 + known shape + reason.
- Real LastPass-backed values must never be allowlisted.
- Negative control: random real-shaped token with empty/wrong allowlist stays RED.

## Test

```bash
./tests/cop911-credential-literal-guard.test.sh
./tests/credential-literal-allowlist-summary.test.sh ./no-credential-literals-synced.sh
```

No Cursor cloud agents. No paid hosted runners. No secret values in logs or PR text.
