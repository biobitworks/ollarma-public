# RTB-05 Distillation Receipt Execution Plan

**Parent:** `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md`
**Status:** implementation plan, not shipped behavior.
**Scope:** make summaries and syntheses auditable, source-hash-backed, and safe
for Antigence safety/security review.

## Role

Distillation is where orchestration risk concentrates. A summary can hide
dissent, drop a safety finding, or sound like a promoted claim. RTB-05 requires
distillation to preserve provenance, uncertainty, omissions, and safety flags.

## Files To Add

| File | Purpose |
|---|---|
| `src/ollarma/distillation.py` | `DistillationReceipt`, source ref model, deterministic reducer facade. |
| `tests/test_distillation.py` | Receipt hashing, source refs, claim ceiling, safety preservation tests. |
| `docs/examples/distillation/README.md` | Operator example for replaying a distillation receipt. |

## Files To Modify

| File | Required changes |
|---|---|
| `src/ollarma/http_api.py` | Add future `POST /distill` only if explicitly scoped; otherwise keep library-only. |
| `src/ollarma/service.py` | Thin helper for library/CLI use if needed. |
| `docs/OLLARMA_SUBSTRATE_CONTRACT.md` | Document as v5.1+ additive once implemented. |

## Schema

```python
class DistillationSourceRef(BaseModel):
    source_type: Literal["chat", "route", "navigator", "swarm", "antigence_verdict"]
    ref_id: str
    content_hash: str
    claim_ceiling: str

class DistillationReceipt(BaseModel):
    schema_version: Literal[1] = 1
    distillation_id: str
    source_refs: tuple[DistillationSourceRef, ...]
    reducer_model: str
    prompt_hash: str
    summary_hash: str
    uncertainty: str
    omissions: tuple[str, ...] = ()
    safety_flags: tuple[str, ...] = ()
    claim_ceiling: Literal["helper", "advisory", "no_claim_promotion"]
    no_claim_promotion: bool = True
```

## Rules

- Distillation inputs must be referenced by id and hash.
- Distillation must preserve negative/safety findings from Antigence verdicts.
- Distillation cannot raise a claim ceiling above any source ceiling.
- Distillation cannot write EXP, KG, Antigence, Watchtower, Overwatch, or
  sibling-repo state.
- Distillation output is evidence for review, not canonical truth.

## Tests

- receipt id/hash is deterministic from canonical content.
- changing a source hash changes the distillation id.
- claim ceiling is the lowest/strictest source ceiling.
- `no_claim_promotion` defaults true and cannot be false for RTB-05.
- safety flags from source verdicts appear in the receipt.
- omissions and uncertainty are required for multi-source distillations.
- no-write guard proves distillation does not mutate external state.

## Acceptance Gate

RTB-05 is complete only when:

- `DistillationReceipt` tests pass;
- source replay from refs/hashes is possible;
- conflicting/safety lane findings are preserved;
- no claim-promotion and no-write guards pass;
- optional Antigence post-distillation review packet can attach later without
  changing receipt identity.

