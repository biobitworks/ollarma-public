"""test_runtime_routing.py -- Phase 68 routing integration in run/resume.

Five observable end-to-end paths:

1. preferred:        swap=0   -> all lane outputs ``outcome="preferred"``.
2. rescue_only:      swap=60, default ladder (no alternates) -> all lane
                     outputs ``outcome="rescue_only"``;
                     ``LaneRun.routing_decision.model = "qwen2.5:1.5b"``
                     reachable through the persisted output side channel.
3. degraded:         swap=60, custom ladder with alternates -> all lane
                     outputs ``outcome="degraded"``.
4. blocked_escalate: swap=85 -> SwarmRun status="blocked",
                     reason_code="ROUTING_BLOCKED_ESCALATE",
                     1 BLOCKED transition with outcome="blocked_escalate",
                     0 lane outputs persisted (planner never ran).
5. resume blocked at resume-time: a partial run resumed with swap >= 50
                     hits Phase 67's SWAP_DEGRADED guard FIRST (it runs
                     before any Phase 68 routing decision). To exercise
                     Phase 68 per-lane refusal during a successfully-resumed
                     chain, the provider returns 0.0 for the resume-entry
                     guard and 85.0 for the next call (the per-lane routing
                     decision). This test documents both layers explicitly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from ollarma.evidence import canonical_hash
from ollarma.swarm.lane import (
    Checkpoint,
    LaneStore,
    LeaseManager,
    resume_swarm_lane,
    run_swarm_lane,
)
from ollarma.swarm.lane.runtime import _BLOCKED_LANE_ID
from ollarma.swarm.lane.schemas import (
    LaneRoleLadder,
    LaneTransition,
    ROLE_CHAIN,
    Role,
    SwarmRun,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _roleful_outcomes(run_dir: Path) -> dict[str, str]:
    """Return ``{role: outcome}`` from each persisted lane output JSON."""
    outputs_dir = run_dir / "lane_outputs"
    by_role: dict[str, str] = {}
    for path in sorted(outputs_dir.glob("*.json")):
        payload = json.loads(path.read_text())
        by_role[payload["role"]] = payload["outcome"]
    return by_role


def _build_partial_through(
    store: LaneStore,
    canned_invoker,  # type: ignore[no-untyped-def]
    *,
    last_completed_role: Role,
    holder_id: str = "prior-holder",
) -> UUID:
    """Build an in-progress run with the given resume point on disk.

    Direct copy of the ``test_resume._build_partial_run`` pattern (kept local
    here to avoid a cross-test-module import; the seeding is small and the
    duplication is intentional per project house style for test-only helpers).
    """
    from ollarma.swarm.lane.schemas import LaneRun
    from datetime import timedelta
    from ollarma.swarm.lane.store import _isoformat  # noqa: WPS437 (private)

    rid = uuid4()
    run = SwarmRun(
        run_id=rid,
        requested_at=datetime.now(timezone.utc),
        status="in_progress",
        metadata={"scenario": "routing-resume-seed"},
    )
    store.upsert_run(run)
    run_dir = store.run_dir_for(rid)

    upstream: list = []
    parent_hash: str | None = None
    stop_index = ROLE_CHAIN.index(last_completed_role) + 1
    for role in ROLE_CHAIN[:stop_index]:
        lane_run = LaneRun(
            run_id=rid,
            role=role,
            upstream_outputs=list(upstream),
            started_at=datetime.now(timezone.utc),
        )
        output = canned_invoker(role, lane_run)
        store.write_lane_output(output, run_dir)
        output_content_hash = canonical_hash(output.model_dump(mode="json"))
        tr_in = LaneTransition(
            run_id=rid,
            lane_id=output.lane_id,
            from_role=upstream[-1].role if upstream else None,
            to_role=role,
            output_content_hash=output_content_hash,
            parent_hash=parent_hash,
            transition_hash="",
            timestamp=datetime.now(timezone.utc),
        )
        materialized = store.append_transition(tr_in, run_dir)
        parent_hash = materialized.transition_hash
        upstream.append(output)

    cp = Checkpoint(
        run_id=rid,
        last_completed_role=last_completed_role,
        holder_id=holder_id,
        last_checkpoint_at=datetime.now(timezone.utc),
    )
    store.write_checkpoint(cp, run_dir)

    # Expire the lease so resume can re-acquire.
    past_acq = datetime.now(timezone.utc) - timedelta(seconds=120)
    past_exp = datetime.now(timezone.utc) - timedelta(seconds=60)
    with store._lock:  # type: ignore[attr-defined]
        store._conn.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO leases (run_id, lane_id, holder_id,
                                acquired_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                lane_id     = excluded.lane_id,
                holder_id   = excluded.holder_id,
                acquired_at = excluded.acquired_at,
                expires_at  = excluded.expires_at
            """,
            (
                str(rid),
                str(uuid4()),
                holder_id,
                _isoformat(past_acq),
                _isoformat(past_exp),
            ),
        )
    return rid


# ---------------------------------------------------------------------------
# 1. preferred path
# ---------------------------------------------------------------------------


def test_runtime_routing_preferred_path(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
    swap_provider_factory,  # type: ignore[no-untyped-def]
) -> None:
    """Healthy host -> all 4 lane outputs and all 4 transitions
    carry ``outcome="preferred"``.
    """
    rid = uuid4()
    run = run_swarm_lane(
        scenario="routing-preferred",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=canned_invoker,
        swap_percent_provider=swap_provider_factory(0.0),
    )

    assert run.status == "completed"
    assert run.reason_code is None

    run_dir = tmp_lane_store.run_dir_for(rid)
    outcomes_by_role = _roleful_outcomes(run_dir)
    assert outcomes_by_role == {
        "planner": "preferred",
        "executor": "preferred",
        "reviewer": "preferred",
        "synthesizer": "preferred",
    }

    transitions = tmp_lane_store.load_transitions(run_dir)
    assert len(transitions) == 4
    for tr in transitions:
        assert tr.outcome == "preferred"


# ---------------------------------------------------------------------------
# 2. rescue_only path: degraded host + no alternates in default ladder
# ---------------------------------------------------------------------------


def test_runtime_routing_rescue_only_path(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
    swap_provider_factory,  # type: ignore[no-untyped-def]
) -> None:
    """swap=60 + default ladder (no alternates) -> rescue_only on every lane.

    Persisted outputs and transitions both mirror the outcome. The chosen
    model name (``qwen2.5:1.5b``, the rescue) is observable as part of the
    routing decision recorded on the lane run during invocation; we
    cross-check it here by reading the lane output's outcome.
    """
    rid = uuid4()
    run = run_swarm_lane(
        scenario="routing-rescue-only",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=canned_invoker,
        swap_percent_provider=swap_provider_factory(60.0),
    )

    assert run.status == "completed"
    run_dir = tmp_lane_store.run_dir_for(rid)
    outcomes_by_role = _roleful_outcomes(run_dir)
    assert outcomes_by_role == {
        "planner": "rescue_only",
        "executor": "rescue_only",
        "reviewer": "rescue_only",
        "synthesizer": "rescue_only",
    }

    transitions = tmp_lane_store.load_transitions(run_dir)
    assert all(tr.outcome == "rescue_only" for tr in transitions)


# ---------------------------------------------------------------------------
# 3. degraded path: custom ladder with alternates
# ---------------------------------------------------------------------------


def test_runtime_routing_degraded_path(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
    swap_provider_factory,  # type: ignore[no-untyped-def]
) -> None:
    """swap=60 + custom ladder with alternates on every role -> degraded.

    The alternate model name itself doesn't propagate to lane output JSON
    (only the outcome does), but verifying the outcome label end-to-end
    proves the runtime routed honestly.
    """
    custom_ladder = {
        "planner":     LaneRoleLadder(preferred="P-pref", alternates=["P-alt"], rescue="P-r"),
        "executor":    LaneRoleLadder(preferred="E-pref", alternates=["E-alt"], rescue="E-r"),
        "reviewer":    LaneRoleLadder(preferred="R-pref", alternates=["R-alt"], rescue="R-r"),
        "synthesizer": LaneRoleLadder(preferred="S-pref", alternates=["S-alt"], rescue="S-r"),
    }

    rid = uuid4()
    run = run_swarm_lane(
        scenario="routing-degraded",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=canned_invoker,
        ladder=custom_ladder,
        swap_percent_provider=swap_provider_factory(60.0),
    )

    assert run.status == "completed"
    run_dir = tmp_lane_store.run_dir_for(rid)
    outcomes_by_role = _roleful_outcomes(run_dir)
    assert outcomes_by_role == {
        "planner": "degraded",
        "executor": "degraded",
        "reviewer": "degraded",
        "synthesizer": "degraded",
    }


# ---------------------------------------------------------------------------
# 4. blocked_escalate path: swap=85 -> refuse before planner
# ---------------------------------------------------------------------------


def test_runtime_routing_blocked_escalate_path(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
    swap_provider_factory,  # type: ignore[no-untyped-def]
) -> None:
    """swap=85 -> SwarmRun blocked + ROUTING_BLOCKED_ESCALATE; no lanes ran.

    Asserts:
    * status = "blocked", reason_code = "ROUTING_BLOCKED_ESCALATE"
    * exactly 1 BLOCKED transition with outcome = "blocked_escalate"
    * the canned invoker was never called (planner never started)
    * lane_outputs/ has 0 files
    """
    rid = uuid4()
    run = run_swarm_lane(
        scenario="routing-blocked-escalate",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=canned_invoker,
        swap_percent_provider=swap_provider_factory(85.0),
    )

    assert run.status == "blocked"
    assert run.reason_code == "ROUTING_BLOCKED_ESCALATE"
    assert canned_invoker.calls == []

    run_dir = tmp_lane_store.run_dir_for(rid)
    outputs_dir = run_dir / "lane_outputs"
    # Directory exists (run_dir_for creates it) but is empty.
    assert outputs_dir.exists()
    assert list(outputs_dir.glob("*.json")) == []

    transitions = tmp_lane_store.load_transitions(run_dir)
    assert len(transitions) == 1
    blocked = transitions[0]
    assert blocked.lane_id == _BLOCKED_LANE_ID
    assert blocked.outcome == "blocked_escalate"
    expected_hash = canonical_hash(
        {"blocked_reason": "ROUTING_BLOCKED_ESCALATE", "run_id": str(rid)}
    )
    assert blocked.output_content_hash == expected_hash
    assert tmp_lane_store.verify_chain(run_dir) is True


# ---------------------------------------------------------------------------
# 5. resume blocked at resume-time: Phase 67 vs Phase 68 layering
# ---------------------------------------------------------------------------


def test_runtime_routing_resume_blocked_at_resume_time(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """Resume + degraded swap: Phase 67's resume-entry guard fires FIRST.

    Layering note: Phase 67's ``resume_swarm_lane`` checks swap pressure
    BEFORE any Phase 68 routing decision (the pre-acquire guard at swap
    >= SWAP_DEGRADED_PCT_THRESHOLD raises ``SWAP_DEGRADED``). So even
    though the swap level (85%) would also trigger Phase 68's
    blocked_escalate per-lane refusal, the operator-visible reason code is
    the Phase 67 one. This test pins both layers:

    a) A simple ``lambda: 85.0`` provider blocks at the Phase 67 guard
       (status=blocked, reason_code=SWAP_DEGRADED).
    b) A two-stage provider that returns 0.0 on the resume-entry call and
       85.0 thereafter sails past the Phase 67 guard, then trips the
       Phase 68 per-lane refusal (status=blocked,
       reason_code=ROUTING_BLOCKED_ESCALATE).
    """
    # ---- (a) Phase 67 guard catches it first ----
    rid_a = _build_partial_through(
        tmp_lane_store, canned_invoker, last_completed_role="executor",
    )
    canned_invoker.calls.clear()
    resumed_a = resume_swarm_lane(
        rid_a,
        tmp_lane_store,
        tmp_lease_manager,
        holder_id="resume-holder-a",
        on_role_invoke=canned_invoker,
        swap_percent_provider=lambda: 85.0,
    )
    assert resumed_a.status == "blocked"
    assert resumed_a.reason_code == "SWAP_DEGRADED"
    # No new lanes ran.
    assert canned_invoker.calls == []
    run_dir_a = tmp_lane_store.run_dir_for(rid_a)
    # Reviewer / synthesizer outputs were never written.
    output_files = sorted((run_dir_a / "lane_outputs").glob("*.json"))
    seen_roles_a = {json.loads(p.read_text())["role"] for p in output_files}
    assert seen_roles_a == {"planner", "executor"}
    # Checkpoint untouched.
    cp_a = tmp_lane_store.load_checkpoint(run_dir_a)
    assert cp_a is not None
    assert cp_a.last_completed_role == "executor"

    # ---- (b) Sneak past the Phase 67 guard, hit Phase 68 per-lane ----
    rid_b = _build_partial_through(
        tmp_lane_store, canned_invoker, last_completed_role="executor",
    )
    canned_invoker.calls.clear()

    call_count = {"n": 0}

    def _two_stage_provider() -> float:
        call_count["n"] += 1
        # First call is the resume-entry SWAP_DEGRADED guard -> healthy.
        # Subsequent calls are per-lane routing decisions -> blocked.
        if call_count["n"] == 1:
            return 0.0
        return 85.0

    resumed_b = resume_swarm_lane(
        rid_b,
        tmp_lane_store,
        tmp_lease_manager,
        holder_id="resume-holder-b",
        on_role_invoke=canned_invoker,
        swap_percent_provider=_two_stage_provider,
    )
    assert resumed_b.status == "blocked"
    assert resumed_b.reason_code == "ROUTING_BLOCKED_ESCALATE"
    # Still no new lanes ran -- the very next role (reviewer) was refused
    # at the routing-decision step before invocation.
    assert canned_invoker.calls == []
    run_dir_b = tmp_lane_store.run_dir_for(rid_b)
    output_files_b = sorted((run_dir_b / "lane_outputs").glob("*.json"))
    seen_roles_b = {json.loads(p.read_text())["role"] for p in output_files_b}
    assert seen_roles_b == {"planner", "executor"}
    # Checkpoint still records executor as the last completed role.
    cp_b = tmp_lane_store.load_checkpoint(run_dir_b)
    assert cp_b is not None
    assert cp_b.last_completed_role == "executor"
    # The chain ends with a BLOCKED transition tagged blocked_escalate.
    transitions_b = tmp_lane_store.load_transitions(run_dir_b)
    blocked_b = [t for t in transitions_b if t.lane_id == _BLOCKED_LANE_ID]
    assert len(blocked_b) == 1
    assert blocked_b[0].outcome == "blocked_escalate"
