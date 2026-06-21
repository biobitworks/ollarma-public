"""routing.py -- Phase 68 routing-policy pure functions.

Maps a per-role :class:`LaneRoleLadder` plus a measured swap-pressure
percent to a (model_name, outcome) decision. The runtime
(:mod:`ollarma.swarm.lane.runtime`) calls :func:`select_lane_model` BEFORE
each lane invocation; if the outcome is ``"blocked_escalate"`` the runtime
refuses the lane and emits a BLOCKED transition without calling any model.

Pure-function contract
----------------------
:func:`select_lane_model` and :func:`_merge_ladder` are PURE: no I/O, no
logging, no side effects, no time. All inputs are explicit; the outputs
are fully determined by the inputs. This keeps the policy unit-testable
without fixtures or mocks.

Thresholds live in :mod:`ollarma.swarm._runtime_contract` so Phase 67's
SWAP_DEGRADED_PCT_THRESHOLD and Phase 68's SWAP_BLOCKED_PCT_THRESHOLD
stay co-located with the rest of the hardware-contract knobs.
"""
from __future__ import annotations

from ollarma.swarm._runtime_contract import (
    DEFAULT_LANE_LADDER,
    SWAP_BLOCKED_PCT_THRESHOLD,
    SWAP_DEGRADED_PCT_THRESHOLD,
)
from ollarma.swarm.lane.schemas import LaneOutcome, LaneRoleLadder, Role


__all__ = ["select_lane_model"]


def select_lane_model(
    role: Role,
    ladder: LaneRoleLadder,
    swap_pct: float,
) -> tuple[str, LaneOutcome]:
    """Pure-function routing decision.

    Parameters
    ----------
    role
        Lane role being scheduled (planner / executor / reviewer / synthesizer).
        Currently unused by the decision logic itself -- routing is governed
        entirely by ``ladder`` + ``swap_pct`` -- but accepted in the signature
        so a future per-role override can land without a caller-side break.
    ladder
        Per-role ladder (preferred + alternates + rescue).
    swap_pct
        Live swap-usage percent, in ``[0.0, 100.0]``. Anything outside that
        range still flows through the threshold ladder honestly: negative
        values count as healthy (preferred path); ``>= SWAP_BLOCKED_PCT_THRESHOLD``
        always refuses.

    Returns
    -------
    (model_name, outcome)
        On ``"blocked_escalate"`` the model_name is ``""`` (empty string) --
        the caller MUST check ``outcome`` and refuse to invoke. The
        empty-string sentinel keeps the return type homogeneous (no Optional)
        and matches Phase 66's "explicit empty for refusal" pattern.

    Decision logic
    --------------
    1. ``swap_pct >= SWAP_BLOCKED_PCT_THRESHOLD`` -> ``("", "blocked_escalate")``
    2. ``swap_pct >= SWAP_DEGRADED_PCT_THRESHOLD`` AND ``ladder.alternates``
       -> ``(ladder.alternates[0], "degraded")``
    3. ``swap_pct >= SWAP_DEGRADED_PCT_THRESHOLD`` AND no alternates
       -> ``(ladder.rescue, "rescue_only")``
    4. else -> ``(ladder.preferred, "preferred")``
    """
    # ``role`` is intentionally unused today; see docstring rationale.
    del role

    if swap_pct >= SWAP_BLOCKED_PCT_THRESHOLD:
        return ("", "blocked_escalate")
    if swap_pct >= SWAP_DEGRADED_PCT_THRESHOLD:
        if ladder.alternates:
            return (ladder.alternates[0], "degraded")
        return (ladder.rescue, "rescue_only")
    return (ladder.preferred, "preferred")


def _merge_ladder(
    override: dict[Role, LaneRoleLadder] | None,
) -> dict[Role, LaneRoleLadder]:
    """Merge an optional per-call ladder override on top of :data:`DEFAULT_LANE_LADDER`.

    Caller-supplied ``override`` overrides defaults role-by-role. Missing
    roles in ``override`` fall through to the default. ``None`` returns a
    shallow copy of the default registry.

    The returned dict always contains every role in ``ROLE_CHAIN``.
    """
    if override is None:
        return dict(DEFAULT_LANE_LADDER)
    merged = dict(DEFAULT_LANE_LADDER)
    merged.update(override)
    return merged
