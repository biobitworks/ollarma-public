"""test_routing_schemas.py -- Phase 68 routing schemas + ladder defaults + ReasonCode.

Covers:
* ``LaneRoleLadder`` round-trip + alternates default
* ``LaneOutcome`` literal validation + propagation through LaneOutputBase /
  PlannerOutput / LaneTransition
* ``LaneRun.routing_decision`` optional default
* ``DEFAULT_LANE_LADDER`` shape + executor preferred = ``qwen2.5-coder:7b``
* Backward-compat: a Phase 66/67-era serialized ``PlannerOutput`` (no
  ``outcome`` field) loads cleanly with the default ``"preferred"``
* New ``ReasonCode.ROUTING_*`` values exposed with the expected string values
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ollarma.escalation import ReasonCode
from ollarma.swarm._runtime_contract import (
    DEFAULT_LANE_LADDER,
    SWAP_BLOCKED_PCT_THRESHOLD,
    SWAP_DEGRADED_PCT_THRESHOLD,
)
from ollarma.swarm.lane import LaneOutcome, LaneRoleLadder
from ollarma.swarm.lane.schemas import (
    LaneRun,
    LaneTransition,
    PlannerOutput,
)


# ---------------------------------------------------------------------------
# LaneRoleLadder
# ---------------------------------------------------------------------------

def test_lane_role_ladder_round_trip() -> None:
    """Construct, dump, validate, equality."""
    ladder = LaneRoleLadder(
        preferred="qwen2.5-coder:7b",
        alternates=["qwen3:4b"],
        rescue="qwen2.5:1.5b",
    )
    dumped = ladder.model_dump()
    rebuilt = LaneRoleLadder.model_validate(dumped)
    assert rebuilt == ladder
    assert rebuilt.preferred == "qwen2.5-coder:7b"
    assert rebuilt.alternates == ["qwen3:4b"]
    assert rebuilt.rescue == "qwen2.5:1.5b"
    assert rebuilt.schema_version == 1


def test_lane_role_ladder_alternates_optional() -> None:
    """``alternates`` defaults to empty list."""
    ladder = LaneRoleLadder(preferred="phi4-mini", rescue="qwen2.5:1.5b")
    assert ladder.alternates == []


def test_lane_role_ladder_is_frozen() -> None:
    """Ladder is frozen (mutation raises)."""
    ladder = LaneRoleLadder(preferred="phi4-mini", rescue="qwen2.5:1.5b")
    with pytest.raises(ValidationError):
        ladder.preferred = "qwen2.5:1.5b"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LaneOutcome literal + propagation
# ---------------------------------------------------------------------------

def test_lane_outcome_literal_rejects_invalid() -> None:
    """``outcome="bad"`` on PlannerOutput raises ValidationError."""
    with pytest.raises(ValidationError):
        PlannerOutput(
            run_id=uuid4(),
            lane_id=uuid4(),
            plan_steps=["step 1"],
            rationale="why",
            outcome="bad",  # type: ignore[arg-type]
        )


def test_lane_outcome_default_is_preferred() -> None:
    """PlannerOutput without ``outcome=`` defaults to ``"preferred"``."""
    p = PlannerOutput(
        run_id=uuid4(),
        lane_id=uuid4(),
        plan_steps=["step 1"],
        rationale="why",
    )
    assert p.outcome == "preferred"


def test_lane_outcome_accepts_all_four_values() -> None:
    """All four LaneOutcome literal values are accepted."""
    for outcome in ("preferred", "degraded", "rescue_only", "blocked_escalate"):
        p = PlannerOutput(
            run_id=uuid4(),
            lane_id=uuid4(),
            plan_steps=["s"],
            rationale="r",
            outcome=outcome,  # type: ignore[arg-type]
        )
        assert p.outcome == outcome


def test_lane_transition_outcome_default_is_preferred() -> None:
    """LaneTransition without ``outcome=`` defaults to ``"preferred"``."""
    t = LaneTransition(
        run_id=uuid4(),
        lane_id=uuid4(),
        from_role=None,
        to_role="planner",
        output_content_hash="abc",
        parent_hash=None,
    )
    assert t.outcome == "preferred"


# ---------------------------------------------------------------------------
# LaneRun.routing_decision
# ---------------------------------------------------------------------------

def test_lane_run_routing_decision_optional() -> None:
    """LaneRun without ``routing_decision=`` defaults to None."""
    lr = LaneRun(run_id=uuid4(), role="planner")
    assert lr.routing_decision is None


def test_lane_run_routing_decision_string_keyed() -> None:
    """LaneRun accepts a string-keyed routing_decision payload."""
    lr = LaneRun(
        run_id=uuid4(),
        role="executor",
        routing_decision={
            "model": "qwen2.5-coder:7b",
            "outcome": "preferred",
            "swap_pct_at_decision": "12.3",
        },
    )
    assert lr.routing_decision is not None
    assert lr.routing_decision["model"] == "qwen2.5-coder:7b"
    assert lr.routing_decision["outcome"] == "preferred"


# ---------------------------------------------------------------------------
# DEFAULT_LANE_LADDER + thresholds
# ---------------------------------------------------------------------------

def test_default_lane_ladder_has_all_4_roles() -> None:
    """DEFAULT_LANE_LADDER covers exactly the v5.1 lane chain."""
    assert set(DEFAULT_LANE_LADDER.keys()) == {
        "planner",
        "executor",
        "reviewer",
        "synthesizer",
    }


def test_default_lane_ladder_executor_uses_qwen_coder_7b() -> None:
    """Executor preferred matches Phase 70 / GPU residency policy."""
    assert DEFAULT_LANE_LADDER["executor"].preferred == "qwen2.5-coder:7b"
    assert DEFAULT_LANE_LADDER["reviewer"].preferred == "qwen2.5-coder:7b"
    assert DEFAULT_LANE_LADDER["planner"].preferred == "qwen2.5:1.5b"
    assert DEFAULT_LANE_LADDER["synthesizer"].preferred == "qwen2.5:1.5b"


def test_default_lane_ladder_rescue_is_always_small() -> None:
    """Rescue model is always the always-pinned small model."""
    for role, ladder in DEFAULT_LANE_LADDER.items():
        assert ladder.rescue == "qwen2.5:1.5b", f"role={role}"


def test_swap_blocked_threshold_is_strictly_greater_than_degraded() -> None:
    """BLOCKED escalation must be monotonic above DEGRADED."""
    assert SWAP_BLOCKED_PCT_THRESHOLD == 80
    assert SWAP_BLOCKED_PCT_THRESHOLD > SWAP_DEGRADED_PCT_THRESHOLD


# ---------------------------------------------------------------------------
# Backward compat: Phase 66/67-era serialized records still load
# ---------------------------------------------------------------------------

def test_backward_compat_load_phase_66_planner_output() -> None:
    """A Phase 66-era PlannerOutput JSON (no ``outcome``) validates with default."""
    legacy_payload = {
        "run_id": str(uuid4()),
        "lane_id": str(uuid4()),
        "plan_steps": ["step a", "step b"],
        "rationale": "Phase 66-era output, no outcome field present",
    }
    p = PlannerOutput.model_validate(legacy_payload)
    assert p.outcome == "preferred"
    assert p.role == "planner"


def test_backward_compat_load_phase_66_lane_transition() -> None:
    """A Phase 66-era LaneTransition JSON (no ``outcome``) validates with default."""
    legacy_payload = {
        "run_id": str(uuid4()),
        "lane_id": str(uuid4()),
        "from_role": "planner",
        "to_role": "executor",
        "output_content_hash": "deadbeef",
        "parent_hash": None,
    }
    t = LaneTransition.model_validate(legacy_payload)
    assert t.outcome == "preferred"


def test_backward_compat_load_phase_66_lane_run() -> None:
    """A Phase 66-era LaneRun JSON (no ``routing_decision``) validates as None."""
    legacy_payload = {
        "run_id": str(uuid4()),
        "role": "executor",
    }
    lr = LaneRun.model_validate(legacy_payload)
    assert lr.routing_decision is None


# ---------------------------------------------------------------------------
# ReasonCode extension
# ---------------------------------------------------------------------------

def test_reason_code_routing_values() -> None:
    """Phase 68 ReasonCode additions expose the documented string values."""
    assert ReasonCode.ROUTING_DEGRADED.value == "ROUTING_DEGRADED"
    assert ReasonCode.ROUTING_RESCUE_ONLY.value == "ROUTING_RESCUE_ONLY"
    assert ReasonCode.ROUTING_BLOCKED_ESCALATE.value == "ROUTING_BLOCKED_ESCALATE"
