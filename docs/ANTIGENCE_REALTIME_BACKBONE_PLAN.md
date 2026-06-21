# Antigence Real-Time Backbone Plan

**Status:** planning contract, not an implementation claim.
**Date:** 2026-05-31.
**Scope:** make Ollarma a bounded real-time bridge, chat surface, KG navigator,
and receipt backbone for Antigence safety/security orchestration and
distillation without promoting Ollarma into a portfolio orchestrator or KG
owner.

## Position

Ollarma should be Antigence's local execution and evidence substrate, not its
policy brain. The split is:

- Ollarma receives local operator and sibling-repo requests, routes them through
  local-first models, resolves bounded KB evidence, and writes hash-chained
  runtime receipts.
- Antigence consumes selected Ollarma outputs as safety/security review inputs:
  hallucination, prompt-injection, method, citation, sandbox, contradiction,
  and leakage checks.
- gsigmad/gettingsciencedone remains canonical for science EXP/PROMPT/KG and
  claim promotion.
- Watchtower observes; Overwatch ingests portfolio evidence; neither is
  executed by Ollarma.

The result should be a real-time sidecar loop:

```text
operator / sibling repo
  -> Ollarma /chat, /route, /kb/search, workflow, swarm lane
  -> local model output + citations + route/lane receipts
  -> Antigence review/distillation packet
  -> pass | flag | block | escalate receipt
  -> operator, Watchtower, Overwatch, or gsigmad ingestion path
```

## Current Evidence

These surfaces already exist and should be reused rather than reinvented:

| Need | Existing Ollarma surface | Current posture |
|---|---|---|
| Chat bridge | `POST /chat`, MCP `chat_with_model`, `ollarma chat` | Live, single-turn; SSE wrapper exists for event-stream responses. |
| Project routing | `POST /route`, MCP `route_prompt`, fleet adapters | Live, retrieval-first and fail-closed on missing/stale KB. |
| KG/KB navigation | `kb-build`, `kb-status`, `kb-search`, `/kb/status/{project}`, `/kb/search` | Live read-only KB contract; not canonical KG ownership. |
| Runtime evidence | `.ollarma/` receipts, scribe, gateway receipts, lane receipts | Live append-only receipt model across core surfaces. |
| Antigence check | `GuardrailGate`, antibody registry, tool-intent gate, redaction hooks | Live as optional sidecar/pass-through when Antigence is unavailable. |
| Project antibodies | `AdapterConfig.antibodies`, `GuardrailGate(adapter)` | Live per-project antibody list; no explicit owner/share/import metadata yet. |
| Real-time transport | SSE support on `/chat`, `/route`, `/agents/{name}/run` | Transport shell exists; not yet a token-level or receipt-event stream. |
| Small-model orchestration | v5.1 lane runtime + Phase 70 predictive swarm | Build-complete pieces; Phase 69/70 operator proof gates still pending. |
| Model isolation | Reserved-model policy for `qwen3:1.7b` | In progress in the current worktree; protects Antigence/Sentinel role model from generic Ollarma selection. |
| Antigence antigen heads | Reported Antigence clinical pack: antigen-bank head, five detectors, abstain-by-default LOUD degraded posture | External downstream status; Ollarma does not implement this yet, but RTB-02/03 must preserve antigen-bank provenance, calibration state, and project-pack ownership. |
| Cellico Bio model probes | Reported sequential probes on 32 GB M1 Max: `qwen3.5:2b` is the clean structured cell-type floor; `qwen3.5:9b` is primary escalation rung; `granite4.1:8b` routes well but leaks prose without grammar | External downstream status; RTB-02 must encode model-role suitability, JSON grammar requirements, and warm/evict policy before treating these as runtime defaults. |

## Missing Work

The objective is not satisfied until these gaps are closed:

1. **Real-time bridge contract.** Define event types for chat tokens,
   route evidence, KB hits, lane transitions, Antigence verdicts, and
   distillation summaries. Current SSE is a response wrapper, not a durable
   bridge event stream.
2. **Antigence review packet schema.** Add a stable file/receipt schema that
   Antigence can consume without scraping free text. It must include input
   hashes, output hashes, citations, route receipt refs, model, safety lane,
   and claim ceiling.
3. **Project antibody swarm contract.** Every project needs its own antibody
   profile, and some antibodies should be explicitly reusable by other projects.
   The plan needs ownership, export policy, import policy, version, and verdict
   provenance so shared antibodies do not become silent global authority.
4. **KG navigator contract.** Keep Ollarma read-only: it may navigate adapter KB
   and gsigmad-provided KG pointers, but must not write canonical KG records.
   It needs a navigator response that distinguishes `kb_direct`,
   `kg_pointer`, `orchestrator_handoff`, and `insufficient_evidence`.
5. **Distillation boundary.** Distillation should produce bounded summaries with
   provenance refs and uncertainty. It must not promote claims, rewrite EXP
   records, or overwrite Antigence policy state.
6. **Safety/security loop.** Antigence verdicts need to feed back into Ollarma
   as `pass`, `flag`, `block`, or `escalate` receipts that can be observed by
   Watchtower/Overwatch and used to stop downstream execution.
7. **Proof gates.** Real-time bridge, KG navigator, and Antigence packet flow
   need fixture tests plus one operator-gated localhost proof before they can be
   called live backbone behavior.
8. **Calibration gate.** Antigence's reported clinical thresholds are
   provisional. Ollarma must treat reject/block verdicts from calibrated heads
   as advisory or LOUD degraded until the source pack reports a closed
   calibration gate, with MCC-tuned thresholds over a sufficiently stratified
   labeled set.
9. **Sequential escalation ladder.** The 32GB M1 Max/M1-class host reality
   means big-model rungs are sequential-only. Fan-out belongs to antibody and
   tiny cell-type lanes; larger model escalation must be explicit, receipted,
   and one-at-a-time.
10. **Per-project manifest population.** The adapter `antibodies` field exists,
   but many registered projects still have empty or underspecified manifests.
   RTB-02 must make project pack manifests concrete enough that Cellico,
   Vitaology, Antigence, and future projects can declare their own sensors.
11. **Structured-output enforcement.** Router and verdict lanes that emit JSON
   need JSON-mode or grammar constraints. Model suitability probes are not
   enough when a model leaks markdown or prose around an otherwise good answer.

## Proposed Build Slices

Requirement traceability: `docs/ANTIGENCE_REALTIME_BACKBONE_REQUIREMENTS.md`.

### RTB-01: Event Stream Contract

Add a schema-first event stream for localhost consumers.

Execution plan: `docs/ANTIGENCE_RTB_01_EVENT_STREAM_PLAN.md`.

Deliverables:

- `BridgeEvent` Pydantic schema with `schema_version`, `event_id`, `run_id`,
  `parent_event_id`, `event_type`, `payload_hash`, `created_at`, and redacted
  payload.
- Event types: `chat_started`, `chat_delta`, `chat_done`, `route_started`,
  `kb_hit`, `route_done`, `lane_transition`, `antigence_review_started`,
  `antigence_verdict`, `distillation_done`, `blocked`, `escalated`.
- Append-only JSONL store under `.ollarma/bridge/events.jsonl`.
- `GET /bridge/events` for recent events and `GET /bridge/events/stream` for
  SSE replay-from-last-id.

Acceptance:

- Existing `/chat`, `/route`, `/workflow`, `/autopilot`, `/gateway/submit`
  semantics stay unchanged.
- SSE clients can reconnect with last event id and receive no duplicates.
- Receipts and events link by hash/id, but events never contain secrets.

### RTB-02: Project Antibody Swarm Contract

Make project-specific and reusable antibodies first-class orchestration lanes.

Execution plan: `docs/ANTIGENCE_RTB_02_ANTIBODY_SWARM_PLAN.md`.

Deliverables:

- Extend adapter/runtime metadata with an `antibody_profile` block:
  `owner_project`, `default_antibodies`, `exports`, `imports`, `version`,
  `claim_ceiling`, and `review_modes`.
- Preserve `AdapterConfig.antibodies` as the backward-compatible shorthand for
  project-local defaults; new metadata is additive.
- Add an `AntibodyLaneSelection` schema that records every active lane as one
  of: `core`, `project_owned`, `borrowed_shared`, or `operator_requested`.
- Require imported/shared antibodies to name source project, version/digest,
  allowed use, and whether the source project allows reuse for security,
  science, citation, methodology, infra, or public-communication review.
- Add verdict provenance to every Antigence review packet:
  antibody key, owner project, source digest/version, lane class, input hash,
  output hash, and whether the verdict is advisory, blocking, or escalation-only.
- Represent antigen-bank heads as a first-class lane family. Their source pack
  must record known-bad exemplar bank digest, embedding model, threshold, and
  calibration status.
- Keep the ladder project-agnostic: project packs choose antibodies and tiny
  cell-type lanes; Ollarma chooses local execution posture and escalation
  sequencing.
- Add model-role policy metadata for antibody lanes: recall-only sensor,
  structured cell-type, router, escalation rung, reasoning rung, and ceiling
  rung.
- Record strict-output requirements for each model role, including JSON grammar
  when a model is used as a router or blocking verdict emitter.

Acceptance:

- Every project can define a project-owned antibody profile without making it
  available globally.
- Reusable antibodies are opt-in exports and opt-in imports; no antibody is
  borrowed silently from another project.
- The review swarm can run core + project-owned + borrowed lanes and preserve
  disagreement instead of averaging it away.
- Antigen-bank lanes can fire on known-bad similarity, but cannot emit a trusted
  `block` verdict unless their source pack marks calibration closed.
- Prompt-injection remains a core always-on lane; project-specific antibodies
  can add to it but cannot remove it.
- Tests cover local-only antibodies, shared antibody import, forbidden import,
  version/digest provenance, conflicting lane verdicts, and calibration-open
  antigen-bank verdicts degrading to advisory.
- Tests cover manifest population for projects with previously empty
  `antibodies` fields and refusal to run a JSON-required router without a
  grammar/structured-output constraint.

Proposed adapter shape:

```yaml
antibodies:
  - prompt_injection
  - methodology
antibody_profile:
  owner_project: deltaprot
  version: 1
  default_antibodies:
    - deltaprot_methodology
    - deltaprot_proteomics_claims
  exports:
    - key: deltaprot_proteomics_claims
      digest: sha256:<digest>
      allowed_uses: [science, methodology, citation]
      claim_ceiling: advisory
  imports:
    - key: citation
      source_project: Antigence
      required_digest: sha256:<digest>
      allowed_uses: [citation]
  review_modes:
    chat: [core, project_owned]
    workflow: [core, project_owned, borrowed_shared]
    distillation: [core, project_owned, borrowed_shared]
```

Rules:

- `antibodies` remains the compatibility field consumed by today's
  `GuardrailGate`.
- `antibody_profile.default_antibodies` is the authoritative project-owned set
  once RTB-02 ships.
- `exports` make antibodies available to other projects, but do not activate
  them elsewhere.
- `imports` activate a shared antibody only when the importing project pins the
  source and digest.
- `claim_ceiling` on an antibody can only lower the review output's claim
  ceiling; it can never promote a finding into canonical KG truth.

### RTB-03: Chat-To-Review Loop

Turn generic/project chat into an optional Antigence-reviewed lane.

Execution plan: `docs/ANTIGENCE_RTB_03_REVIEWED_CHAT_PLAN.md`.

Deliverables:

- Request flag: `review_with_antigence=true` on `/chat` and `/route`.
- `AntigenceReviewPacket` schema containing prompt hash, response hash, model,
  citations, route receipt ref, active antibodies, and claim ceiling.
- Review verdict stored as an Ollarma receipt and bridge event.
- Fail-open/pass-through only when the request is explicitly helper-only and no
  execution/writeback follows; fail-closed before workflow/autopilot mutation.

Acceptance:

- Antigence unavailable yields explicit `antigence_unavailable` metadata.
- `block` verdict prevents downstream execution and surfaces recovery guidance.
- `flag` verdict returns response plus warning; operator must decide next step.

### RTB-04: KG Navigator

Expose read-only navigation over KB artifacts and gsigmad-provided KG pointers.

Execution plan: `docs/ANTIGENCE_RTB_04_KG_NAVIGATOR_PLAN.md`.

Initial indexing stance:

- For Ollarma's own semantic file search, approve only `src/`, `tests/`, and
  `docs/` for macfind indexing.
- Use macfind defaults: `confidence_threshold=0.25`, `max_results=10`.
- Do not index `.planning/`, `runs/`, generated result bundles, cache
  directories, or broad `~/projects/active` by default. Those are useful
  evidence stores, but they are noisy retrieval sources and can swamp KG
  navigation with artifact JSON.
- Add sibling projects later only through explicit adapter/KB declarations or
  per-project approved dirs. Broad all-project indexing is not a prerequisite
  for the Antigence backbone.

Deliverables:

- `POST /navigator/query` with project, query, optional KG pointer scope, and
  explicit mode: `kb_only`, `kg_pointer`, or `auto_readonly`.
- `NavigatorResult` schema with answer class, citations, authority level,
  stale status, and next action.
- CLI/MCP mirrors: `ollarma navigator query`, MCP `navigator_query`.
- No write access to gsigmad, Antigence, Watchtower, or Overwatch state.

Acceptance:

- Missing/stale KB fails closed when stale behavior is `block`.
- KG pointers are surfaced as references, not rewritten as Ollarma truth.
- Tests prove no route can create or mutate KG records.

### RTB-05: Distillation Receipts

Make orchestration/distillation auditable enough for Antigence safety layers.

Execution plan: `docs/ANTIGENCE_RTB_05_DISTILLATION_PLAN.md`.

Deliverables:

- `DistillationReceipt` with source receipt refs, source hashes, reducer model,
  summary hash, uncertainty, omissions, safety flags, and claim ceiling.
- Deterministic distillation mode for bounded summaries from chat, route, KB,
  and lane outputs.
- Optional Antigence post-distillation review packet.

Acceptance:

- Distillation is reproducible from stored refs and hashes.
- Summary text never becomes a canonical KG or EXP update by itself.
- Negative/safety findings are preserved rather than averaged away.

### RTB-06: Antigence Backbone Proof

Run a localhost-only proof bundle over fixture data.

Execution plan: `docs/ANTIGENCE_RTB_06_PROOF_BUNDLE_PLAN.md`.

Deliverables:

- Fixture project with KB data, route query, reviewed chat, navigator query,
  distillation, and blocked Antigence verdict case.
- Proof bundle under `runs/bridge/<UTC>/` containing events, receipts, packets,
  verification output, and operator-readable summary.
- Runbook for local operator proof with Antigence available and unavailable.

Acceptance:

- Tests cover happy path, stale KB refusal, Antigence unavailable, flag, block,
  reconnect/replay, and secret redaction.
- Operator proof demonstrates end-to-end event/receipt reconstruction.
- Documentation states exactly which parts are live and which remain gated.

## Non-Goals

- No cloud or frontier fallback without the existing gateway receipt chain.
- No writes into Antigence, gsigmad, Watchtower, Overwatch, or sibling repos.
- No claim promotion, EXP mutation, or canonical KG write by Ollarma.
- No cross-project priority scheduler or portfolio orchestrator.
- No silent use of Antigence/Sentinel-reserved models on generic Ollarma lanes.
- No silent global antibody pool. Project antibodies are local unless explicitly
  exported and explicitly imported with provenance.
- No fan-out of large local models. Big-model rungs are sequential escalation
  targets, not parallel swarm cells on this host class.
- No trusted reject/block from a provisional antibody pack. Open calibration
  gates stay visible and reduce verdict authority.

## Verification Matrix

| Requirement | Evidence needed before claiming done |
|---|---|
| Real-time bridge | Bridge event schemas, append-only store tests, SSE replay tests, localhost proof events. |
| Chat | `/chat` and `/route` compatibility tests plus optional review packet tests. |
| Project antibodies | Adapter metadata tests, lane-selection tests, shared-antibody provenance tests, forbidden-import tests. |
| KG navigator | KB/KG pointer tests showing read-only behavior, stale fail-closed, citations. |
| Antigence backbone | Review packet/verdict receipts, flag/block behavior, Antigence unavailable posture. |
| Orchestration/distillation | DistillationReceipt tests, source hash replay, no claim-promotion guard. |
| Safety/security | Secret-redaction tests, reserved-model tests, blocked verdict prevents mutation. |
| Calibration/hardware | Pack calibration-state tests, advisory downgrade tests, sequential escalation receipts, embed-model unavailable posture. |
| Model suitability | Sequential benchmark receipts, warm/evict policy tests, JSON grammar tests for router/verdict lanes. |

## Next Action

Open a scoped implementation phase or quick task for RTB-01, then build the
RTB-02 swarm skeleton. Within RTB-02, the highest-leverage source change is the
per-project antibody manifest plus confidence-gated escalation router:
`qwen2.5:1.5b` as recall sensor, `qwen3.5:2b`/`qwen3.5:4b` as structured
cell-type floor, `qwen3.5:9b` as primary escalation, 14B reasoning models
load-on-demand, and 24B-30B ceiling models only in sanctioned sequential
benchmarks. If choosing inside Antigence between the swarm orchestration layer
and calibration harness, build the orchestration skeleton first but keep
calibration as a hard authority gate: no provisional head may emit a trusted
reject/block until its pack reports closed calibration.
