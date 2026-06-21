# Lab Notebook

Append-only record of experiment entries.

---

## SIG-20260419T210739Z-claude-291a

**Experiment:** EXP-1.1 — ollarma v4.5 first governed dogfood (local-first restart-safe execution)
**Classification:** EXPLORATORY
**Status:** completed_with_findings
**Phase reference:** `.planning/phases/56-first-governed-dogfood/` (GSD Phase 56)
**Agent:** claude (Opus 4.6 / Claude Code session 92311783)
**Signed at:** 2026-04-19T21:07:39Z

### Hashes

- Prompt artifact (RFC8785/sha256): `d193aced4e93681667ff256fd8408219a68b5a6e57b3a6d257a70fb48b06d26f`
- Handoff artifact (sha256): `87a85f02ab1eb35d2eff283e3dc439c65d9f3075efb5be01a87fdef6308799b2`
- Phase 56 scaffolding commit: `0ca30c3`
- Phase 56 PLAN.md commit: `ff3c87e`

### Findings

1. **Substrate guards fire correctly** under host pressure (SWAP_DEGRADED at 3078MB swap): autopilot blocked script execution rather than silently escalating. Local-first behavior preserved. Zero frontier calls across all five invocation paths tested.
2. **Demo target workload is sound** — `demo/hackathon/demo_project/scripts/seedgraph_handoff_demo.py` produces the deterministic `handoff.json` when run directly. The autopilot block is a guard, not a defect.
3. **HTTP surfaces (`/chat`, `/route`) are fully instrumented** — admission, routing classifier, scribe hooks, explicit escalation receipts all observed. One `/route` call with a stale-KB project correctly produced an `escalation_receipt` with `reason_code=KB_STALE` and `next_action=kb_build_or_frontier` rather than fabricating an answer.
4. **CLI autopilot does not emit scribe entries** — only HTTP surfaces do. Flagged as DEBT-56.2 for v5.0 backlog.
5. **SWAP_DEGRADED guard is global across asset types** in autopilot — blocks even deterministic model-free scripts. Flagged as DEBT-56.1 for v5.0 backlog (per-tier granularity).

### Outcome against H0

Partially retained, but for a **non-defect reason**. End-to-end happy path (autopilot → script → handoff) did not run in-wrapper because the swap guard fired correctly. The substrate is behaving as v4.5 promises; the host state was the degraded condition. No substrate defect found.

### Deferred to later experiments

- DEBT-56.1 — per-tier autopilot guard granularity
- DEBT-56.2 — CLI autopilot scribe parity
- OBS-56.1 — refresh model selection artifact (SELECTION_STALE observed)

### Signature

This entry is signed by content-derivation hash: take the lowercased entry seed

```
20260419T210739Z|EXP-1.1|0ca30c3|d193aced4e93681667ff256fd8408219a68b5a6e57b3a6d257a70fb48b06d26f|87a85f02ab1eb35d2eff283e3dc439c65d9f3075efb5be01a87fdef6308799b2
```

Compute `sha256`; first 4 hex chars → `291a`. The SIG header above encodes this.

---

## SIG-20260420T050408Z-claude-7cf3

**Claim type:** substrate-ship (NOT a measured statistical claim)
**Milestone:** v5.0 Gateway / Frontier / SWE-bench — release candidate `v5.0-rc1`
**Phase reference:** `.planning/phases/57..65/` (10 phases; GSD Phase 65 closeout); `.planning/milestones/v5.0-CLOSEOUT.md`
**Agent:** claude (Opus 4.6 / Claude Code session continuation)
**Signed at:** 2026-04-20T05:04:08Z

### What this signature covers

This SIG entry signs the **shipping of v5.0 substrate** — the code, tests, docs, and planning artifacts. It does NOT sign any measured statistical claim about SWE-bench pass@1 or any provider performance assertion. Those measurements belong to EXP-1.2 (CONFIRMATORY), which remains `status: planned` and will receive its own SIG entry after the operator-executed live run.

### Anchors

- **Gateway core foundations (Phase 57):** commits `c0acb54` → `77f1cc6` → `2fc8354` (3 plans, 107 tests)
- **Substrate legibility micro-batch (Phase 57.1):** `2b8c913` (CLI scribe parity; F-01 BLOCKER closed), `817da5f` (readiness gateway block), `4d8ed0f` (receipts trace CLI), `54a04ab` (substrate consumer contract)
- **Admission + rate caps (Phase 58):** `d3b6843` (allowlist + vk registry), `9b12200` (rate caps + 429 + restart-safe)
- **Provider integrations (mocked core):** `2fdfa6f` Anthropic (Phase 59), `7748cbe` OpenAI + agnostic refactor (Phase 60)
- **SWE-bench substrate (Phases 61+62):** `e651a1d` local-lane harness, `e0d48d1` frontier-lane + leaderboard + chain verify
- **Observability wave + debt pillar:** `e44b3c8` dashboard gateway panel, `e331f13` debt minimum closure + re-track
- **Pre-registered experiment:** EXP-1.2 `.gsigmad/experiments/EXP-1.2.yaml` (CONFIRMATORY; h0 stated; status=planned)

### Verified invariants (I-01..I-06)

- I-01 (local-first default): gateway ships `enabled=false`; no v4.5 path delegates silently to gateway
- I-02 (no silent fallback): 4-layer enforcement proven across 1308 non-live tests
- I-03 (v4.5 paths stable): regression canaries green throughout; `/chat`, `/route`, `/autopilot`, `/workflow` byte-identical semantics
- I-04 (receipt chain is audit primitive): operator-walkable via `ollarma receipts trace`; `verify_leaderboard_chain`; `GatewayReceiptStore.verify_chain`
- I-05 (gateway bounded + audited + opt-in): all three properties enforced at admission
- I-06 (keys never in git/logs/receipts): grep-tested in 58-01, 59-01, 60-01, 63-01; dashboard redacts vk IDs

### What is NOT signed (honestly deferred)

- **Measured pass@1** for local lane or frontier lane on SWE-bench Lite — EXP-1.2 live run pending operator session with:
  - `ANTHROPIC_API_KEY` installed in macOS Keychain (`security add-generic-password -s ollarma-anthropic -a $USER -w`)
  - Healthy swap headroom (currently 6.8GB / 7GB; blocker for local lane)
  - Anthropic cost budget (~$3-$5 for 300 claude-haiku-4-5 runs)
- **Hard Gate 3** of v5.0 (live 300-problem dual-lane run) — substrate-only in this signature
- **Part of Hard Gate 5** (v4.5 retroactive-review DEBT-07..14 backlog) — re-tracked DEFERRED per `.planning/phases/64-debt-pillar/64-DEBT-TRIAGE.md`

### Hard-gate status at signature time

- HG-1 EscalationReceipt + FrontierReceipt: ✅ PASS
- HG-2 Budget gate fails closed: ✅ PASS
- HG-3 SWE-bench full suite both lanes: ⚠ SUBSTRATE-ONLY; live deferred
- HG-4 Zero un-receipted frontier paths: ✅ PASS + VERIFIED
- HG-5 Debt pillar minimum: ◐ PARTIAL (v5.0-layer closed; v4.5 retro-review DEFERRED)
- HG-6 Keys never in git/logs/receipts: ✅ PASS + VERIFIED
- HG-7 Substrata consumer contract: ✅ PASS

### Test baseline at signature

1308 non-live tests passing, 0 regressions, 1 deselected (`test_determinism_live`, environmental/swap). Baseline held across 10 phases + 17 commits.

### Deferred-to-EXP-1.2 contract

When the operator executes the HG-3 live run and produces a leaderboard artifact whose hash chain verifies clean:
1. A separate SIG entry will be appended to this notebook.
2. That entry will reference `EXP-1.2` and carry the measured pass@1 numbers for both lanes.
3. It will set EXP-1.2 `status: completed_with_findings` (or equivalent per the observed result).
4. It will recommend tag promotion `v5.0-rc1` → `v5.0` if all 7 hard gates pass.

### Signature derivation

Entry seed (lowercased):

```
20260420t050408z|v5.0-rc1-substrate-ship|2b8c913|e0d48d1|e331f13|tests=1308
```

Compute `sha256`; first 4 hex chars → `7cf3`. The SIG header above encodes this.
