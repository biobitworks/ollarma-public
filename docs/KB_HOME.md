# Ollarma — Knowledge Base Home

This file is the operator-facing index into Ollarma's existing planning, governance,
and contract documents. It is a receipt, not a new source of truth: every link below
points at a file already in this repo. Edit the source files, not this index.

> **Boundary.** Ollarma is a bounded local-execution substrate: routing, deterministic
> execution, and receipt-chain audit. It does not own canonical truth for any
> downstream project; consumer repos own their own KBs (see
> [`OLLARMA_KB_CONTRACT.md`](OLLARMA_KB_CONTRACT.md)).

## Project anchors

| Topic | File |
|------|------|
| Project README | [`../README.md`](../README.md) |
| Agent guide | [`../AGENTS.md`](../AGENTS.md) |
| Autonomous start | [`../AUTONOMOUS_START.md`](../AUTONOMOUS_START.md) |
| Canonical contract | [`../CANON.md`](../CANON.md) |
| Live state | [`../.planning/STATE.md`](../.planning/STATE.md) |
| Roadmap | [`../.planning/ROADMAP.md`](../.planning/ROADMAP.md) |
| Lab notebook | [`../LAB_NOTEBOOK.md`](../LAB_NOTEBOOK.md) |

## Substrate contracts (canonical)

| Contract | File | Purpose |
|---------|------|---------|
| Substrate consumption surface | [`OLLARMA_SUBSTRATE_CONTRACT.md`](OLLARMA_SUBSTRATE_CONTRACT.md) | HTTP / CLI / module / receipts schema |
| KB contract | [`OLLARMA_KB_CONTRACT.md`](OLLARMA_KB_CONTRACT.md) | Adapter-declared KB metadata, fail-closed semantics |
| Deterministic execution boundary | [`DETERMINISTIC_EXECUTION_BOUNDARY.md`](DETERMINISTIC_EXECUTION_BOUNDARY.md) | What the autopilot will/won't run |
| Deterministic execution policy (machine-readable) | [`DETERMINISTIC_EXECUTION_POLICY.json`](DETERMINISTIC_EXECUTION_POLICY.json) | Asset-class allowlist |
| Persistence playbook | [`OLLARMA_PERSISTENCE_PLAYBOOK.md`](OLLARMA_PERSISTENCE_PLAYBOOK.md) | launchd + startup readiness + GPU residency |
| Local adoption guide | [`LOCAL_ADOPTION.md`](LOCAL_ADOPTION.md) | How sibling projects consume ollarma |
| Operator recovery playbook | [`OPERATOR_RECOVERY_PLAYBOOK.md`](OPERATOR_RECOVERY_PLAYBOOK.md) | What to do when the substrate is in a bad state |
| Recovery protocol | [`RECOVERY_PROTOCOL.md`](RECOVERY_PROTOCOL.md) | Replay / receipt-chain audit |
| Replication & Antigence integration | [`REPLICATION_AND_ANTIGENCE.md`](REPLICATION_AND_ANTIGENCE.md) | Antigence sidecar handshake |
| Cross-agent review logging | [`CROSS_AGENT_REVIEW_LOGGING.md`](CROSS_AGENT_REVIEW_LOGGING.md) | Local evidence streams, transcript limits, review packet contract |
| Antigence real-time backbone plan | [`ANTIGENCE_REALTIME_BACKBONE_PLAN.md`](ANTIGENCE_REALTIME_BACKBONE_PLAN.md) | Planned bridge events, reviewed chat, KG navigation, distillation receipts |
| Antigence real-time backbone requirements | [`ANTIGENCE_REALTIME_BACKBONE_REQUIREMENTS.md`](ANTIGENCE_REALTIME_BACKBONE_REQUIREMENTS.md) | Requirement-to-slice traceability for RTB planning |

## Operator references

| Topic | File |
|------|------|
| Dashboard runtime (operator surface) | [`OLLARMA_DASHBOARD.md`](OLLARMA_DASHBOARD.md) and [`DASHBOARD.md`](DASHBOARD.md) |
| Hackathon backup demo | [`HACKATHON_BACKUP_DEMO.md`](HACKATHON_BACKUP_DEMO.md) |
| Backup push / mirror prompt | [`OLLARMA_BACKUP_PUSH_MIRROR_PROMPT.md`](OLLARMA_BACKUP_PUSH_MIRROR_PROMPT.md) |
| Project helper prompt template | [`OLLARMA_PROJECT_HELPER_PROMPT.md`](OLLARMA_PROJECT_HELPER_PROMPT.md) |
| Test prompt template | [`OLLARMA_TEST_PROMPT.md`](OLLARMA_TEST_PROMPT.md) |

## Examples

Live usage examples live under [`examples/`](examples/).

## Experiments and runs

| Resource | Path |
|---------|------|
| Experiments register | [`../experiments/`](../experiments/) (negative-results registry, etc.) |
| Phase plans | [`../.planning/phases/`](../.planning/phases/) |
| Run artifacts | [`../runs/`](../runs/) |
| Result summaries | [`../results/`](../results/) |
| PROMPT-EXP map | [`../PROMPT_EXP_MAP.md`](../PROMPT_EXP_MAP.md) |

## What this index is NOT

- It is not a substitute for `OLLARMA_SUBSTRATE_CONTRACT.md`. Consumer projects must
  read the contract before calling ollarma.
- It does not promote ollarma into being a canonical KB owner for any other project.
  Consumers register their own KBs through the adapter declaration.
- It does not change ollarma's deterministic execution boundary. Anything not on the
  asset-class allowlist remains rejected by the autopilot.
