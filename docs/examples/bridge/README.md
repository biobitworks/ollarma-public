# Bridge Events Example

RTB-01 adds a finite, replayable localhost event spine for observation. It is
additive to existing `/chat` and `/route` behavior.

```bash
curl -fsS http://127.0.0.1:8484/bridge/events
curl -fsS -N http://127.0.0.1:8484/bridge/events/stream
```

Events are stored under `.ollarma/bridge/events.jsonl` below the active bridge
root. Payloads are redacted before hashing and storage.

This slice does not implement token-level model streaming or a long-lived live
tail. `/bridge/events/stream` replays stored events and then emits `event: done`.
