---
exp_id: EXP-1.1
phase_ref: 56-first-governed-dogfood
classification: EXPLORATORY
status: completed_with_findings
run_at: 2026-04-19T21:02:20Z
---

# EXP-1.1 Results — ollarma v4.5 first governed dogfood

## Hypothesis recap

- **H0 (null):** v4.5 substrate does NOT deliver restart-safe local-first governed execution on `demo/hackathon/demo_project` — guards fail to fire OR frontier lapse occurs OR handoff artifact absent.
- **H1 (exploratory):** substrate delivers the loop end-to-end — admission engaged, routing stays local, scribe logs, autopilot report + handoff produced, zero silent frontier lapse.

## Observed outcome

**H0 partially retained due to autopilot guard gating — not due to substrate defect.**

The v4.5 substrate's guards fired correctly and honestly under the observed host condition (3078 MB swap used — 6× the SWAP_DEGRADED threshold). Autopilot refused to execute the demo script under swap pressure (`exit_code=2`, `escalated=false`). There was no frontier lapse and no silent failure.

The demo script itself — run deterministically outside the autopilot wrapper — produced the expected `handoff.json` (sha256 `87a85f02ab1eb35d2eff283e3dc439c65d9f3075efb5be01a87fdef6308799b2`), confirming the target workload is sound.

The HTTP-mediated paths (`/chat`, `/route`) exercised scribe + admission + routing classifier + explicit escalation receipts. Zero silent frontier calls. Local rescue model (`qwen3:1.7b`) answered helper-lane calls.

## Receipt inventory

| Path | Surface | Lane | Model | Outcome |
|---|---|---|---|---|
| CLI autopilot | `autopilot` | code | qwen3:1.7b (target) | blocked on SWAP_DEGRADED, not escalated |
| HTTP /route ollarma-demo | `route_prompt` | n/a | — | blocked (fleet-miss; fleet is global-scoped) |
| HTTP /route Overwatch (status) | `route_prompt` | kb_direct | — | answered from KB metadata |
| HTTP /route Overwatch (summary) | `route_prompt` | frontier_or_human | — | explicit escalation receipt (KB_STALE) |
| HTTP /chat helper | `chat` | helper | qwen3:1.7b | answered locally |

Zero receipts show a frontier rung. Zero silent escalations.

## Artifacts

- `EXP-1.1.yaml` — pre-registration (this directory)
- `.planning/phases/56-first-governed-dogfood/RESEARCH.md`
- `.planning/phases/56-first-governed-dogfood/PLAN.md`
- `.planning/phases/56-first-governed-dogfood/VERIFICATION.md`
- `.planning/phases/56-first-governed-dogfood/artifacts/autopilot-run-report.json`
- `.planning/phases/56-first-governed-dogfood/artifacts/autopilot-discovery-report.json`
- `.planning/phases/56-first-governed-dogfood/artifacts/preflight-readiness.json`
- `.planning/phases/56-first-governed-dogfood/artifacts/postrun-readiness.json`
- `.planning/phases/56-first-governed-dogfood/artifacts/scribe-excerpt.jsonl`
- `.planning/phases/56-first-governed-dogfood/artifacts/http-path-receipts.txt`
- `.planning/phases/56-first-governed-dogfood/artifacts/handoff.json`

## Claims

No statistical claims. EXPLORATORY classification; no power analysis; no p-value test. This experiment probes the substrate's *guard behavior* on a first real workload, not a measurable effect.

## Deferred (to address in later experiments)

- **DEBT-56.1** — autopilot SWAP_DEGRADED guard: per-tier granularity (model-free scripts should bypass)
- **DEBT-56.2** — CLI autopilot scribe parity with HTTP autopilot
- **OBS-56.1** — refresh model selection artifact (`ollarma run --suites code --trials 3`)

## Conclusion

The substrate's **guards are sharper than its host state**. The dogfood found no substrate defect; it found a real deployment condition (persistent swap pressure on a 16GB M1 Pro running many services) and demonstrated the guards handle it gracefully — block over escalate, explicit receipt over silent call.

Phase 56 closes with this finding on record. Signed entry to follow in LAB_NOTEBOOK.md.
