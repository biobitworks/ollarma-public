---
name: ollarma-bridge-runtime
description: "Surface Ollarma's bridge role + live blockers + operator commands when a session needs to know what Ollarma is, what's live, and what to run. Use when a user asks 'what is ollarma's role', 'is gemini/grok wired', 'how do I refresh model selection', 'is the gateway live', 'how do I run the Phase 69 proof', or any question that conflates Ollarma with Watchtower/Overwatch/gsigmad."
allowed-tools: Read, Bash, Glob, Grep
---

# Ollarma Bridge Runtime — Operator Reference Skill

Repo-local Claude Code skill. Read-only. **Does not run providers, does not write outside Ollarma, does not promote claims.**

## When to invoke

Invoke when the operator's question is one of:

- "What is Ollarma's role in the stack?"
- "Is Gemini/Grok wired up?"
- "Is the gateway live?"
- "What does `SELECTION_STALE` mean? How do I clear it?"
- "What does `SWAP_DEGRADED` mean?"
- "How do I run the Phase 69 proof?"
- "What's the difference between Ollarma and Watchtower/Overwatch/gsigmad?"
- Any prompt that risks treating Ollarma as a portfolio orchestrator, a governance authority, or a live multi-provider relay.

## Required reading (in order)

1. `docs/MULTI_AGENT_BRIDGE_CONTRACT.md` — bridge role, boundary roles, live blockers, operator commands
2. `docs/PROVIDER_ROADMAP.md` — what's LIVE / SUBSTRATE-LIVE / NOT LIVE — FUTURE / REJECTED
3. `docs/SWARM_PROOF_RUNBOOK.md` — Phase 69 operator-run procedure
4. `.planning/seeds/SEED-ollarma-swarm-broker-role.md` — pre-committed scope ceiling for any future cross-project swarm work
5. `docs/OLLARMA_SUBSTRATE_CONTRACT.md` — HTTP/CLI/Python module contract that sibling repos consume

## What to do (in order)

1. Read the four files above.
2. Identify which of the operator's confusions (if any) is in play. Common ones:
   - Treating Ollarma as governance authority → correct: gsigmad governs science, Overwatch governs portfolio truth
   - Treating Watchtower as a writeback channel → correct: Watchtower observes, never promotes
   - Assuming Gemini/Grok are wired → correct: future adapters; not in this repo
   - Assuming the gateway is on by default → correct: disabled by default; per-project allowlist required
   - Treating the Phase 69 driver as proof itself → correct: driver is INFRASTRUCTURE-READY; operator-run receipt closes Phase 69
3. Surface the relevant live blockers from `docs/MULTI_AGENT_BRIDGE_CONTRACT.md` §4 — `SELECTION_STALE`, `SWAP_DEGRADED`, gateway-disabled, Phase 69 proof-run pending.
4. Hand over exact commands from `docs/MULTI_AGENT_BRIDGE_CONTRACT.md` §6 if the operator needs to act.
5. **Do NOT implement** new provider runtime code. Do NOT add API keys, secrets, or live calls. If the conversation drifts toward implementation, halt and route to the appropriate scoping skill (`/gsd-new-milestone` for a real provider, `/gsd-quick` for a documentation update).

## Operator commands cheat-sheet

```bash
# Refresh model selection — resolves SELECTION_STALE
ollarma run --suites code --trials 3
ollarma report --run-id <fresh_run_id>
ollarma verify <fresh_run_id>

# Phase 69 proof-run smoke (offline, no GPU, no LLM)
.venv/bin/python scripts/swarm_proof_run.py \
    --target /tmp/stub-target --task "smoke" \
    --mode smoke --dry-run \
    --out runs/swarm_proof/smoke_$(date +%s)

# Phase 69 proof-run live (operator action; closes Phase 69)
scripts/swarm_proof_run.py \
    --mode proof \
    --target ./<real-repo> \
    --task "<real task>" \
    --allow-target-writes
```

## One-line summary you can quote back to the operator

*Ollarma owns local runtime receipts. Watchtower observes. Overwatch governs portfolio truth. gsigmad governs science/EXP/KG. Local-first by default; gateway opt-in per-project; Gemini/Grok future, not current.*

## Hard rails

- No cloud LLM calls.
- No API keys, no secrets, no provider runtime code in this skill.
- No edits outside the Ollarma repo.
- No promotion of any claim to the portfolio KG.
- If the operator's question requires action outside the Ollarma boundary, name the right surface (Overwatch for portfolio truth; gsigmad for science; Watchtower for observation) and stop.

## Cross-references

- `docs/MULTI_AGENT_BRIDGE_CONTRACT.md`
- `docs/PROVIDER_ROADMAP.md`
- `docs/SWARM_PROOF_RUNBOOK.md`
- `.planning/seeds/SEED-ollarma-swarm-broker-role.md`
- `docs/OLLARMA_SUBSTRATE_CONTRACT.md`
- `docs/LOCAL_ADOPTION.md`
