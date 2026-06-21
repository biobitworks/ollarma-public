# Ollarma HOWTO

Operator-facing index for Ollarma — the bounded local chat, routing,
and deterministic-execution lane.

This file is the operator entry point for routine work. The
authoritative governance documents remain `CANON.md`, `AGENTS.md`,
`README.md`, and the deeper docs/ playbooks referenced below.

## Operator quick links

### Routine operations

- [`docs/LOCAL_ADOPTION.md`](./LOCAL_ADOPTION.md) — how to adopt
  Ollarma locally without bypassing governance.
- [`docs/OPERATOR_RECOVERY_PLAYBOOK.md`](./OPERATOR_RECOVERY_PLAYBOOK.md) —
  recovery flows when Ollarma is degraded.
- [`docs/RECOVERY_PROTOCOL.md`](./RECOVERY_PROTOCOL.md) — broader
  recovery protocol.
- [`docs/SWARM_PROOF_RUNBOOK.md`](./SWARM_PROOF_RUNBOOK.md) — proof-run
  for swarm verification.

### Contracts and boundaries

- [`docs/DETERMINISTIC_EXECUTION_BOUNDARY.md`](./DETERMINISTIC_EXECUTION_BOUNDARY.md)
- [`docs/DETERMINISTIC_EXECUTION_POLICY.json`](./DETERMINISTIC_EXECUTION_POLICY.json)
- [`docs/MULTI_AGENT_BRIDGE_CONTRACT.md`](./MULTI_AGENT_BRIDGE_CONTRACT.md)
- [`docs/OLLARMA_KB_CONTRACT.md`](./OLLARMA_KB_CONTRACT.md)
- [`docs/OLLARMA_SUBSTRATE_CONTRACT.md`](./OLLARMA_SUBSTRATE_CONTRACT.md)

### Dashboards and KB

- [`docs/KB_HOME.md`](./KB_HOME.md) — formal knowledge-base index.
- [`docs/DASHBOARD.md`](./DASHBOARD.md) — general dashboard guide.
- [`docs/OLLARMA_DASHBOARD.md`](./OLLARMA_DASHBOARD.md) — Ollarma-
  specific dashboard.
- [`docs/PROVIDER_ROADMAP.md`](./PROVIDER_ROADMAP.md) — provider /
  model roadmap.

### Replication / Antigence handoff

- [`docs/REPLICATION_AND_ANTIGENCE.md`](./REPLICATION_AND_ANTIGENCE.md)
- [`docs/CROSS_AGENT_REVIEW_LOGGING.md`](./CROSS_AGENT_REVIEW_LOGGING.md)
- [`docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md`](./ANTIGENCE_REALTIME_BACKBONE_PLAN.md)
- [`docs/ANTIGENCE_REALTIME_BACKBONE_REQUIREMENTS.md`](./ANTIGENCE_REALTIME_BACKBONE_REQUIREMENTS.md)

### Prompt-governed work

- [`prompts/`](../prompts/) — canonical prompt artifacts.
- [`PROMPT_EXP_MAP.md`](../PROMPT_EXP_MAP.md) — prompt-to-EXP mapping.
- [`docs/OLLARMA_TEST_PROMPT.md`](./OLLARMA_TEST_PROMPT.md),
  [`docs/OLLARMA_PROJECT_HELPER_PROMPT.md`](./OLLARMA_PROJECT_HELPER_PROMPT.md),
  [`docs/OLLARMA_BACKUP_PUSH_MIRROR_PROMPT.md`](./OLLARMA_BACKUP_PUSH_MIRROR_PROMPT.md).

## Watchtower projection (downstream observer)

Watchtower projects Ollarma as gsigmad-governed
(`gsigmad-governance-bridge`). The bridge status surface
(`/api/ollarma/bridge-status`) remains `degraded=true` as of
`.planning/quick/260511-project-tracking-readiness-fix/`: swap
degraded, gateway disabled, KB 1 ready / 15 stale / 10 blocked. That
operational blocker is tracked separately and is not closed by adding
this HOWTO file.

## Boundaries

- Ollarma remains canonical for local chat, routing, and
  deterministic execution.
- Watchtower is observer-only and never submits Ollarma workflows.
- Do not promote Ollarma to autonomous-watcher status until KB +
  swap + gateway are clean.
- This HOWTO indexes existing docs; it does not change any operator
  contract or policy.
