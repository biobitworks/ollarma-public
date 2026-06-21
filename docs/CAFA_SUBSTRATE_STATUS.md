# CAFA Verification Substrate — Ollarma lane status

**Updated:** 2026-06-11 · **Lane:** Ollarma (deterministic substrate + orchestration)
**Prompts:** `prompts/PROMPT_CAFA_OLLARMA_LANE.md` · **Boundary:** `docs/LOCAL_VS_ONLINE_BOUNDARY.md`

## Built (local-first, tested, committed)

| # | Deliverable | Module | State |
|---|---|---|---|
| D1 | Local dataset registry + provenance | `cafa/dataset_registry.py` | ✅ real go-basic.obo registered (sha256, 48,321 terms); fail-closed bootstrap |
| D2 | Deterministic GO antibodies (rule_floor) | `cafa/go_guardrails.py`, `cafa/go_ontology.py` | ✅ stdlib-only OBO DAG; validity/score/cap/partition/propagation; validated on real GO |
| D3 | Local citation resolution | `cafa/citation.py` | ✅ resolves PMIDs vs local corpus; novel→`absolutely_necessary` gate (no auto-fetch) |
| D4 | Cascade orchestration | `cafa/cascade.py` | ✅ GO path fully deterministic; text path → escalation request to Antigence (frontier_allowed=false) |
| D5 | Distillation-pair collection | `cafa/distillation.py` | ✅ dedup JSONL collector; frontier-fraction tracking (local→local) |
| D6 | Verdict-provenance receipts | `cafa/receipts.py` | ✅ RTB-02 hash-chained, tamper-evident |
| D7 | Stress test (deterministic slice) | `scripts/cafa_stress.py` | ✅ see below |
| D8 | MCP/RAG oracle (boundary measurement) | `cafa/oracle.py` | ✅ harness built (agreement/accuracy/MCC, proven_local vs needs_online); LIVE numbers need the Antigence model verdicts |

**Tests:** 46 cafa unit tests; full suite green / 0 failures. **All pure-local: 0 model, 0 network, 0 frontier in steady state.**

## What is "done" vs "needs live inputs"

All eight deliverables' **logic + interfaces are built and tested** (fixture-driven
where they consume downstream inputs, exactly like D2 against a fixture ontology).
What remains is **live inputs**, not logic:
- D3: wire the real SciFact/CAFA abstract corpus (one-time download) into `LocalAbstractCorpus`.
- D8 + calibration: feed the Antigence lane's local model verdicts (claim_entailment / overclaim / sequence_grounding) on the labeled corpus → real agreement/MCC → the measured boundary.
- Full-pipeline stress: rerun `cafa_stress` once the model lanes are in the cascade.

## D7 stress result (2026-06-11, deterministic path, real go-basic.obo)

```
48,321 terms loaded in 0.29s
2000 proteins x 10 terms = 20,000 input rows
verified in 1.39s -> 1,439 proteins/s, 14,391 input-rows/s
propagation: 20,000 -> 338,232 rows (16.9x ancestor blow-up)
receipt chains valid (sample 50): True
model/network/frontier calls: 0 / 0 / 0  (fully local)
```

The deterministic floor sustains ~14k GO-prediction rows/sec with full
propagation + hash-chained receipts, entirely offline. Reproduce:
`python scripts/cafa_stress.py 2000 10`.

## Dependency boundary (what's left and why)

- **D8 (MCP/RAG oracle)** and the **full-pipeline stress** require the Antigence
  lane's **local model antibodies** (claim_entailment / open_world_overclaim /
  sequence_grounding) producing verdicts to grade and route. Until that lane is
  live, the deterministic substrate is complete and the orchestration spine emits
  the escalation requests it will consume.

## Import surface for the Antigence lane

```python
from ollarma.cafa import (
    GoDag, Prediction, verify_go_submission,   # deterministic GO path
    screen_text_claim,                         # emits escalation_request -> antigence
)
from ollarma.cafa.citation import resolve_citations, LocalAbstractCorpus
from ollarma.cafa.distillation import DistillationCollector
from ollarma.cafa.receipts import build_receipts, verify_chain
```

`screen_text_claim(...).escalation_request` → `{to_lane: "antigence",
antibodies: [...], target, text, cited_pmids, frontier_allowed: False}`.
