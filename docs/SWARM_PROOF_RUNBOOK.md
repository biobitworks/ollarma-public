# Swarm Proof Runbook (Phase 69)

**Audience:** Operator running the Phase 69 swarm-lane proof to satisfy v5.1 milestone-acceptance criterion #4 (*"at least one real target repo completes a bounded local swarm run"*).

**Hard stance:** This runbook documents how to produce the receipt that closes Phase 69. The driver itself ships INFRASTRUCTURE-READY only; running it is operator action. Until an operator-run receipt against a real target repo is committed, **Phase 69 stays `[ ]` in `.planning/ROADMAP.md`** and `progress.phase_69_complete=false` in STATE.md.

---

## Pre-flight checklist

All five must be true before you press go.

- [ ] **Model selection is fresh.** `SELECTION_STALE` is not set. If unsure, run the refresh sequence in §2 below.
- [ ] **Swap is healthy.** `live_swap_percent_provider()` reports `< 50%`. If `>= 50%`, expect routing degradation (qwen2.5:1.5b rescue model). If `>= 80%`, the lane will refuse with `ROUTING_BLOCKED_ESCALATE`. Reduce host load (`sudo purge`; close apps) before proceeding.
- [ ] **Ollama daemon is up** and the chosen model is resident. `ollama ps` returns the model. If not: `ollama pull qwen2.5-coder:7b qwen2.5:1.5b`.
- [ ] **Target repo path is real and read/writable.** The driver writes ONLY to `--out` by default; with `--mode proof --allow-target-writes` it MAY write into the target repo. Make sure the target is the one you actually want to mutate.
- [ ] **You have a real task description.** Don't pass a stub. The proof bundle's value depends on the task being something a sibling-repo author would actually delegate.

---

## Step 1 — Smoke first (offline, no LLM)

Always run smoke before live. This validates the driver against a stub target and confirms the bundle layout matches expectations.

```bash
cd <repo>

.venv/bin/python scripts/swarm_proof_run.py \
    --target /tmp/stub-target \
    --task "smoke validation" \
    --mode smoke --dry-run \
    --out runs/swarm_proof/smoke_$(date +%Y%m%dT%H%M%SZ)
```

Expected exit code: `0`.

Expected bundle contents under the `--out` dir:

```
manifest.json                              # invocation + git HEAD
README.md                                  # bundle docs
runs.sqlite                                # LaneStore index
runs/<run_id>/checkpoint.json              # last_completed_role: "synthesizer"
runs/<run_id>/lane_transitions.jsonl       # 4 hash-chained lines
runs/<run_id>/lane_outputs/<lane_id>.json  # 4 typed outputs (one per role)
```

If smoke fails, fix the local environment before doing anything live. Do not skip.

---

## Step 2 — Refresh model selection (resolves `SELECTION_STALE`)

Required if any of the following:
- The reference benchmark on disk is older than the freshness budget.
- You've changed the resident model set (`ollama pull` of new model, deletion of old).
- You're running this on a fresh workstation.

```bash
cd <repo>

# Re-run code suite, 3 trials per model, against current resident set
ollarma run --suites code --trials 3

# Render a human-readable report for the run
ollarma report --run-id <fresh_run_id>

# Verify the receipt chain
ollarma verify <fresh_run_id>
```

`<fresh_run_id>` comes from `ollarma run`'s stdout.

Expected exit codes: all `0`. Verify pass = receipt chain is intact end-to-end.

---

## Step 3 — Confirm host posture

```bash
# Live readiness with gateway block
curl -s http://127.0.0.1:8484/startup/readiness | jq .gateway

# Swap percent (Phase 68 stdlib provider)
.venv/bin/python -c "from ollarma.swarm.lane import live_swap_percent_provider; print(f'{live_swap_percent_provider():.2f}%')"
```

Expected: gateway block has `enabled: false` (correct default) OR `enabled: true` if you intend a frontier escalation. Swap < 50%.

If swap is `>= 50%`, **stop and reduce memory pressure first.** Do not proceed with a real proof-run while degraded — the resulting bundle will be valid but operationally noisy (rescue-only outputs everywhere).

---

## Step 4 — The proof run

This is the operator-controlled run that closes Phase 69. It writes to a real target repo, so the safety gate requires both `--mode proof` AND `--allow-target-writes` explicitly.

```bash
cd <repo>

scripts/swarm_proof_run.py \
    --mode proof \
    --target ./<real-repo> \
    --task "<real task>" \
    --allow-target-writes
```

Replace:
- `<real-repo>` — relative or absolute path to a sibling repo you control. The driver passes this through to the swarm-lane orchestrator's metadata; the on-role-invoke callback is responsible for whether/how to mutate it.
- `<real task>` — a concrete, narrow task description. Examples: *"add a `--strict` CLI flag to the validate subcommand and a unit test for it"*; *"draft a docstring for `swarm.lane.runtime.run_swarm_lane` referencing PROMPT-OLLARMA-SWARM-001 v0.3"*.

**Wall budget:** ~1-2h depending on model + task complexity. Sequential lane execution (planner → executor → reviewer → synthesizer); single-GPU contract.

**Expected exit codes:**

| Code | Meaning |
|---|---|
| `0` | SwarmRun status=completed; bundle is the proof artifact |
| `1` | SwarmRun status=blocked (lease, swap, or routing) — read `swarm_run.json` for `reason_code` |
| `2` | Safety gate refused — you forgot `--allow-target-writes` or used `--mode proof` without it |
| `3` | SwarmRun status=failed (lane exception or token budget exceeded) |

---

## Step 5 — Inspect the bundle

```bash
LATEST=$(ls -dt runs/swarm_proof/swarm_acceptance_* runs/swarm_proof/[0-9]* | head -1)
echo "Latest bundle: $LATEST"

# Quick verdict
cat "$LATEST/manifest.json" | jq .args
cat "$LATEST/runs/"*/swarm_run.json | jq '{status, reason_code, run_id}'

# Receipt chain integrity
.venv/bin/python -c "
from pathlib import Path
from ollarma.swarm.lane import LaneStore
store = LaneStore(Path('$LATEST'))
import json
run_dir = next((Path('$LATEST') / 'runs').iterdir())
print('chain verifies:', store.verify_chain(run_dir))
"

# Read the synthesizer output (last lane in the chain)
cat "$LATEST/runs/"*/lane_outputs/*.json | jq -r 'select(.role == "synthesizer") | .final_summary'
```

---

## Step 6 — Commit + close Phase 69

The proof-run alone does NOT update planning state. You commit the bundle and flip the planning bit explicitly.

```bash
cd <repo>

# Commit the bundle (it lives under runs/swarm_proof/<UTC>/)
git add runs/swarm_proof/<UTC>/
git commit -m "proof(69): operator-run receipt — <target> / <one-line-task>

SwarmRun: <run_id>
Status: completed
Receipt chain verifies; bundle includes manifest + transitions + checkpoint
+ lane_outputs + README.

Closes v5.1 milestone-acceptance criterion #4.
"

# Update STATE.md: progress.phase_69_complete: true
# (Manual edit; the gsd-sdk state.advance-plan handler doesn't parse this format yet.)

# Update ROADMAP.md: flip [ ] -> [x] on Phase 69
# (Manual edit; mirror the Phase 66/67/68 SUMMARY pattern in 69-SUMMARY.md.)
```

---

## When to halt + investigate

| Trigger | What to do |
|---|---|
| Swap > 80% mid-run, run blocks with `ROUTING_BLOCKED_ESCALATE` | Don't retry blindly. Reduce host load. Re-run only after swap drops AND you've documented why the prior attempt blocked (it's a real signal, not a flake). |
| Quarantine rate > 5% in any single lane | The model is producing structurally invalid outputs. Inspect `quarantine.jsonl`. Likely cause: prompt template mismatch with the resident model. Don't paper over with retries. |
| Receipt chain `verify_chain()` returns False | Bundle is corrupt. Do NOT commit. Investigate per `docs/RECOVERY_PROTOCOL.md`. |
| Lease expires mid-run | Phase 67 will surface this as `LEASE_EXPIRED`. Don't manually re-acquire. Re-run from scratch with a fresh `run_id`. |

---

## What this runbook does NOT do

- It does not authorize provider runtime code changes.
- It does not authorize new API keys, secrets, or live cloud calls.
- It does not promote any output to the portfolio KG (gsigmad's job).
- It does not modify Watchtower or Overwatch state.
- It does not bypass any of the v5.0 hard invariants (I-01..I-06).

---

## Cross-references

- `scripts/swarm_proof_run.py` — the driver itself
- `tests/swarm/lane/test_proof_run_driver.py` — 3 subprocess smoke tests for the driver
- `.planning/phases/69-proof-run-and-verification-bundle/69-CONTEXT.md` — phase rationale + PI direction (verbatim)
- `.planning/phases/69-proof-run-and-verification-bundle/69-01-PLAN.md` — implementation contract
- `docs/MULTI_AGENT_BRIDGE_CONTRACT.md` — Ollarma's bridge role + boundaries
- `docs/PROVIDER_ROADMAP.md` — what's live, what's substrate, what's future
- `docs/RECOVERY_PROTOCOL.md` — recovery protocol for corrupt bundles
- `.planning/ROADMAP.md` §Phase 69 — current status (INFRASTRUCTURE-READY)

*Last updated: 2026-05-06.*
