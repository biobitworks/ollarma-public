# Antigence Real-Time Backbone Requirements

**Parent:** `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md`
**Status:** requirements traceability for planning and future implementation.

This file turns the user objective into auditable requirements. A future
completion claim must cite evidence for every row.

## Requirements

| ID | Requirement | Planned slice | Evidence required |
|---|---|---|---|
| RTB-REQ-01 | Ollarma exposes a durable real-time bridge event spine for localhost consumers. | RTB-01 | `BridgeEvent` schema, append-only store, `/bridge/events`, `/bridge/events/stream`, replay tests. |
| RTB-REQ-02 | Existing `/chat`, `/route`, `/workflow`, `/autopilot`, and `/gateway/submit` semantics remain backward-compatible. | RTB-01, RTB-03 | Existing endpoint tests plus new event-hook tests showing response payloads/statuses unchanged. |
| RTB-REQ-03 | Bridge events never store secrets or provider raw credential payloads. | RTB-01 | Redaction unit tests and HTTP tests inspecting stored payloads. |
| RTB-REQ-04 | Every project can define project-owned Antigence antibodies. | RTB-02 | `AdapterConfig.antibody_profile` tests and profile resolver tests. |
| RTB-REQ-05 | Shared antibodies are opt-in export plus opt-in import with source, digest/version, allowed use, and claim ceiling. | RTB-02 | Valid import tests, missing export tests, digest mismatch tests, allowed-use refusal tests. |
| RTB-REQ-06 | Antibody review behaves as a swarm of lanes, not a hidden global gate. | RTB-02, RTB-03 | `AntibodyLaneSelection` tests, conflict-preservation tests, review packet provenance. |
| RTB-REQ-07 | Generic and project chat can optionally request Antigence review without making Antigence mandatory for helper-only use. | RTB-03 | `/chat` and `/route` review flag tests, Antigence unavailable metadata, pass/flag/block behavior. |
| RTB-REQ-08 | Antigence block verdicts prevent downstream mutating execution. | RTB-03, RTB-05 | Workflow/autopilot guard tests proving blocked verdict stops execution before mutation. |
| RTB-REQ-09 | Ollarma exposes a read-only KG navigator over adapter KB artifacts and gsigmad-provided KG pointers. | RTB-04 | `/navigator/query`, CLI, MCP tests; stale KB refusal; no-write tests. |
| RTB-REQ-10 | KG navigation does not write canonical KG, EXP, Antigence, Watchtower, Overwatch, or sibling-repo state. | RTB-04 | Filesystem mutation guard tests and docs showing read-only boundary. |
| RTB-REQ-11 | Initial semantic file-search scope is focused and avoids noisy artifact directories. | RTB-04 | Verified macfind config/runbook showing Ollarma `src/`, `tests/`, `docs/` only; `.planning/` and `runs/` excluded. |
| RTB-REQ-12 | Distillation is auditable, source-hash-backed, and preserves uncertainty/omissions/safety flags. | RTB-05 | `DistillationReceipt` tests and replay from source refs. |
| RTB-REQ-13 | Distillation cannot promote claims or mutate science governance records. | RTB-05 | Claim-ceiling tests and no-write guard tests. |
| RTB-REQ-14 | Safety/security findings are preserved rather than averaged away. | RTB-02, RTB-05 | Conflicting lane verdict tests and distillation tests preserving negative/safety findings. |
| RTB-REQ-15 | Antigence backbone proof demonstrates reviewed chat, KG navigation, distillation, and blocked verdict behavior on localhost fixtures. | RTB-06 | `runs/bridge/<UTC>/` proof bundle with events, receipts, packets, verification output, and summary. |
| RTB-REQ-16 | Ollarma remains bounded local runtime, not portfolio orchestrator or KG owner. | All | Contract docs, no-write tests, and absence of code paths writing external governance state. |
| RTB-REQ-17 | Antigen-bank heads preserve exemplar-bank digest, embedding model, threshold, and calibration status. | RTB-02, RTB-03 | Lane-selection tests and review packet tests proving antigen-bank provenance is present. |
| RTB-REQ-18 | Open or unknown calibration gates cannot produce authoritative reject/block verdicts. | RTB-02, RTB-03, RTB-06 | Advisory downgrade tests, reviewed-chat authority tests, proof bundle showing calibration-open downgrade. |
| RTB-REQ-19 | Big-model escalation rungs run sequentially on the reference host; fan-out is limited to antibodies and tiny cell-type lanes. | RTB-02, RTB-06 | Sequential escalation receipt tests and proof bundle evidence; no parallel big-model fan-out path. |
| RTB-REQ-20 | Embedding-model unavailability for antigen heads fails LOUD degraded rather than silent pass. | RTB-02, RTB-03 | `nomic-embed-text` unavailable/thrashing tests showing degraded authority and explicit metadata. |
| RTB-REQ-21 | Project antibody packs are the ownership unit, while the routing/escalation ladder stays project-agnostic. | RTB-02 | Pack registry tests proving project-owned defaults, explicit exports/imports, and ladder-independent lane selection. |
| RTB-REQ-22 | Router and blocking-verdict lanes require structured-output enforcement before their verdicts can be trusted. | RTB-02, RTB-03, RTB-06 | JSON-mode/grammar tests; granite-style prose leak fixture fails closed without grammar. |
| RTB-REQ-23 | Model suitability is recorded as policy evidence and hardened by sequential benchmark receipts before becoming a runtime default. | RTB-02, RTB-06 | Sequential `run_benchmark`/proof receipts, load/evict order, confidence metrics, and explicit benchmark-only ceiling rungs. |
| RTB-REQ-24 | Empty per-project antibody manifests are surfaced as pack gaps, not treated as complete safety coverage. | RTB-02, RTB-06 | Adapter scan tests and proof bundle showing projects with empty `antibodies` fields are reported for population. |
| RTB-REQ-25 | Ollarma owns a stable `nomic-embed-text` pin and bounded `/embed` surface for downstream calibration and antigen-bank harnesses. | Embed-pin quick, RTB-02, RTB-03 | Embedding service tests proving `/api/embeddings` uses configured keep-alive/pin behavior, `/embed` schema tests, LOUD degraded unavailable tests, and macfind delegation to the shared embed path. |

## Completion Rule

The planning objective can be considered fully planned when:

- every requirement above has at least one planned implementation slice;
- every slice has a testable acceptance gate;
- the non-goal boundaries are explicitly stated in the plan; and
- remaining implementation work is clearly separated from current capability.

The implementation objective is not complete until the evidence column is
backed by passing tests, run artifacts, or inspected runtime behavior.
