# Ollarma — Publication Evidence Bundle

**Compiled:** 2026-06-10 (UTC) · **Operator role:** PI/PM/Operator/SWE consolidation pass
**Host of record:** Mac Studio M1 Max (`Mac13,1`), 32 GB unified memory, 8P+2E · Ollama 0.24.0 (MLX, `OLLAMA_FLASH_ATTENTION=1`)
**Bridge:** Ollarma @ `127.0.0.1:8484`, state `ready` at compile time
**Claim ceiling (binding for the whole bundle):** **MEASURED, host-specific, time-specific.** Every quantitative result below is a fact about *this* host, *this* Ollama version, *this* model roster, under *this* peer-contention load. None are universal-hardware or universal-model claims. No causal or quality-ranking claim is made from capacity/latency data. Co-residency and throughput depend on Ollama version, `num_ctx`, quantization, and concurrent peer load — all of which vary.

---

## 0. What this bundle is (and is not)

This is the consolidated evidence backbone for a **systems/engineering publication** about `ollarma`: a bounded, offline, local-model execution substrate with deterministic guardrails, receipt-chain auditing, and an explicit small-model-swarm backup runtime. It documents *what was measured*, *how to reproduce it*, and *the exact boundary each measurement located*.

It is **not** a claim of novel ML/algorithmic discovery, and it is **not** a model-quality leaderboard. Quality/selection ranking is a separate artifact (the benchmark selection harness, currently `SELECTION` recompute-on-demand) and is deliberately out of scope here. This bundle answers capacity, resilience, determinism, and orchestration-boundary questions only.

The five evidence pillars:

| # | Pillar | Question answered | Primary artifact | Verdict |
|---|--------|-------------------|------------------|---------|
| P1 | Harness + runtime correctness | Does the substrate behave as specified across all modules/scripts? | full pytest suite | ✅ 1672 passed / 1 deselected / 0 failures |
| P2 | Concurrency resilience | Where is the error cliff under load? | `clients/stress_test.py` | ✅ no error cliff in any reachable regime (0 errors → 256 req / 128 workers) |
| P3 | Placement / throughput | What does GPU vs CPU fallback cost? | `clients/gpu_vs_cpu_bench.py` | ✅ GPU 2.5–4.7× faster decode; CPU fallback viable at every size |
| P4 | Capacity / co-residency | How many models of what sizes co-reside before a hard limit? | `experiments/coresidency/` | ✅ limit located: 3-model **count cap** binds before the ~23–24 GB memory ceiling |
| P5 | Swarm acceptance + calibration + remediation | Does the deliberative swarm meet its pre-registered MESI gates? | `scripts/swarm_acceptance.py`, `scripts/swarm_calibration.py`, `PROMPT-004` | ❌ Two honest negative results: EXP-002 **FALSIFIED** the engine (confabulates stance from noise); EXP-004 remediation **NOT REMEDIATED** (abstention fix cleared inert to 0.0000 but lobotomized real debate). Criterion #5 **re-scoped to v5.2**; v5.1 ships its other 5 criteria. |

---

## P1 — Harness + runtime correctness (full test suite)

**Measurement (2026-06-10):** full offline suite, current host.

```
1672 passed, 1 deselected, 1 warning in 94.37s
```

- 1 deselected = the documented live seed-determinism probe (`-m live`), skipped offline by design (M1 MLX run-to-run variance is documented; see `README.md` §Seed Determinism and commit `20edf4f`).
- 0 failures across every module: gateway admission/rate-caps, receipt store, fair-share queue, recovery packet, swarm lanes (planner/executor/reviewer/synthesizer), notebook workflow, antibody-model-policy, MCP server, HTTP API, dashboard, sibling reader, overwatch adapter, SWE-bench ingest/leaderboard, embeddings, idempotency.
- The suite grew from 1618 (last STATE snapshot, 2026-05-31) to **1672** with zero regressions — every module and script carries live coverage.

**Boundary located:** the harness itself is regression-clean. No module ships without test coverage; the one live-gated test is documented variance, not a failure.

**Reproduce:** `pytest -q` (offline) · `pytest -m live -v` (requires Ollama).

---

## P2 — Concurrency resilience (no error cliff)

**Measurement (2026-05-31 → 06-01, same host):** `clients/stress_test.py`, escalating load through the live bridge.

| load (req / workers) | ok | errors | p50 | p95 | max | wall |
|---|---|---|---|---|---|---|
| 24 / 6   | 24  | 0 | 3.25 s | 6.47 s | 6.83 s | 15.7 s |
| 40 / 12  | 40  | 0 | 5.36 s | 15.90 s | 16.12 s | 24.7 s |
| 80 / 24  | 80  | 0 | 13.19 s | 28.50 s | 31.48 s | 49.6 s |
| 150 / 48 | 150 | 0 | 35.25 s | 77.45 s | 86.30 s | 120.6 s |
| 200 / 64 | 200 | 0 | 35.69 s | 87.90 s | 97.46 s | 133.2 s |
| 256 / 128| 256 | 0 | 37.09 s | 116.35 s | 125.63 s | 143.1 s |

**Boundary located:** **bounded latency, never errors.** Zero errors through 128 concurrent workers / 256 requests. Inference is serialized on the single `local_inference_single` scheduler lane; beyond ~48 workers p50 *plateaus* at ~35–37 s (extra workers only deepen the queue) while tail latency creeps up. Fast read-only tiers (kb_search ~0.06 s) keep the median bounded. The only theoretical failure point is a single inference exceeding the client timeout (chat 180 s / route 240 s); the pinned fallback never approached it (max observed 125 s under heaviest load).

**Why this matters for the thesis:** "bounded local substrate" is an honesty claim — the system degrades predictably (queue latency) rather than catastrophically (5xx/hang). Confirmed across two orders of magnitude of load.

**Reproduce:** `python3 clients/stress_test.py 150 48`

---

## P3 — Placement / throughput (GPU vs CPU fallback)

**Measurement (seed=42, temp=0, num_predict=160, same host):** `clients/gpu_vs_cpu_bench.py`.

| model | GPU tps | CPU tps | speedup | GPU prefill tps | CPU prefill tps |
|---|---|---|---|---|---|
| qwen2.5:1.5b | 81.5 | 17.4 | 4.70× | 686 | 65 |
| qwen3:1.7b | 84.2 | 19.9 | 4.22× | 615 | — |
| phi4-mini | 44.2 | 17.3 | 2.55× | — | — |
| qwen2.5-coder:7b | 42.0 | 9.0 | 4.67× | — | — |

**Boundary located:** GPU (MLX/Metal) decode is 2.5–4.7× faster; prefill gap ~10× on small models. CPU-forced (`num_gpu=0`) **never failed** at any tested size — CPU fallback is a viable degrade lane (7B → 9 tps, ~5× slower). Placement confirmed via `ollama ps` (100% CPU vs 100% GPU). Ollarma *observes* placement as an admission signal (`guards.py`) but does not force it; Ollama owns placement under unified memory.

**Reproduce:** `python3 clients/gpu_vs_cpu_bench.py`

---

## P4 — Capacity / co-residency (the count cap binds first)

**Pre-registered** (`experiments/coresidency/PREREG.md`, CONFIRMATORY descriptive measurement). **Data:** `experiments/coresidency/results.jsonl` (29 measured states). **Synthesis:** `experiments/coresidency/MATRIX.md`.

Two stacked limits — **the config cap binds before memory**:

| Limit | Value (this host) | Binds when |
|---|---|---|
| ① Model-count cap `OLLAMA_MAX_LOADED_MODELS` (default) | **3 models resident, total** | ALWAYS — with rescue+embed pinned, only **1 working model slot** remains |
| ② Unified-memory ceiling (all-GPU) | **~23–24 GB VRAM** (max 23.36 GB @ 14% free) | only for the large tier; two ≥24 B models cannot co-reside |
| ③ CPU spill / swap | **never reached** | Ollama **evicts** rather than splitting to CPU — all 29 states 100% GPU; swap never grew |

**Headline:** "How many models can co-reside?" → **3 by default config** (≈1 working model beyond the pinned rescue+embed floor), *not* memory-limited until the large tier. Memory would allow ~4–6 small/mid models — but only by raising `OLLAMA_MAX_LOADED_MODELS`. That env var is the single lever that changes the answer.

**Per-model resident footprint (measured, ctx=4096):** every model 1.5B→30B runs all-GPU alongside the pinned ~2.71 GB floor. Resident VRAM exceeds disk size by the KV-cache + runtime overhead. Practical max observed: `qwen3.6:27b` at 20.65 GB resident (23.4 GB with floor, 14% free). The "12 GB budget" in the legacy CLAUDE.md is stale (it described the retired M1 Pro 16 GB host); this 32 GB host carries ~23–24 GB of weights fully on GPU.

**Hardware limits located (verbatim from MATRIX.md):**
1. Concurrent model count: 3 (config) → ~1 working model under default + pinned floor. **Primary limit.**
2. All-GPU memory ceiling: ~23–24 GB resident VRAM. Above this → eviction, not CPU spill.
3. Two-large-model wall: any pair of ≥24 B models exceeds 32 GB → cannot co-reside.
4. CPU/swap limits: untested-because-unreached — Ollama evicts before spilling.
5. Peer-contention floor: ~2.9 GB GPU permanently committed (rescue+embed pins) + transient peer loads.

**Reproduce:** `python3 experiments/coresidency/coresidency_probe.py`

---

## P5 — Swarm acceptance + EXP-002 calibration

### P5a — Predictive deliberative swarm (Phase 70, T9b acceptance, 2026-05-31)

Pre-registered as `PROMPT-OLLARMA-SWARM-001`; run `420d2ae`. **5/5 named MESI gates PASS:** `api_contract`, `wall_time`, `jsd_detectability` (5/5), `resilience_quarantine` (≤0.4%), `thermal_hygiene`. **Verdict: PARTIAL** — the inert-control strict-cleanliness *sub-criterion* failed: max round-over-round JSD `0.0412` ≥ the `0.02` cleanliness gate, originating in the cold-start round0→round1 transition out of unanimous-neutral. The `0.05` falsification threshold was **NOT** crossed. Per operator: this is **calibration/remediation, not engine rejection.**

### P5b — EXP-002 inert-control calibration (COMPLETE, 2026-06-10) → **FAIL (engine falsification)**

Pre-registered as `PROMPT_002_INERT_CONTROL_CALIBRATION.md`; runner `scripts/swarm_calibration.py` (16 passing unit tests). Tested whether the T9b round-1 wobble **reproduces** across 5 distinct inert prompt forms under identical local-GPU swarm settings, or was a single-prompt artifact.

**Live run:** 2026-06-10T14:01:57Z → 15:28:01Z, qwen2.5-coder:7b, N=50 × M=5, seed=42, temp=0, settings_match_t9b=true, single-writer per PROMPT-002 §No-Overlap Guards, total wall 5044 s, quarantine 0.0. Output: `runs/swarm_calibration_20260610T140157Z/`.

| inert prompt | max JSD | strict-clean (<0.02) | falsified (>0.05) |
|---|---|---|---|
| inert_1_lorem | 0.0412 | ✗ | ✗ |
| inert_2_shuffled_neutral | 0.0203 | ✗ | ✗ |
| inert_3_repeated_sentence | 0.0146 | ✓ | ✗ |
| inert_4_random_tokens | **0.0810** | ✗ | **✓** |
| inert_5_bland_factual | **0.0858** | ✗ | **✓** |

**Verdict: `FAIL` — engine falsification.** 2/5 inert prompts exceeded the 0.05 falsification threshold; the T9b 0.0412 wobble reproduced exactly. All divergence is the **round0→round1 cold-start** out of unanimous-neutral (later rounds ≤0.015) — the swarm manufactures spurious stance differentiation from noise in its first deliberative round.

**Boundary status:** criterion #5 (the sole v5.1 ship gate that was open) is **NOT met and stays BLOCKED**; Phase 70 clean closeout is **stopped**. This is an honest negative result, registered in `experiments/NEGATIVE_RESULTS_REGISTRY.md`. The PI disposition and remediation routing are in `docs/publication/CRITERION5_PI_DECISION.md` (Branch C). Remediation hypothesis: cold-start aggregation; requires a *new* pre-registration + fresh T9c — no silent patch-and-rerun.

> **Meta-point for the publication:** the verification layer's *own* pre-registered falsification gate fired on its *own* swarm engine, and the governance honored it (blocked the ship) instead of relabeling PARTIAL/FAIL as PASS. That is the system working as designed — a deterministic guardrail refusing to promote an unvalidated claim. The honesty of this negative result is itself evidence for the thesis.

**Reproduce:** `.venv/bin/python scripts/swarm_calibration.py --smoke` (offline) · `.venv/bin/python scripts/swarm_calibration.py` (live, operator-gated, single-writer).

---

## Provenance & integrity

- Machine-readable index: [`evidence_index.jsonl`](./evidence_index.jsonl) — one row per evidence artifact with path, type, host, claim ceiling, and reproduce command.
- Claims register: [`CLAIMS_LEDGER.md`](./CLAIMS_LEDGER.md) — every asserted claim classified DIRECT / MEASURED / INFERRED, with the artifact that supports it and any open dependency.
- All five pillars are reproducible offline-or-on-host with the commands listed. The host of record is fixed (`Mac13,1`, 32 GB, Ollama 0.24.0). Re-running on a different host/version will produce different *numbers* but the same *boundary shapes* (count cap before memory; latency before errors; GPU faster than CPU but CPU viable) — those shapes are the falsifiable claims.

## Known gaps (honest carry-forward)

1. **Criterion #5 re-scoped to v5.2 (two negative results).** EXP-002 falsified the Phase 70 predictive swarm on inert controls (confabulates stance from noise); EXP-004's abstention remediation cleared inert (JSD 0.0000) but failed the anti-lobotomy gate (silenced real debate too) and was reverted. As PI/PM, v5.1 ships its five met criteria (#1-4 execution-lane swarm + #6 notebook runtime); the predictive swarm (#5) moves to a v5.2 milestone with a discriminating-abstention remediation hypothesis. Honest closure, never relabeled PASS. See `CRITERION5_PI_DECISION.md`.
2. **Selection is recompute-on-demand** — the model→tier *quality* mapping is a separate harness, not in this bundle. Capacity ≠ quality.
3. **Single host of record** — all numbers are M1 Max 32 GB. The retired M1 Pro 16 GB numbers in legacy docs are explicitly superseded, not re-measured.
4. **Bridge default-routing gap** (from `.planning/HANDOFF.md`) — `append_sidecar_event`/`collect_watch` still default to the global SWE spine in some paths; isolation stress PASSED but the default-routing flip is a follow-up, not part of this bundle.
