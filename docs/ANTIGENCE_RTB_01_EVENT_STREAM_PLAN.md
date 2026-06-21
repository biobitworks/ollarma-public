# RTB-01 Event Stream Execution Plan

**Parent:** `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md`
**Status:** implementation plan, not shipped behavior.
**Scope:** add a durable localhost bridge event spine that later RTB slices can
attach to for reviewed chat, project antibody lanes, KG navigation, and
distillation.

## Why This Comes First

The current HTTP SSE behavior wraps a completed response as one event and a
`[DONE]` marker. That is useful transport, but it is not a durable real-time
bridge. Antigence needs a replayable sequence of small, typed events so safety
review, orchestration, and distillation can reconstruct what happened without
scraping logs or model output.

RTB-01 should therefore ship only the event spine:

- typed bridge event schema,
- append-only JSONL store,
- redaction and hash discipline,
- recent-event query,
- SSE replay by last event id,
- compatibility hooks from existing `/chat` and `/route` without changing their
  JSON semantics.

## Files To Add

| File | Purpose |
|---|---|
| `src/ollarma/bridge_events.py` | `BridgeEvent`, `BridgeEventType`, store, redaction, append/list helpers. |
| `tests/test_bridge_events.py` | Schema/store/redaction/hash/replay unit tests. |
| `tests/test_bridge_event_http.py` | HTTP recent-events and SSE replay tests. |
| `docs/examples/bridge/README.md` | Tiny operator example for reading and replaying bridge events. |

## Files To Modify

| File | Required changes |
|---|---|
| `src/ollarma/http_api.py` | Add `GET /bridge/events`, `GET /bridge/events/stream`; emit coarse `/chat` and `/route` events. |
| `src/ollarma/service.py` | If needed, expose thin helpers for event root resolution; do not move business logic here. |
| `tests/test_sse_streaming.py` | Keep existing tests green; add assertions that old `/chat?stream=true` wrapper is unchanged. |
| `docs/OLLARMA_SUBSTRATE_CONTRACT.md` | Mark bridge events as additive v5.1+ planned/live once implemented, not part of v5.0 stable contract. |
| `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md` | Flip RTB-01 from planned to implemented only after tests pass. |

## Schema

```python
class BridgeEvent(BaseModel):
    schema_version: Literal[1] = 1
    event_id: str
    run_id: str
    parent_event_id: str | None = None
    event_type: BridgeEventType
    created_at: str
    source: Literal["chat", "route", "kb", "workflow", "swarm", "antigence", "distillation"]
    payload_hash: str
    payload: dict[str, Any]
    receipt_refs: tuple[str, ...] = ()
```

Initial event types:

- `chat_started`
- `chat_done`
- `route_started`
- `route_done`
- `kb_hit`
- `blocked`
- `escalated`

Reserved for later RTB slices:

- `chat_delta`
- `lane_transition`
- `antigence_review_started`
- `antigence_verdict`
- `distillation_done`

## Store Invariants

- Path: `.ollarma/bridge/events.jsonl` under the active repo/root.
- Append-only line-delimited canonical JSON.
- One event per line; no embedded newlines.
- `event_id` is stable: hash of canonical event content excluding
  `event_id`.
- `payload_hash` is hash of redacted payload only.
- Store append rejects event id collisions with different content.
- `list_events(after_event_id=None, limit=100)` returns stable file order.
- Unknown `after_event_id` returns an empty list with `after_event_id_found=false`
  metadata rather than replaying the whole stream accidentally.

## Redaction Rules

The bridge event payload is for real-time observation, not secret storage.

Redact at minimum:

- `Authorization`
- `api_key`
- `token`
- `password`
- `secret`
- `keychain_service` values only when they imply credential material
- provider raw request/response bodies from gateway calls

The redactor should recurse through dicts/lists and preserve keys with
`"<redacted>"` values so structure remains debuggable.

## Endpoint Behavior

### `GET /bridge/events`

Query params:

- `after_event_id`: optional string
- `limit`: default `100`, max `500`

Response:

```json
{
  "schema_version": 1,
  "events": [],
  "after_event_id": null,
  "after_event_id_found": true,
  "limit": 100
}
```

### `GET /bridge/events/stream`

Query params:

- `after_event_id`: optional string
- `limit`: default `100`, max `500`

Behavior:

- returns `text/event-stream`;
- emits stored events after `after_event_id`;
- emits `event: done` with final metadata;
- does not block forever in RTB-01. Long-lived live tail can be a later slice.

## `/chat` and `/route` Hooking

Do not change existing response schemas or status codes.

For `/chat`:

1. Append `chat_started` after request body validation.
2. Append `chat_done` after `service.chat_with_model` returns.
3. Append `blocked` on structured scheduler/backpressure failures.

For `/route`:

1. Append `route_started` after project/prompt validation.
2. Append `route_done` after `service.route_prompt` returns.
3. If route response contains citations/evidence, emit bounded `kb_hit`
   summaries, not full chunk text.
4. Append `blocked` or `escalated` for fail-closed or handoff outcomes.

## Tests

Unit tests:

- `BridgeEvent` validates required fields and rejects unknown event types.
- `payload_hash` changes when redacted payload changes.
- redaction recursively removes secret-like values.
- store append writes one line per event.
- store rejects embedded newline serialization.
- store list returns stable order.
- replay after a known id returns only newer events.
- replay after an unknown id does not replay all events.

HTTP tests:

- `GET /bridge/events` returns empty list on a clean temp repo.
- `GET /bridge/events/stream` returns event stream and done event.
- `/chat` non-streaming JSON response remains unchanged.
- `/chat?stream=true` existing response-wrapper behavior remains unchanged.
- `/chat` emits `chat_started` and `chat_done` events.
- `/route` emits `route_started` and `route_done` events.
- secret-like fields are redacted from stored bridge payloads.

Regression tests:

- Existing `/chat`, `/route`, `/workflow`, `/autopilot`, and
  `/gateway/submit` tests remain green.
- Existing `tests/test_sse_streaming.py` remains green.

## Acceptance Gate

RTB-01 is complete only when:

- bridge event schema and store tests pass;
- HTTP event endpoint tests pass;
- existing SSE tests pass;
- bridge events can be reconstructed from `.ollarma/bridge/events.jsonl`;
- docs state that token-level streaming is still not implemented unless a later
  slice adds it.

## Deferred

- Token-level streaming from Ollama chunks.
- Long-lived live-tail SSE.
- Antigence review packets.
- Project antibody lane execution.
- Distillation receipts.
- KG navigator endpoint.

