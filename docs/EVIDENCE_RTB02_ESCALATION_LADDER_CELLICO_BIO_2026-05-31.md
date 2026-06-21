# Evidence — Antibody Escalation Ladder (cross-lane handoff from cellico-bio)

**Date:** 2026-05-31
**Source lane:** Claude Code, working in `repos/cellico-bio`
**Feeds:** `docs/ANTIGENCE_RTB_02_ANTIBODY_SWARM_PLAN.md` (the planned `src/ollarma/antibody_model_policy.py`: "antibody model roles, warm/evict posture, strict-output requirements, and escalation rungs"), `docs/ANTIGENCE_RTB_05_DISTILLATION_PLAN.md`, `models.yml` re-tier (2026-05-31).
**Status:** measured evidence, advisory. Not a runtime change. No EXP/PROMPT allocated.

## What this is

RTB-02 needs an evidence basis for the size ladder, warm/evict posture, and strict-output requirements. I ran live antibody-role probes through the ollarma MCP bridge against the freshly-pulled roster and measured the cell-type floor. These numbers should ground the `antibody_model_policy.py` defaults.

## Host reality (confirms `models.yml` header)

Verified live: **Apple M1 Max, 32 GB unified**. At probe time ~15 GB was already resident (qwen2.5:1.5b pinned + qwen2.5-coder:7b + phi4-mini) and swap was ~2.4 GB; bridge `state: drift`.

Refinement to the `models.yml` note "the 24–31B tier now fits": it *loads*, but only by evicting the resident cell-type swarm and pushing into swap. **Recommendation: never keep a 24–31B model warm and never load two large models concurrently. Treat them as escalation-only — load → resolve → evict.** This is why naive "one large model per subagent" fan-out must be forbidden; model-suitability tests must be sequential.

## Measured cell-type floor (antibody roles)

Probes: **ClaimOverreach** antibody (2 captions, ground truth BLOCK / PASS) and **swarm routing** antibody (escalate-or-accept on a low-confidence verdict). `temperature=0`.

| Model | Size | ClaimOverreach | Routing | Finding |
|---|---|---|---|---|
| qwen2.5:1.5b | 1.5B | verdict right (`BLOCK`) but **schema broken** — invented `COMPUTATIONAL`, wrong array shape, no `reason` | — | **Recall-only sensor.** Good antigen alarm; cannot emit a clean verdict → must escalate for structure. Keep as pinned rescue/sensor only. |
| **qwen3.5:2b** | 2.7B | **clean JSON `[BLOCK, PASS]`**, sound reasons | valid JSON, `ESCALATE` but over-shot to `heavy` | **Cell-type floor** — smallest model that emits a clean structured verdict. Crude tier calibration. |
| qwen3.5:4b | 3.4B | clean, better reasons | (not probed) | Solid floor with better reasoning. |
| phi4-mini | 3.8B | correct `[BLOCK, PASS]`, valid JSON | — | Good cell-type alternative. |
| granite4.1:8b | 5.3B | (not probed) | **best tier calibration** (`ESCALATE → reasoning`, well-reasoned) but **leaked prose + ```json fence** | Strong router, but **strict-JSON requires a grammar/format constraint** — do NOT rely on raw output. |
| qwen3.5:9b | 6.6B | science-triage: correct, calibrated confidence, clean | — | Primary / escalation rung 1. |

## Direct inputs for `antibody_model_policy.py`

1. **Default size ladder (per antibody lane):** `tiny=qwen3.5:2b` (floor) → `mid=qwen3.5:9b` (or `qwen3.5:9b-mlx` for decode speed) → `reasoning=deepseek-r1:14b`/`phi4-reasoning:14b` → `heavy=one of {qwen3.6:27b, mistral-small3.2:24b, gemma4:26b, qwen3-coder:30b}` load-on-demand. `qwen2.5:1.5b` is a **sensor/rescue rung below the floor**, not a verdict producer.
2. **warm/evict posture:** keep `{qwen2.5:1.5b(pinned), qwen3.5:2b, qwen3.5:4b}` warm (~9 GB); mid warm-if-room; reasoning + heavy strictly load-on-demand with mandatory evict-after. Encode a hard rule: **max one model ≥14B resident at a time**.
3. **strict-output requirement:** any antibody whose lane includes a tool-caller (granite family) MUST set a JSON grammar / `format=json`. Empirically granite4.1:8b leaks prose without it; qwen3.5:2b/4b do not. Add a `test_antibody_model_policy.py` case asserting grammar is enforced for tool-caller lanes.
4. **escalation trigger:** escalate when the floor returns (a) invalid schema, or (b) confidence below the lane threshold. Note the 2b over-escalates to `heavy` — cap escalation to **one rung per step** so the cascade isn't short-circuited to the most expensive model.

## How to harden (next ollarma run)

Run the existing harness sequentially (it manages load/evict ordering):
`run_benchmark(models=[qwen3.5:2b,qwen3.5:4b,qwen3.5:9b,granite4.1:8b,deepseek-r1:14b], suites=[science,code,swarm], trials=3)` then `get_report`. The 24–31B tier should be benchmarked one-at-a-time in a sanctioned run, never concurrently.

— cellico-bio lane
