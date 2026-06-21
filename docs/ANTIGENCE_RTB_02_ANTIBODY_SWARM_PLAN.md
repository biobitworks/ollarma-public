# RTB-02 Project Antibody Swarm Execution Plan

**Parent:** `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md`
**Status:** implementation plan, not shipped behavior.
**Scope:** make project-owned and explicitly shared Antigence antibodies usable
as bounded swarm review lanes with provenance.

## Why This Comes After RTB-01

Antibody reviews need a durable event spine before they become orchestration
signals. RTB-01 provides the replayable bridge events. RTB-02 defines how a
project chooses review lanes, how other projects may reuse specific antibodies,
and how every verdict records provenance.

The current repo already has:

- `AdapterConfig.antibodies: list[str]`
- `GuardrailGate(adapter)` unioning project antibodies with `prompt_injection`
- `ANTIBODY_REGISTRY` for known Antigence antibody systems

RTB-02 must extend that model without breaking existing adapters. It must also
align with the reported downstream Antigence direction: project-specific
antibody packs are first-class, some packs may explicitly export reusable
antibodies, antigen-bank heads are a valid lane type, and the size ladder
remains project-agnostic.

## Files To Add

| File | Purpose |
|---|---|
| `src/ollarma/antibody_profile.py` | Pydantic schemas and resolver for antibody profiles, exports, imports, lane selections. |
| `src/ollarma/antibody_model_policy.py` | Pydantic schemas and resolver for antibody model roles, warm/evict posture, strict-output requirements, and escalation rungs. |
| `tests/test_antibody_profile.py` | Schema, resolver, provenance, import/export, conflict tests. |
| `tests/test_antibody_model_policy.py` | Model-role policy, sequential escalation, JSON grammar, and warm/evict tests. |
| `docs/examples/antibody-profile.yaml` | Small adapter-profile example for operators. |

## Files To Modify

| File | Required changes |
|---|---|
| `src/ollarma/fleet.py` | Add optional typed `antibody_profile` field to `AdapterConfig`; keep `antibodies` compatibility. |
| `src/ollarma/guardrail.py` | Accept resolved lane selections or keep existing behavior when none is supplied. |
| `src/ollarma/service.py` | Use resolved antibody lanes when constructing project guardrails for review-enabled calls. |
| `tests/test_fleet.py` | Verify YAML adapters preserve typed `antibody_profile`. |
| `tests/test_guardrail.py` | Verify core `prompt_injection` cannot be removed and lane metadata is exposed. |
| `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md` | Flip RTB-02 from planned to implemented only after tests pass. |

## Schema

```python
class AntibodyExport(BaseModel):
    key: str
    digest: str
    allowed_uses: tuple[str, ...]
    claim_ceiling: Literal["advisory", "blocking", "escalation_only"]

class AntibodyImport(BaseModel):
    key: str
    source_project: str
    required_digest: str
    allowed_uses: tuple[str, ...]

class AntigenBankConfig(BaseModel):
    key: str
    digest: str
    embedding_model: str
    threshold: float | None = None
    calibration_status: Literal["open", "closed", "unknown"] = "unknown"

class AntibodyLaneSelection(BaseModel):
    antibody_key: str
    lane_class: Literal["core", "project_owned", "borrowed_shared", "operator_requested"]
    head_type: Literal["rule_floor", "antigen_bank", "tiny_cell", "model_judge"] = "rule_floor"
    owner_project: str
    source_project: str | None = None
    source_digest: str | None = None
    embedding_model: str | None = None
    threshold: float | None = None
    calibration_status: Literal["open", "closed", "unknown"] = "unknown"
    allowed_uses: tuple[str, ...] = ()
    claim_ceiling: str = "advisory"

class AntibodyModelPolicy(BaseModel):
    role: Literal[
        "recall_sensor",
        "structured_cell_type",
        "router",
        "escalation_rung",
        "reasoning_rung",
        "ceiling_rung",
    ]
    model: str
    residency: Literal["pinned", "warm_if_room", "load_on_demand", "benchmark_only"]
    strict_output: Literal["none", "json_mode", "json_grammar"]
    max_parallel_group: Literal["tiny_cell_fanout", "sequential_only"] = "sequential_only"
    confidence_floor: float | None = None

class AntibodyProfile(BaseModel):
    owner_project: str
    version: int = 1
    default_antibodies: tuple[str, ...] = ()
    antigen_banks: tuple[AntigenBankConfig, ...] = ()
    exports: tuple[AntibodyExport, ...] = ()
    imports: tuple[AntibodyImport, ...] = ()
    review_modes: dict[str, tuple[str, ...]] = {}
    model_policy: tuple[AntibodyModelPolicy, ...] = ()
```

## Resolution Rules

1. `prompt_injection` is always selected as `core`.
2. `AdapterConfig.antibodies` remains compatibility input and maps to
   `project_owned` lanes unless the new `antibody_profile` is present.
3. `antibody_profile.default_antibodies` becomes the project-owned default once
   present.
4. `exports` only advertise reusable antibodies. They do not activate anything
   in other projects.
5. `imports` activate a shared antibody only when the importing project pins
   source project and digest.
6. An import is invalid if the source profile has no matching export, digest, or
   allowed use.
7. Operator-requested lanes must still resolve through either local defaults or
   valid imports; free-form unknown antibody keys are rejected.
8. Conflicting lane verdicts are preserved for RTB-03/RTB-05 synthesis; do not
   collapse them to a single average score in RTB-02.
9. Antigen-bank heads must pin their exemplar-bank digest, embedding model, and
   calibration status. `nomic-embed-text` unavailability or bridge thrash emits
   LOUD degraded/advisory posture rather than a silent pass.
10. Tiny cell-type lanes are the preferred fan-out unit on the reference host.
   Larger local models are escalation rungs and must run sequentially with
   explicit receipts.
11. A lane whose `calibration_status` is `open` or `unknown` cannot produce an
   authoritative `block` or `reject`; RTB-03 must downgrade it to advisory or
   escalation-only metadata.
12. Router lanes and verdict lanes that require structured output must declare
    `json_mode` or `json_grammar`. A model that leaks prose/markdown cannot be
    used as a blocking router until the strict-output constraint is active.
13. The initial measured ladder is a policy seed, not a universal benchmark:
    `qwen2.5:1.5b` is recall-only sensor; `qwen3.5:2b` and `qwen3.5:4b` are
    structured cell-type floor; `qwen3.5:9b` is primary escalation rung;
    `deepseek-r1:14b` and `phi4-reasoning:14b` are load-on-demand reasoning
    rungs; 24B-30B models are benchmark-only ceiling rungs.
14. Per-project manifests must be populated explicitly. Empty `antibodies: []`
    remains valid compatibility input, but it is not acceptable evidence that a
    project has a complete safety pack.

## Verdict Provenance

Every review verdict produced after RTB-02 must be able to report:

- target project,
- antibody key,
- lane class,
- owner project,
- source project,
- source digest/version,
- allowed use,
- claim ceiling,
- input hash,
- output hash,
- verdict status: `pass`, `flag`, `block`, or `escalate`.
- head type,
- embedding model for antigen-bank heads,
- calibration status,
- authority status: `trusted`, `advisory`, `degraded`, or `escalation_only`.
- model role,
- model residency policy,
- strict-output mode,
- escalation rung and confidence floor when applicable.

RTB-02 does not have to implement full `AntigenceReviewPacket`; that lands in
RTB-03. It must provide the lane/provenance data RTB-03 will embed.

## Example Adapter Block

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
  antigen_banks:
    - key: deltaprot_bad_claim_patterns
      digest: sha256:example-bank
      embedding_model: nomic-embed-text
      calibration_status: open
  exports:
    - key: deltaprot_proteomics_claims
      digest: sha256:example
      allowed_uses: [science, methodology, citation]
      claim_ceiling: advisory
  imports:
    - key: citation
      source_project: Antigence
      required_digest: sha256:antigence-citation-example
      allowed_uses: [citation]
  review_modes:
    chat: [core, project_owned]
    workflow: [core, project_owned, borrowed_shared]
    distillation: [core, project_owned, borrowed_shared]
  model_policy:
    - role: recall_sensor
      model: qwen2.5:1.5b
      residency: pinned
      strict_output: none
      max_parallel_group: tiny_cell_fanout
    - role: structured_cell_type
      model: qwen3.5:2b
      residency: pinned
      strict_output: json_mode
      max_parallel_group: tiny_cell_fanout
    - role: router
      model: granite4.1:8b
      residency: warm_if_room
      strict_output: json_grammar
      max_parallel_group: sequential_only
    - role: escalation_rung
      model: qwen3.5:9b
      residency: warm_if_room
      strict_output: json_mode
      max_parallel_group: sequential_only
    - role: reasoning_rung
      model: phi4-reasoning:14b
      residency: load_on_demand
      strict_output: json_mode
      max_parallel_group: sequential_only
    - role: ceiling_rung
      model: qwen3.6:27b
      residency: benchmark_only
      strict_output: json_mode
      max_parallel_group: sequential_only
```

## Cellico Bio Probe Constraints

The reported Cellico Bio probes are treated as planning evidence, not as a
complete benchmark. They set the initial policy shape:

| Model | Observed role | Policy consequence |
|---|---|---|
| `qwen2.5:1.5b` | Sensed claim overreach but broke schema | Recall-only sensor; escalate for structured verdict. |
| `qwen3.5:2b` | Clean JSON for claim overreach and valid router JSON, but overshot tier | Cheapest structured cell-type floor; router needs calibration. |
| `qwen3.5:4b` | Cleaner structured reasons | Stronger cell-type lane when memory allows. |
| `phi4-mini` | Correct claim-overreach verdicts | Alternate structured cell-type. |
| `granite4.1:8b` | Best tier calibration, leaked prose/markdown | Router candidate only with JSON grammar. |
| `qwen3.5:9b` | Clean science triage, calibrated confidence | Primary escalation rung. |
| `deepseek-r1:14b` / `phi4-reasoning:14b` | Reasoning rung, not first-pass fan-out | Load on demand, evict after. |
| 24B-30B models | Untested by design on 32 GB host | Benchmark-only ceiling, one at a time. |

The proof path must use sequential load/test/evict behavior for anything above
the tiny cell-type floor. No "one big model per subagent" fan-out is allowed on
the reference host.

## Tests

Unit tests:

- `AntibodyProfile` accepts empty profiles.
- `AdapterConfig` preserves typed `antibody_profile` from YAML.
- flat `antibodies` list maps to `project_owned` lanes.
- `prompt_injection` is always present exactly once.
- valid import resolves to `borrowed_shared` with source/digest metadata.
- import fails when source export is missing.
- import fails when digest mismatches.
- import fails when requested use is outside allowed uses.
- unknown operator-requested antibody is rejected.
- conflicting lane verdicts can be represented without lossy aggregation.
- antigen-bank lane selection records exemplar-bank digest and embedding model.
- open calibration status downgrades `block`/`reject` authority to advisory.
- embedding unavailable posture is LOUD degraded, not silent pass.
- larger model escalation rungs are represented as sequential receipts, not
  parallel lane fan-out.
- router/verdict lanes requiring JSON fail closed when no JSON-mode or grammar
  constraint is configured.
- empty project antibody manifests are detected and reported as pack gaps.

Integration-facing tests:

- `GuardrailGate(adapter)` retains old behavior when no profile exists.
- profile-backed lane selection produces the same active key set plus metadata.
- review mode `chat` excludes `borrowed_shared` lanes when configured that way.
- review mode `workflow` can include borrowed lanes.
- model policy resolver chooses tiny cell fan-out first, then sequential
  escalation rungs.

## Acceptance Gate

RTB-02 is complete only when:

- adapter schema change is backward-compatible with existing adapters;
- project-owned and shared antibody lane selection is deterministic;
- invalid shared-antibody imports fail closed;
- provenance metadata is available for every selected lane;
- antigen-bank head provenance and calibration state are available for every
  selected antigen lane;
- open calibration cannot produce an authoritative block/reject;
- no global antibody pool exists;
- tests prove prompt-injection remains core and always-on.
- tests prove strict-output requirements are enforced before a router/verdict
  lane can block;
- tests prove the warm/evict policy never schedules big-model fan-out.

## Deferred

- Running multiple antibody lanes against live model output.
- Persisting full review packets.
- Feeding verdicts back into `/chat`, `/route`, workflow, or distillation.
- Antigence dashboard/inbox roundtrip.
- MCC threshold tuning for project packs; RTB-02 records calibration state but
  does not close it.
