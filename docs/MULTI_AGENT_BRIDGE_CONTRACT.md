# Ollarma Multi-Agent Bridge Contract

**Audience:** Operators and sibling-repo authors who need a single page that says what Ollarma's role is in the wider Biobitworks AI stack — and what it explicitly is not.

**Scope:** Ollarma's place between human-driven CLIs (ChatGPT Codex, Claude Code), local execution (Ollama models), and future provider adapters (Gemini, Grok, etc.).

**Stability pledge:** This document inherits the v5.0 substrate stability pledge (`schema_version=1` for all referenced receipt classes through the v5.0 milestone). v5.1+ may extend additively.

**Status:** This contract documents intent and current capability. It is **NOT** a claim that all listed surfaces are live (see §4 — Live blockers). It does **NOT** authorize provider runtime code, secrets, or live calls beyond what already ships.

---

## §1 — Ollarma is a bounded bridge, not an orchestrator

```
   ┌────────────────────┐    ┌──────────────────┐    ┌────────────────────────┐
   │  ChatGPT Codex     │    │   Claude Code    │    │  Future provider       │
   │  (operator UI)     │    │  (operator UI)   │    │  adapters (Gemini,     │
   │                    │    │                  │    │  Grok, …) — NOT live   │
   └─────────┬──────────┘    └────────┬─────────┘    └────────────┬───────────┘
             │                        │                            │
             │   prompts + bounded    │   prompts + bounded        │   future
             ▼   workflow handoffs    ▼   workflow handoffs        ▼   adapter spec
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │                                                                             │
   │                            O L L A R M A                                    │
   │              (bounded local-execution substrate + bridge)                   │
   │                                                                             │
   │   • routes prompts to local Ollama OR records an explicit escalation       │
   │   • emits hash-chained receipts for every admission/route/gateway/lane     │
   │   • OWNS local runtime receipts; does NOT own portfolio truth              │
   │   • OWNS task lease/claim semantics; does NOT govern admission policy      │
   │                                                                             │
   └─────────┬───────────────────────────────────────────┬────────────────────┬─┘
             │                                           │                    │
             ▼                                           ▼                    ▼
   ┌──────────────────────┐              ┌──────────────────────┐  ┌────────────────┐
   │  Local Ollama (MLX)  │              │  v5.0 Gateway        │  │  Cross-repo    │
   │  qwen2.5-coder:7b    │              │  (DISABLED by        │  │  read-only     │
   │  qwen2.5:1.5b        │              │  default; opt-in     │  │  triage via    │
   │  deepseek-r1:14b ... │              │  per-project)        │  │  fleet adapter │
   └──────────────────────┘              └──────────────────────┘  └────────────────┘
```

Ollarma is the **bounded bridge** that:

- **Receives** explicit, receipted requests from chat-driven operators (ChatGPT Codex, Claude Code) or from sibling repos via the substrate contract (`/route`, `/workflow`, `/gateway/submit`, etc.).
- **Decides locally first** — every request resolves on local Ollama unless the operator has explicitly opted into the gateway lane with a populated `EscalationReceipt`.
- **Emits receipts** — every admission, route, gateway call, lane decision, and recovery event lands as a `schema_version=1` Pydantic receipt under `.ollarma/`.
- **Stops at its own boundary** — Ollarma does not promote receipts to the portfolio KG, does not author claims, and does not govern admission policy.

---

## §2 — Boundary roles in the stack

The portfolio has four distinct authority surfaces. Ollarma is one of them and respects the other three.

| Authority surface | Owned by | Ollarma's stance |
|---|---|---|
| **Local runtime receipts** (`.ollarma/gateway/*.jsonl`, `.ollarma/incidents/*.json`, `.ollarma/session-log.jsonl`, `.ollarma/bridge/events.jsonl`, `.ollarma/recovery_block_receipts.jsonl`) | **Ollarma** (this repo) | Authors them. Append-only, hash-chained. Machine-local forensic evidence. |
| **Portfolio truth** (cross-project inventory, ownership graph, governance surface) | **Overwatch** | Reads Ollarma's receipts via the mirror table in `docs/OLLARMA_BACKUP_PUSH_MIRROR_PROMPT.md`. Ollarma does NOT write to Overwatch state. |
| **Operator observation cockpit** (Vitaology cockpit, fleet status, swarm-lane status) | **Watchtower** | Reads Ollarma's receipts read-only. Watchtower OBSERVES; it does not promote. Ollarma does NOT write to Watchtower state. |
| **Science / experiments / KG** (PROMPT-### pre-registration, EXP-### lab notebook, claim closure chain, citations) | **gsigmad / gettingsciencedone** | Defines contracts Ollarma implements against (e.g., signed lab-notebook entries for substrate ships). Ollarma READS KG pointers it is given; it NEVER writes to the KG. |

**One-line summary:** *Ollarma owns local runtime receipts. Watchtower observes. Overwatch governs portfolio truth. gsigmad governs science/EXP/KG.*

---

## §3 — Local-first and receipt-first invariants (v5.0; carried into v5.1)

These bind every bridge surface Ollarma exposes:

- **I-01 Local-first default** — every prompt resolves on local Ollama unless the operator has explicitly populated and submitted an `EscalationReceipt`. There is no implicit cloud fallback.
- **I-02 No silent escalation** — every frontier call requires an `EscalationReceipt` in and emits a `FrontierReceipt` out. Failure-to-escalate produces a structured rejection receipt with a `reason_code`, never a silent retry.
- **I-03 v4.5/v5.0 production paths stable** — `/chat`, `/route`, `/autopilot`, `/workflow`, `/gateway/submit` keep byte-identical semantics across v5.x. Bridge additions (Phase 66-69 swarm-lane runtime; Phase 70 predictive swarm) are NEW surfaces, not modifications of existing ones.
- **I-04 Receipt chain is the audit primitive** — `escalation_receipt → gateway_admission → provider_call → frontier_receipt` is reconstructable end-to-end from `.ollarma/` state. v5.1's lane runtime extends this with `lane_transitions.jsonl` + `checkpoint.json` per `runs/<rid>/`.
- **I-05 Bounded + audited + opt-in** — gateway is opt-in per-project; lane runtime is opt-in per-call; predictive swarm is opt-in per-call. Miss any of the three properties → doesn't ship.
- **I-06 API keys never in git, logs, or receipts** — keychain or env only. Provider adapters that violate this are rejected.
- **I-07 Degraded local sidecar remains useful** — stale validated selection or host pressure may degrade `/health`, but `/chat`, `/route` readiness, and helper sidecar status should expose a pressure-aware local fallback when an installed chat-capable model is available. This does not claim a fresh benchmark winner and does not weaken workflow, gateway, or proof-run gates.

---

## §4 — Live blockers as of 2026-05-06

The bridge surface is partially live. Operators must read this before treating any of these as ready.

| Blocker | What it means | Resolution path |
|---|---|---|
| **`SELECTION_STALE`** | Model selection guide on disk is older than the freshness budget OR was produced against a different model set than is currently resident on the host. The bridge will refuse to claim a fresh "winner" until refresh. Low-risk chat/route sidecar surfaces may report `fallback_ready` with a pressure-aware local model when one is installed. | Run `ollarma run --suites code --trials 3` then `ollarma report --run-id <fresh_run_id>` then `ollarma verify <fresh_run_id>`. |
| **`SWAP_DEGRADED`** | Host swap usage exceeds the v4.5 / v5.1 thresholds (Phase 68: `SWAP_DEGRADED_PCT_THRESHOLD=50` for routing degradation; `SWAP_BLOCKED_PCT_THRESHOLD=80` for routing refusal). Routing ladder will downgrade to rescue model OR refuse altogether. | Operator must reduce host memory pressure (close apps; `sudo purge`) before high-confidence routing. The condition is observable via `ollarma serve` → `/startup/readiness` → `gateway` block. |
| **Gateway disabled by default** | `/gateway/submit` returns `status=disabled` until the operator explicitly sets `gateway.enabled=true` AND adds the project to the per-project allowlist. This is correct behavior per I-05; flagging here so operators know the gateway is NOT a default route. | Document opt-in steps in `docs/OLLARMA_SUBSTRATE_CONTRACT.md` §3 (when written). For now: set config + allowlist + provider keychain entries. |
| **Phase 69 proof-run pending** | `scripts/swarm_proof_run.py` ships INFRASTRUCTURE-READY only. The v5.1 milestone-acceptance criterion #4 ("at least one real target repo completes a bounded local swarm run") is NOT yet satisfied. Phase 69 stays `[ ]` in `.planning/ROADMAP.md` until an operator-run receipt is committed. | See `docs/SWARM_PROOF_RUNBOOK.md` for the operator-run procedure. |

---

## §5 — Bridge surfaces by current status

| Surface | Status | Reference |
|---|---|---|
| Local Ollama via `/chat`, `/route`, MCP `chat_with_model` | **LIVE** (v3.x → v5.0) | README §Service Surfaces; `docs/OLLARMA_SUBSTRATE_CONTRACT.md` §1 |
| Bounded execution via `/autopilot`, `/workflow` | **LIVE** (v3.x → v4.5) | `docs/DETERMINISTIC_EXECUTION_BOUNDARY.md` |
| Recovery engine via `/recovery/*` + `recover` CLI | **LIVE** (v4.3) | `docs/RECOVERY_PROTOCOL.md` |
| Frontier gateway via `/gateway/submit` | **SUBSTRATE-LIVE, OPERATOR-GATED** (v5.0-rc1; HG-3 live run pending) | `docs/OLLARMA_SUBSTRATE_CONTRACT.md` §1; provider keys never live in git |
| Anthropic adapter | **SUBSTRATE-LIVE; live smoke deferred** (v5.0 Phase 59) | `.planning/milestones/v5.0-ROADMAP.md` |
| OpenAI adapter | **SUBSTRATE-LIVE; live smoke deferred** (v5.0 Phase 60) | `.planning/milestones/v5.0-ROADMAP.md` |
| Predictive deliberative swarm (Phase 70) | **BUILD-COMPLETE; T9 acceptance run pending operator** | `.planning/SESSION_PROMPT_T9_RESUME.md`; `prompts/PROMPT_OLLARMA_SWARM_001_LOCAL_GPU.md` |
| Execution-lane swarm runtime (Phases 66-68) | **BUILD-COMPLETE 2026-05-06** | `src/ollarma/swarm/lane/`; `tests/swarm/lane/` 116/116 passing |
| Phase 69 swarm proof run | **INFRASTRUCTURE-READY; operator-run pending** | `scripts/swarm_proof_run.py`; `docs/SWARM_PROOF_RUNBOOK.md` |
| **Gemini adapter** | **NOT LIVE — future provider adapter** | `docs/PROVIDER_ROADMAP.md` |
| **Grok adapter** | **NOT LIVE — future provider adapter** | `docs/PROVIDER_ROADMAP.md` |
| Cross-chat broker / cross-model relay | **SEED-ONLY** (no milestone scoped) | `.planning/seeds/SEED-ollarma-swarm-broker-role.md` |
| Antigence real-time backbone | **PLANNED - not implemented** | `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md` |

---

## §6 — Operator commands (current; safe to run)

These are the commands a sibling-repo author or operator can run today against this version of Ollarma. None require API keys or live provider calls.

```bash
# 1. Refresh model selection (resolves SELECTION_STALE)
ollarma run --suites code --trials 3
ollarma report --run-id <fresh_run_id>
ollarma verify <fresh_run_id>

# 2. Check live host posture (gateway block + swap)
curl -s http://127.0.0.1:8484/startup/readiness | jq .gateway
curl -s http://127.0.0.1:8484/startup/readiness | jq .swap

# 3. Trace a receipt chain
ollarma receipts trace <escalation_receipt_id> --repo-root .

# 4. Run the Phase 69 proof-run driver in smoke mode (offline, no GPU)
.venv/bin/python scripts/swarm_proof_run.py \
    --target /tmp/stub-target \
    --task "smoke" \
    --mode smoke --dry-run \
    --out runs/swarm_proof/smoke_$(date +%s)

# 5. Run the actual proof-run (operator action; produces the receipt
#    that closes Phase 69 — see docs/SWARM_PROOF_RUNBOOK.md)
scripts/swarm_proof_run.py \
    --mode proof \
    --target ./<real-repo> \
    --task "<real task>" \
    --allow-target-writes
```

---

## §7 — Cross-references

- `docs/OLLARMA_SUBSTRATE_CONTRACT.md` — the consumer contract (HTTP / CLI / Python imports)
- `docs/PROVIDER_ROADMAP.md` — current vs. future provider adapters (Gemini, Grok, etc.)
- `docs/SWARM_PROOF_RUNBOOK.md` — Phase 69 operator-run procedure
- `docs/LOCAL_ADOPTION.md` — install + adapter discovery for this workstation
- `docs/DETERMINISTIC_EXECUTION_BOUNDARY.md` — execution-boundary contract
- `docs/CROSS_AGENT_REVIEW_LOGGING.md` — current local evidence streams, transcript limits, and redacted review packet contract
- `docs/OLLARMA_BACKUP_PUSH_MIRROR_PROMPT.md` — Overwatch ingestion mirror table
- `docs/RECOVERY_PROTOCOL.md` — recovery engine contract
- `.planning/seeds/SEED-ollarma-swarm-broker-role.md` — pre-committed scope ceiling for any future cross-project swarm work
- `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md` - concrete plan for real-time bridge events, reviewed chat, read-only KG navigation, and distillation receipts for Antigence
- `docs/ANTIGENCE_REALTIME_BACKBONE_REQUIREMENTS.md` - auditable requirement-to-slice traceability for the Antigence backbone plan
- `.planning/ROADMAP.md` — current milestone (v5.1 active; v5.0-rc1 carry-forward)

*Last updated: 2026-05-31.*
