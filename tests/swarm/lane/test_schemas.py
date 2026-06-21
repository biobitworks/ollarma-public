"""test_schemas.py -- pydantic round-trip + bounds + frozen + invalid-role.

Phase 66 plan 01 coverage of ``ollarma.swarm.lane.schemas``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ollarma.swarm.lane.schemas import (
    ROLE_CHAIN,
    ExecutorOutput,
    LaneOutputBase,
    LaneRun,
    LaneTransition,
    PlannerOutput,
    ReviewerOutput,
    SwarmRun,
    SynthesizerOutput,
)


# ---------------------------------------------------------------------------
# Role + ROLE_CHAIN
# ---------------------------------------------------------------------------

def test_role_chain_constant_is_correct() -> None:
    assert ROLE_CHAIN == ("planner", "executor", "reviewer", "synthesizer")
    # Ensure it's a tuple (immutable) not a list.
    assert isinstance(ROLE_CHAIN, tuple)


def test_role_literal_rejects_invalid() -> None:
    """Pydantic should reject a role outside the Literal set."""
    with pytest.raises(ValidationError):
        # ``role`` is fixed to "planner" on PlannerOutput, but the base
        # validator runs through Role; building via the base class with a
        # bogus role string is the cleanest way to trip it.
        LaneOutputBase(
            run_id=uuid4(),
            lane_id=uuid4(),
            role="manager",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Round-trips
# ---------------------------------------------------------------------------

def _round_trip(model_cls, instance):
    dumped = instance.model_dump(mode="json")
    rebuilt = model_cls.model_validate(dumped)
    assert rebuilt == instance


def test_planner_output_round_trip() -> None:
    p = PlannerOutput(
        run_id=uuid4(),
        lane_id=uuid4(),
        plan_steps=["step-a", "step-b"],
        rationale="because",
    )
    assert p.role == "planner"
    _round_trip(PlannerOutput, p)


def test_executor_output_round_trip() -> None:
    e = ExecutorOutput(
        run_id=uuid4(),
        lane_id=uuid4(),
        actions_taken=["wrote /tmp/a.txt"],
        artifacts_written=["/tmp/a.txt"],
        success=True,
    )
    assert e.role == "executor"
    assert e.error_message is None
    _round_trip(ExecutorOutput, e)


def test_reviewer_output_round_trip() -> None:
    r = ReviewerOutput(
        run_id=uuid4(),
        lane_id=uuid4(),
        findings=["finding-1"],
        severity="warn",
        summary="non-blocking",
    )
    assert r.role == "reviewer"
    _round_trip(ReviewerOutput, r)


def test_synthesizer_output_round_trip() -> None:
    s = SynthesizerOutput(
        run_id=uuid4(),
        lane_id=uuid4(),
        final_summary="all good",
        decisions=["ship it"],
        next_actions=["update STATE.md"],
    )
    assert s.role == "synthesizer"
    _round_trip(SynthesizerOutput, s)


def test_lane_transition_round_trip() -> None:
    t = LaneTransition(
        run_id=uuid4(),
        lane_id=uuid4(),
        from_role=None,
        to_role="planner",
        output_content_hash="0" * 64,
        parent_hash=None,
        transition_hash="abc",  # placeholder; store fills the real value
    )
    _round_trip(LaneTransition, t)


def test_swarm_run_round_trip() -> None:
    sr = SwarmRun(
        requested_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
        status="in_progress",
        reason_code=None,
        metadata={"gsd_contract_version": "v2.3-tbd"},
    )
    _round_trip(SwarmRun, sr)


def test_lane_run_round_trip() -> None:
    rid = uuid4()
    upstream = [
        PlannerOutput(
            run_id=rid,
            lane_id=uuid4(),
            plan_steps=["a"],
            rationale="r",
        ),
    ]
    lr = LaneRun(
        run_id=rid,
        role="executor",
        upstream_outputs=upstream,
    )
    dumped = lr.model_dump(mode="json")
    rebuilt = LaneRun.model_validate(dumped)
    # ``upstream_outputs`` round-trips into LaneOutputBase (the typed
    # subclass discrimination is the orchestrator's job in plan 02).
    assert rebuilt.run_id == lr.run_id
    assert rebuilt.role == "executor"
    assert len(rebuilt.upstream_outputs) == 1
    assert rebuilt.upstream_outputs[0].role == "planner"


# ---------------------------------------------------------------------------
# Frozen / bounded
# ---------------------------------------------------------------------------

def test_lane_output_base_is_frozen() -> None:
    p = PlannerOutput(
        run_id=uuid4(),
        lane_id=uuid4(),
        plan_steps=["a"],
        rationale="r",
    )
    with pytest.raises(ValidationError):
        # pydantic v2 frozen models raise ValidationError on attribute set.
        p.rationale = "mutated"  # type: ignore[misc]


def test_planner_rationale_max_length() -> None:
    too_long = "x" * 2001
    with pytest.raises(ValidationError):
        PlannerOutput(
            run_id=uuid4(),
            lane_id=uuid4(),
            plan_steps=["a"],
            rationale=too_long,
        )


def test_swarm_run_status_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        SwarmRun(
            status="bogus",  # type: ignore[arg-type]
        )


def test_reviewer_severity_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        ReviewerOutput(
            run_id=uuid4(),
            lane_id=uuid4(),
            findings=[],
            severity="critical",  # type: ignore[arg-type]
            summary="x",
        )


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

def test_all_models_carry_schema_version_one() -> None:
    p = PlannerOutput(
        run_id=uuid4(), lane_id=uuid4(), plan_steps=[], rationale="",
    )
    e = ExecutorOutput(
        run_id=uuid4(), lane_id=uuid4(),
        actions_taken=[], artifacts_written=[], success=True,
    )
    r = ReviewerOutput(
        run_id=uuid4(), lane_id=uuid4(),
        findings=[], severity="pass", summary="",
    )
    s = SynthesizerOutput(
        run_id=uuid4(), lane_id=uuid4(),
        final_summary="", decisions=[], next_actions=[],
    )
    t = LaneTransition(
        run_id=uuid4(), lane_id=uuid4(),
        from_role=None, to_role=None,
        output_content_hash="0" * 64, parent_hash=None,
    )
    sr = SwarmRun()
    lr = LaneRun(run_id=uuid4(), role="planner")
    for m in (p, e, r, s, t, sr, lr):
        assert m.schema_version == 1
