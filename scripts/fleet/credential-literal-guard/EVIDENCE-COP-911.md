# COP-911 allowlist evidence (self-improving-dev, 2026-11-26)

Hashes, shapes, paths, lengths only. No credential values.

## Method

- Detector core loaded from `no-credential-literals-synced.sh` (same bytes the guard hashes).
- LastPass: `lpass export --fields=password,extra` hashed in memory (1108 token hashes); positive control 40/80 env hashes present in export set.
- Env: 464 value hashes from `~/.hermes/.env`, profiles, `~/Projects/*/.env*`, `~/.claude/.ingest-env`.
- FAKE only if absent from LastPass export AND env AND source fixture located.

## Verdicts

| sha12 | shape | full_len | path class | env | LastPass | verdict | action |
|---|---|---|---|---|---|---|---|
| 457643f44d19 | akia | 20 | security-guidance `test_patterns_file_shape.py` | absent | absent | FAKE | EXEMPT path pin + allowlist row |
| c3f6af301323 | xox | 24 | agent-memory-synced `memory.db` | absent | absent | FAKE | allowlist row |
| 6f1ec2d089f4 | xox | 25 | agent-memory-synced `memory.db` | absent | present (shaped+field) | REAL | same-length redact; never allowlist |

## Full sha256 (safe)

- akia: `457643f44d19aed85fd756aa50cc0cd6b57376d4e8f5a72f9f85972a522002a3`
- xox fake: `c3f6af3013237ef35c87136896518a6792862b3262429f16264e8ba5fefa9e6c`
- xox real: `6f1ec2d089f463b078f9a07751fdf066390c35279c681b4b8920a25a63b28c36`

## Negative controls (automated)

`tests/cop911-credential-literal-guard.test.sh` N1–N3: empty allowlist, wrong shape, missing row → RED for a runtime-built real-shaped `glpat-`.
