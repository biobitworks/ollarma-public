# RTB-03 Reviewed Chat Execution Plan

**Parent:** `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md`
**Status:** implementation plan, not shipped behavior.
**Scope:** let `/chat` and `/route` request optional Antigence review while
preserving existing helper behavior and fail-closed execution boundaries.

## Role

Reviewed chat is the first place Antigence consumes Ollarma output in real
time. It should be optional for helper-only use and mandatory only when a later
workflow would mutate files, run code, or promote evidence.

## Files To Add

| File | Purpose |
|---|---|
| `src/ollarma/antigence_review.py` | `AntigenceReviewPacket`, verdict schema, review runner facade. |
| `tests/test_antigence_review.py` | Packet hashing, unavailable posture, pass/flag/block schema tests. |
| `tests/test_reviewed_chat_http.py` | `/chat` and `/route` review flag behavior. |

## Files To Modify

| File | Required changes |
|---|---|
| `src/ollarma/http_api.py` | Accept `review_with_antigence=true` on `/chat` and `/route`; preserve old response without the flag. |
| `src/ollarma/service.py` | Add thin reviewed-chat/reviewed-route helpers if needed. |
| `src/ollarma/guardrail.py` | Expose active antibody/lane metadata from RTB-02 resolver. |
| `docs/OLLARMA_SUBSTRATE_CONTRACT.md` | Document the optional flag as v5.1+ additive once implemented. |

## Schema

```python
class AntigenceReviewPacket(BaseModel):
    schema_version: Literal[1] = 1
    packet_id: str
    target_project: str | None = None
    request_surface: Literal["chat", "route"]
    prompt_hash: str
    response_hash: str
    model: str
    route_receipt_ref: str | None = None
    citations: tuple[dict[str, Any], ...] = ()
    antibody_lanes: tuple[AntibodyLaneSelection, ...] = ()
    claim_ceiling: Literal["helper", "advisory", "blocking", "no_claim_promotion"]

class AntigenceVerdict(BaseModel):
    schema_version: Literal[1] = 1
    packet_id: str
    status: Literal["pass", "flag", "block", "escalate", "unavailable"]
    authority: Literal["trusted", "advisory", "degraded", "escalation_only"]
    reasons: tuple[str, ...] = ()
    lane_verdicts: tuple[dict[str, Any], ...] = ()
    next_action: str
```

## Behavior

- Without `review_with_antigence`, `/chat` and `/route` behave exactly as they
  do today.
- With `review_with_antigence=true`, Ollarma builds a review packet after model
  output is available.
- If Antigence is unavailable:
  - helper-only `/chat` and `/route` return output plus explicit
    `antigence_unavailable` metadata;
  - any downstream mutating execution remains blocked until review is available
    or the operator explicitly routes to a governed alternative.
- `pass` returns the original response plus review metadata.
- `flag` returns the original response plus warning metadata and next action.
- `block` returns a blocked response for reviewed mode and emits a `blocked`
  bridge event when RTB-01 is available.
- `escalate` returns explicit handoff metadata; it never silently calls a
  frontier provider.
- A provisional/open-calibration antibody pack may return `flag` or
  `escalate`, but RTB-03 must not treat it as an authoritative `block` unless
  the verdict authority is `trusted`.

## Tests

- `/chat` response is unchanged without review flag.
- `/route` response is unchanged without review flag.
- review flag appends `review_packet` and `antigence_verdict` metadata.
- Antigence unavailable is explicit and non-crashing for helper-only requests.
- `block` verdict suppresses response text where required and returns recovery
  guidance.
- `flag` verdict preserves response text with warning metadata.
- review packet includes prompt/response hashes, model, citations, and antibody
  lane provenance.
- open-calibration antigen-bank `block` downgrades to advisory/escalation-only
  authority and cannot suppress downstream output by itself.
- no API keys, tokens, or raw provider payloads appear in review packets.

## Acceptance Gate

RTB-03 is complete only when:

- old `/chat` and `/route` tests remain green;
- reviewed mode tests pass for unavailable/pass/flag/block/escalate;
- review packets carry RTB-02 antibody lane provenance;
- verdict authority reflects calibration state;
- reviewed mode emits RTB-01 bridge events when the event spine exists;
- block verdicts can be used by workflow/autopilot guards before mutation.
