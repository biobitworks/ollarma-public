"""schemas.py -- Typed records for the execution-lane swarm runtime (Phase 66).

All models are pydantic v2, ``frozen=True``, ``schema_version: int = 1``.

Roles
-----
``Role`` is a fixed ``Literal`` covering the v5.1 lane chain:
``planner -> executor -> reviewer -> synthesizer``. The chain is sequential
within a single SwarmRun (single-GPU contract). ``ROLE_CHAIN`` is the
canonical tuple used by the orchestrator (Phase 66 plan 02) when stepping
between lanes.

Outputs
-------
Each role has its own typed output extending ``LaneOutputBase``. The base
carries identity (``run_id``, ``lane_id``, ``role``) plus housekeeping
(``schema_version``, ``created_at``). Role-specific outputs are kept small
and free-text so the synthesizer downstream can quote them verbatim into a
short summary without parsing nested structures.

Receipts
--------
Every lane handoff emits a ``LaneTransition`` receipt that the ``LaneStore``
hash-chains via ``ollarma.evidence.canonical_hash``. ``transition_hash`` is
computed over the canonical (sorted-keys) JSON of the receipt minus the
``transition_hash`` field itself, plus the ``parent_hash`` -- the same
pattern as ``GatewayReceiptStore`` (Phase 57).

Run records
-----------
``SwarmRun`` is the top-level run record (one per launch). ``LaneRun`` is the
per-lane execution record (four per SwarmRun in v5.1 -- one per role).
``LaneRun.metadata: dict[str, str]`` is the gsd v2.3 control-plane contract
slot; left empty in v5.1 until the contract freeze is verified.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Role enumeration
# ---------------------------------------------------------------------------

Role = Literal["planner", "executor", "reviewer", "synthesizer"]
"""Fixed v5.1 lane roles. Pluggable role registry is YAGNI per 66-CONTEXT.md."""

ROLE_CHAIN: tuple[Role, ...] = ("planner", "executor", "reviewer", "synthesizer")
"""Canonical sequential chain. Plan 66-02's orchestrator iterates this tuple."""


def _utc_now() -> datetime:
    """Tz-aware UTC ``datetime``.

    Pydantic v2 serializes tz-aware ``datetime`` to ISO-8601 with explicit
    offset (e.g., ``2026-05-06T19:27:18.840000+00:00``). The hash chain is
    insensitive to representation since ``canonical_hash`` runs on the
    ``model_dump(mode="json")`` output (string form), so any tz-aware output
    that round-trips back to the same ``datetime`` is fine.
    """
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Lane output base + role-specific outputs
# ---------------------------------------------------------------------------

LaneOutcome = Literal["preferred", "degraded", "rescue_only", "blocked_escalate"]
"""Phase 68 routing outcome.

* ``preferred`` -- ladder's preferred model was chosen (healthy host).
* ``degraded`` -- ``ladder.alternates[0]`` was chosen (swap >= DEGRADED threshold).
* ``rescue_only`` -- ``ladder.rescue`` was chosen (degraded with no alternates).
* ``blocked_escalate`` -- system too degraded to honestly route; refusal.

Default ``"preferred"`` keeps Phase 66/67-era serialized records loadable.
"""


class LaneRoleLadder(BaseModel):
    """Per-role model ladder.

    ``preferred`` is the default model; ``alternates`` are fallbacks under
    degraded host state (chosen in order); ``rescue`` is the always-available
    small model used when even alternates aren't fitting. The Phase 68
    ``select_lane_model`` policy (plan 68-02) consumes this in conjunction
    with a live swap-percent provider.
    """

    model_config = ConfigDict(frozen=True)

    preferred: str
    alternates: list[str] = Field(default_factory=list)
    rescue: str
    schema_version: int = 1


class LaneOutputBase(BaseModel):
    """Common fields for every lane's typed output."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    lane_id: UUID
    role: Role
    schema_version: int = 1
    created_at: datetime = Field(default_factory=_utc_now)
    outcome: LaneOutcome = "preferred"
    """Phase 68 routing outcome for the model that produced this output.

    Defaults to ``"preferred"`` so Phase 66/67-era serialized records (which
    pre-date the ``outcome`` field) still validate without error.
    """


class PlannerOutput(LaneOutputBase):
    """Planner's output: ordered free-text steps + rationale.

    No enforced bound on ``plan_steps`` length (the orchestrator may apply
    one); ``rationale`` is bounded at 2000 chars to keep the JSON small
    enough to round-trip through the next lane's prompt at ``num_ctx=1024``.
    """

    role: Literal["planner"] = "planner"
    plan_steps: list[str]
    rationale: str = Field(max_length=2000)


class ExecutorOutput(LaneOutputBase):
    """Executor's output: actions taken + artifacts written + success flag."""

    role: Literal["executor"] = "executor"
    actions_taken: list[str]
    artifacts_written: list[str]
    success: bool
    error_message: str | None = None


class ReviewerOutput(LaneOutputBase):
    """Reviewer's output: findings list + severity + summary."""

    role: Literal["reviewer"] = "reviewer"
    findings: list[str]
    severity: Literal["pass", "warn", "block"]
    summary: str = Field(max_length=2000)


class SynthesizerOutput(LaneOutputBase):
    """Synthesizer's output: final summary + decisions + next actions."""

    role: Literal["synthesizer"] = "synthesizer"
    final_summary: str = Field(max_length=2000)
    decisions: list[str]
    next_actions: list[str]


# ---------------------------------------------------------------------------
# Lane transition receipt (hash-chained)
# ---------------------------------------------------------------------------

class LaneTransition(BaseModel):
    """Receipt emitted on every lane handoff.

    ``parent_hash`` chains back to the prior transition's ``transition_hash``
    (or ``None`` on the first receipt of a run). ``output_content_hash`` is
    the canonical hash of the LaneOutputBase model dump; storing it here
    means the receipt chain stays small while still binding tamper-evidently
    to the lane's full output.

    ``transition_hash`` is populated by ``LaneStore.append_transition`` --
    constructed transitions carry an empty string until they enter the chain.
    """

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    lane_id: UUID
    from_role: Role | None  # None on the very first transition (planner-in)
    to_role: Role | None  # None on the last transition (synthesizer-out)
    output_content_hash: str
    parent_hash: str | None
    transition_hash: str = ""
    timestamp: datetime = Field(default_factory=_utc_now)
    schema_version: int = 1
    outcome: LaneOutcome = "preferred"
    """Phase 68 routing outcome mirrored on the receipt.

    Mirrored here (in addition to ``LaneOutputBase.outcome``) so the receipt
    chain alone tells the routing history without needing to load lane
    outputs. Defaults to ``"preferred"`` for Phase 66/67-era backward compat.
    """


# ---------------------------------------------------------------------------
# Run + lane run records
# ---------------------------------------------------------------------------

RunStatus = Literal["pending", "in_progress", "completed", "failed", "blocked"]


class SwarmRun(BaseModel):
    """Top-level swarm run record (one per launched run).

    Stored both as a SQLite row (in ``runs.sqlite`` for cross-run query) and
    as a JSON file (in ``runs/<rid>/swarm_run.json`` for tamper-evident
    archival). The ``LaneStore`` is responsible for keeping the two in sync.
    """

    model_config = ConfigDict(frozen=True)

    run_id: UUID = Field(default_factory=uuid4)
    requested_at: datetime = Field(default_factory=_utc_now)
    status: RunStatus = "pending"
    reason_code: str | None = None
    """Free-text reason code for failed/blocked runs.

    Examples: ``SWAP_DEGRADED`` (mem-pressure rescue), ``LEASE_EXPIRED``
    (TTL fired during lane), ``LANE_FAILED`` (parse/quarantine exhaustion).
    Not enum-bounded here so plan 66-02 / 66-03 can extend without schema
    migration.
    """
    metadata: dict[str, str] = Field(default_factory=dict)
    """gsd v2.3 control-plane contract slot. Empty in v5.1; populated when
    the contract freeze is verified."""
    schema_version: int = 1


class LaneRun(BaseModel):
    """Per-lane execution record. One per role per SwarmRun in v5.1."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    lane_id: UUID = Field(default_factory=uuid4)
    role: Role
    upstream_outputs: list[LaneOutputBase] = Field(default_factory=list)
    """Full visibility: every prior lane's output, in chain order. The
    synthesizer thus sees all four upstream outputs; the planner sees an
    empty list."""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    output_id: UUID | None = None
    """ID of the LaneOutputBase produced by this lane (``lane_id`` of the
    output). Populated on completion; ``None`` while the lane is in flight."""
    metadata: dict[str, str] = Field(default_factory=dict)
    """gsd v2.3 control-plane contract slot. Empty in v5.1."""
    schema_version: int = 1
    routing_decision: dict[str, str] | None = None
    """Phase 68 routing decision for this lane.

    String-keyed for backward serialization compat. Carries
    ``{"model": <chosen_model>, "outcome": <LaneOutcome>,
    "swap_pct_at_decision": <float-as-str>}``. Populated by the runtime
    (plan 68-02) before each lane invocation; ``None`` for Phase 66/67-era
    serialized records and for any code path that bypasses ``select_lane_model``.
    """

class Checkpoint(BaseModel):
    """Per-run checkpoint -- single source of truth for "where did we stop".

    Persisted to ``<run_dir>/checkpoint.json`` after every lane completion
    (atomic temp+rename via :meth:`LaneStore.write_checkpoint`). The Phase 67
    resume orchestrator (plan 67-02) reads this file to decide which lane to
    re-enter; if the file is missing, the orchestrator falls back to walking
    ``lane_outputs/`` in role order.

    Fields
    ------
    run_id
        Run this checkpoint belongs to.
    last_completed_role
        Most recent role whose lane finished cleanly (output written +
        transition appended). ``None`` means no lane has completed yet
        (e.g., the run failed mid-planner before producing any output).
    holder_id
        Lease holder that wrote the checkpoint. Recorded so resume can
        warn / log when a different holder takes over.
    last_checkpoint_at
        UTC tz-aware timestamp when this checkpoint was written.
    schema_version
        Pinned to 1 for Phase 67. Phase 68 may bump to add fields like
        ``swap_percent_at_checkpoint`` or ``routing_ladder_state``.
    """

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    last_completed_role: Role | None
    holder_id: str
    last_checkpoint_at: datetime
    schema_version: int = 1


class ResumeCandidate(BaseModel):
    """Operator-facing description of a resumable run.

    Returned by :meth:`LaneStore.list_resumable_runs`. A run is "resumable"
    when its SwarmRun row has ``status='in_progress'`` AND its lease has
    expired (``leases.expires_at < now``) -- i.e., the prior holder's TTL
    fired and nobody has reclaimed.

    ``last_completed_role`` is read best-effort from the on-disk
    ``checkpoint.json``; ``None`` if the file is missing (the resume
    orchestrator handles that case by walking ``lane_outputs/``).
    """

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    last_completed_role: Role | None
    prior_holder_id: str
    lease_expired_at: datetime
    candidate_age_seconds: float
    schema_version: int = 1


__all__ = [
    "Checkpoint",
    "ExecutorOutput",
    "LaneOutcome",
    "LaneOutputBase",
    "LaneRoleLadder",
    "LaneRun",
    "LaneTransition",
    "PlannerOutput",
    "ResumeCandidate",
    "ReviewerOutput",
    "ROLE_CHAIN",
    "Role",
    "RunStatus",
    "SwarmRun",
    "SynthesizerOutput",
]
