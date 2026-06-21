"""test_routing.py -- Phase 68 ``select_lane_model`` + ``_merge_ladder`` policy.

Pure-function policy tests. No fixtures, no I/O, no Ollama. Each case pins
one observable input -> output mapping per the decision logic in
:func:`ollarma.swarm.lane.routing.select_lane_model`:

1. swap_pct < SWAP_DEGRADED_PCT_THRESHOLD                    -> preferred
2. swap_pct in [DEGRADED, BLOCKED) AND no alternates         -> rescue_only
3. swap_pct in [DEGRADED, BLOCKED) AND alternates            -> degraded
4. swap_pct >= SWAP_BLOCKED_PCT_THRESHOLD                    -> blocked_escalate

Plus :func:`_merge_ladder` coverage (None / partial / full override).
"""
from __future__ import annotations

from ollarma.swarm._runtime_contract import (
    DEFAULT_LANE_LADDER,
    SWAP_BLOCKED_PCT_THRESHOLD,
    SWAP_DEGRADED_PCT_THRESHOLD,
)
from ollarma.swarm.lane.routing import _merge_ladder, select_lane_model
from ollarma.swarm.lane.schemas import LaneRoleLadder


# ---------------------------------------------------------------------------
# select_lane_model decision matrix
# ---------------------------------------------------------------------------


def test_select_lane_model_preferred() -> None:
    """Healthy host (swap=0) + default executor ladder -> preferred."""
    model, outcome = select_lane_model(
        "executor", DEFAULT_LANE_LADDER["executor"], swap_pct=0.0,
    )
    assert outcome == "preferred"
    assert model == "qwen2.5-coder:7b"


def test_select_lane_model_rescue_only_no_alternates() -> None:
    """Degraded host + no alternates in ladder -> rescue_only.

    The default executor ladder has no alternates, so the DEGRADED branch
    falls through to the rescue model (``qwen2.5:1.5b``).
    """
    model, outcome = select_lane_model(
        "executor", DEFAULT_LANE_LADDER["executor"], swap_pct=60.0,
    )
    assert outcome == "rescue_only"
    assert model == "qwen2.5:1.5b"


def test_select_lane_model_degraded_with_alternates() -> None:
    """Degraded host + alternates available -> degraded (alternates[0]).

    Custom ladder with three alternatives; degraded picks the FIRST
    alternate, not the rescue and not a later alternate.
    """
    custom = LaneRoleLadder(
        preferred="A", alternates=["B", "C"], rescue="D",
    )
    model, outcome = select_lane_model("executor", custom, swap_pct=60.0)
    assert outcome == "degraded"
    assert model == "B"


def test_select_lane_model_blocked_escalate() -> None:
    """swap >= BLOCKED threshold -> ('', 'blocked_escalate'); empty model
    is the refusal sentinel and the caller must check outcome before invoking.

    The ladder content is irrelevant here -- BLOCKED short-circuits.
    """
    model, outcome = select_lane_model(
        "executor", DEFAULT_LANE_LADDER["executor"], swap_pct=85.0,
    )
    assert outcome == "blocked_escalate"
    assert model == ""

    # Also blocked when a custom ladder with alternates is supplied.
    custom = LaneRoleLadder(
        preferred="A", alternates=["B"], rescue="C",
    )
    model2, outcome2 = select_lane_model("executor", custom, swap_pct=85.0)
    assert outcome2 == "blocked_escalate"
    assert model2 == ""


def test_select_lane_model_threshold_boundaries() -> None:
    """Exact-boundary behavior at DEGRADED (50) and BLOCKED (80) thresholds.

    The thresholds are ``>=`` -- 49.99 is healthy; 50.0 is degraded; 79.99
    is degraded; 80.0 is blocked. Using the default executor ladder means
    the DEGRADED branch resolves to ``rescue_only`` (no alternates).
    """
    ladder = DEFAULT_LANE_LADDER["executor"]

    # Sanity-check the constants that drive the boundaries (test would lie
    # silently if the contract changed).
    assert SWAP_DEGRADED_PCT_THRESHOLD == 50
    assert SWAP_BLOCKED_PCT_THRESHOLD == 80

    _, outcome_just_under_degraded = select_lane_model(
        "executor", ladder, swap_pct=49.99,
    )
    assert outcome_just_under_degraded == "preferred"

    _, outcome_at_degraded = select_lane_model(
        "executor", ladder, swap_pct=50.0,
    )
    assert outcome_at_degraded == "rescue_only"

    _, outcome_just_under_blocked = select_lane_model(
        "executor", ladder, swap_pct=79.99,
    )
    assert outcome_just_under_blocked == "rescue_only"

    _, outcome_at_blocked = select_lane_model(
        "executor", ladder, swap_pct=80.0,
    )
    assert outcome_at_blocked == "blocked_escalate"


# ---------------------------------------------------------------------------
# _merge_ladder
# ---------------------------------------------------------------------------


def test_merge_ladder_none_returns_defaults() -> None:
    """``_merge_ladder(None)`` returns a dict with all 4 default roles.

    The result is a shallow copy -- mutating it must not corrupt the
    module-level constant.
    """
    merged = _merge_ladder(None)
    assert set(merged.keys()) == {"planner", "executor", "reviewer", "synthesizer"}
    # Same content as defaults role-by-role.
    for role, ladder in DEFAULT_LANE_LADDER.items():
        assert merged[role] == ladder

    # Mutating the merged dict does not bleed back into the constant.
    merged["executor"] = LaneRoleLadder(preferred="X", rescue="Y")
    assert DEFAULT_LANE_LADDER["executor"].preferred == "qwen2.5-coder:7b"


def test_merge_ladder_partial_override() -> None:
    """Override just ``executor`` -> executor is custom, other 3 are defaults."""
    custom_executor = LaneRoleLadder(
        preferred="custom-coder", alternates=["fallback-coder"], rescue="rescue-coder",
    )
    merged = _merge_ladder({"executor": custom_executor})

    assert merged["executor"] == custom_executor
    # Other roles unchanged.
    assert merged["planner"] == DEFAULT_LANE_LADDER["planner"]
    assert merged["reviewer"] == DEFAULT_LANE_LADDER["reviewer"]
    assert merged["synthesizer"] == DEFAULT_LANE_LADDER["synthesizer"]


def test_merge_ladder_full_override() -> None:
    """Override all 4 roles -> all 4 are custom; defaults not present."""
    full = {
        "planner":     LaneRoleLadder(preferred="P", rescue="Pr"),
        "executor":    LaneRoleLadder(preferred="E", rescue="Er"),
        "reviewer":    LaneRoleLadder(preferred="R", rescue="Rr"),
        "synthesizer": LaneRoleLadder(preferred="S", rescue="Sr"),
    }
    merged = _merge_ladder(full)

    assert merged["planner"].preferred == "P"
    assert merged["executor"].preferred == "E"
    assert merged["reviewer"].preferred == "R"
    assert merged["synthesizer"].preferred == "S"
    # Critically: none of the defaults survived (the override is exhaustive).
    for role, ladder in merged.items():
        assert ladder != DEFAULT_LANE_LADDER[role]
