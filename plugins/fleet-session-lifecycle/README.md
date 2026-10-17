# Fleet session lifecycle external outbox

This bundled lifecycle plugin checkpoints redacted, derived turn summaries locally. It does **not** bundle or name a vendor connector.

## Optional external adapter

Install any indexing or archival adapter as a standalone plugin under `~/.hermes/plugins/` (or as a pip entry point), then validate that external plugin before enabling it:

```sh
hermes plugins validate ~/.hermes/plugins/<external-outbox-adapter>
```

The lifecycle config remains disabled until it explicitly opts in. The adapter receives a durable JSONL path only through `--input`; it must accept `event_id` as an idempotency key and acknowledge every record it has consumed.

```yaml
fleet_session_lifecycle:
  enabled: true
  outbox_command: ["<external-outbox-adapter>"]
  outbox_timeout_seconds: 5
```

When the adapter is absent or fails, lifecycle processing remains fail-open: the redacted records stay in `state/fleet-session-lifecycle-outbox.jsonl` for a later turn. The lifecycle plugin never passes the vault path, credentials, or raw conversation history to an adapter.

Every record in that file is one complete LF-terminated JSON object. A record torn by a crash mid-write is repaired before the next enqueue, so an adapter never has to parse past a partial line — its event is replayed from the still-pending durable admission instead.
