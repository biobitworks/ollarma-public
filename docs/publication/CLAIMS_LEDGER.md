# Ollarma — Claims Ledger

**Purpose:** every claim the publication evidence bundle asserts, classified by claim-ceiling tier, with the artifact that supports it and any open dependency. No claim is promoted above its evidence.

**Tiers** (from gsigmad claim-ceiling-review): `DIRECT` (observed in this session) · `MEASURED` (recorded in a committed artifact, host/time-specific) · `INFERRED` (reasoned from measured data) · `OPEN` (asserted nowhere until evidence lands).

| # | Claim | Tier | Support | Open dependency |
|---|-------|------|---------|-----------------|
| C1 | The full test suite passes with 0 failures (1672 passed / 1 deselected). | DIRECT | This session's `pytest -q` run, 2026-06-10. | none |
| C2 | The bridge has no error cliff in any reachable load regime (0 errors → 256 req / 128 workers). | MEASURED | `clients/STRESS_FINDINGS.md` concurrency table. | host/version-specific |
| C3 | Inference is serialized; past ~48 workers p50 plateaus (~35–37 s) and only tail latency grows. | MEASURED + INFERRED | concurrency table (measured); serialization mechanism inferred from `local_inference_single` lane. | none |
| C4 | GPU decode is 2.5–4.7× faster than CPU; CPU fallback never failed at any tested size. | MEASURED | `clients/gpu_vs_cpu_bench.py` results. | only 4 models tested |
| C5 | The 3-model count cap (`OLLAMA_MAX_LOADED_MODELS` default) binds before the ~23–24 GB memory ceiling. | MEASURED | `experiments/coresidency/` (29 states, pre-registered CONFIRMATORY). | host/version/contention-specific |
| C6 | Ollama evicts rather than spilling to CPU; swap never grew across 29 states. | MEASURED | `experiments/coresidency/results.jsonl`. | untested-because-unreached (not a proof of impossibility) |
| C7 | Every model 1.5B→30B runs all-GPU alongside the pinned ~2.71 GB floor on this host. | MEASURED | `experiments/coresidency/MATRIX.md` footprint table. | does not generalize to 16 GB hosts |
| C8 | The model-size→role ladder (sensor/floor/router/escalation/reasoning/ceiling) is grounded in live probes. | MEASURED_ADVISORY | `docs/EVIDENCE_RTB02_ESCALATION_LADDER_CELLICO_BIO_2026-05-31.md`. | advisory; policy seed not universal benchmark |
| C9 | The predictive deliberative swarm passes 5/5 pre-registered MESI gates. | MEASURED | commit `420d2ae`, PROMPT-OLLARMA-SWARM-001. | none for the 5 gates |
| C10 | The inert-control strict-cleanliness sub-criterion is met (criterion #5 PASS). | **RESOLVED FALSE** | EXP-002 (2026-06-10) **falsified** the engine: 2/5 inert prompts > 0.05 (random_tokens 0.0810, bland_factual 0.0858). Criterion #5 BLOCKED. | none — definitively not met; remediation gated on new pre-registration |
| C11 | Capacity/latency results imply nothing about model *quality* for a tier. | DIRECT (scope statement) | bundle §0 + coresidency claim ceiling. | quality needs separate selection harness |

## Forbidden / explicitly-not-claimed

- ❌ Any universal-hardware claim ("models co-reside up to 24 GB" is **this host only**).
- ❌ Any model-quality or tier-suitability claim from capacity/latency data.
- ❌ Criterion #5 PASS, or v5.1 "fully shipped," until C10's dependency resolves.
- ❌ "Two ≥24 B models cannot co-reside on any 32 GB host" — measured here as exceeding *this* budget; not a hardware theorem.

## C10 resolution (CLOSED 2026-06-10)

EXP-002 live calibration completed → verdict **`FAIL` (engine falsification)**. PI selected **Branch C** in `docs/publication/CRITERION5_PI_DECISION.md`: criterion #5 stays BLOCKED, Phase 70 clean closeout STOPPED, registered as a negative result (`experiments/NEGATIVE_RESULTS_REGISTRY.md`). Remediation hypothesis (cold-start aggregation) requires a NEW pre-registration + fresh T9c; no silent patch-and-rerun. C10 is resolved FALSE — this is the verification layer's falsification gate working as designed, not a defect in the evidence process.
