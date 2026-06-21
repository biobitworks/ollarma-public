"""test_resume.py -- ``resume_swarm_lane`` orchestrator coverage (Phase 67 plan 03).

Covers the eight observable resume paths:

1. Idempotent resume of an already-completed run (no-op return).
2. Resume blocked by swap-degraded stub provider.
3. Resume blocked by a lease held by a different holder.
4-7. Resume from each possible ``last_completed_role`` (planner / executor /
   reviewer / synthesizer pre-completion) -- chain continues from the next role.
8. Resume with no ``checkpoint.json`` -- falls back to walking
   ``lane_outputs/`` to infer the resume point.
9. Resume of an unknown ``run_id`` -- raises ``ValueError`` with literal
   ``RESUME_NO_CANDIDATE`` in the message.

Test design constraints (per 67-03 plan + Wave-2 deviation notes):

* No real Ollama: every test stubs ``on_role_invoke``.
* No real ``time.sleep``: lease expiry is simulated via direct SQLite
  mutation (mirroring ``test_checkpoint.py``'s ``_seed_run_with_lease``
  helper). Total wall < ~2s for this file.
* The Wave-2 runtime accepts ``status in ('in_progress', 'failed')`` for
  resume (and short-circuits ``status='completed'`` as a no-op). Tests that
  build partial state seed the row as ``status='in_progress'``.
* ``_invoke_with_token_loss_handling`` is a separate concern -- exercised
  in :mod:`test_token_loss`. This module exclusively exercises the resume
  orchestrator's control flow.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

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
    LaneTransition,
    ROLE_CHAIN,
    Role,
    SwarmRun,
)


# ---------------------------------------------------------------------------
# On-disk seeding helpers
# ---------------------------------------------------------------------------


def _expire_lease_in_sql(
    store: LaneStore, run_id: UUID, *, holder_id: str = "prior-holder",
) -> None:
    """Insert (or overwrite) a lease row whose ``expires_at`` is firmly in
    the past, so ``LeaseManager.acquire`` will treat it as
    ``expired_reclaimed``-eligible without any wall-clock waiting.

    Mirrors the direct-SQL pattern in ``test_checkpoint.py``'s
    ``_seed_run_with_lease`` helper -- this is the project-house style for
    deterministic lease-state simulation.
    """
    from ollarma.swarm.lane.store import _isoformat  # noqa: WPS437 (private)

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
                str(run_id),
                str(uuid4()),
                holder_id,
                _isoformat(past_acq),
                _isoformat(past_exp),
            ),
        )


def _build_partial_run(
    store: LaneStore,
    canned_invoker,  # type: ignore[no-untyped-def]
    *,
    last_completed_role: Role | None,
    write_checkpoint: bool = True,
    holder_id: str = "prior-holder",
) -> UUID:
    """Construct a partially-completed run on disk through the actual store
    APIs (so the hash chain stays honest), then expire the lease.

    Walks the chain up to and including ``last_completed_role`` calling
    ``canned_invoker`` for each role and persisting the outputs +
    transitions via ``LaneStore``. After this returns, the run is in a
    state indistinguishable from a real crashed-mid-run scenario.
    """
    rid = uuid4()
    requested_at = datetime.now(timezone.utc)

    # 1. SwarmRun row marked in_progress.
    run = SwarmRun(
        run_id=rid,
        requested_at=requested_at,
        status="in_progress",
        metadata={"scenario": "partial-run-seed"},
    )
    store.upsert_run(run)

    # 2. Walk the chain through last_completed_role, persisting outputs +
    #    transitions exactly the way runtime.py would.
    run_dir = store.run_dir_for(rid)
    upstream: list = []  # noqa: UP006 (heterogeneous LaneOutputBase subclasses)
    parent_hash: str | None = None

    if last_completed_role is not None:
        stop_index = ROLE_CHAIN.index(last_completed_role) + 1
        for role in ROLE_CHAIN[:stop_index]:
            from ollarma.swarm.lane.schemas import LaneRun

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

        if write_checkpoint:
            cp = Checkpoint(
                run_id=rid,
                last_completed_role=last_completed_role,
                holder_id=holder_id,
                last_checkpoint_at=datetime.now(timezone.utc),
            )
            store.write_checkpoint(cp, run_dir)

    # 3. Expire the lease via direct SQL.
    _expire_lease_in_sql(store, rid, holder_id=holder_id)
    return rid


# ---------------------------------------------------------------------------
# 1. Idempotency: completed run resumes to itself, untouched
# ---------------------------------------------------------------------------


def test_resume_idempotent_completed_run(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """A run that already finished returns its existing SwarmRun unchanged.

    Crucially, no new transitions are appended and no new lane outputs are
    written -- the resume is a pure no-op for completed runs.
    """
    rid = uuid4()
    completed = run_swarm_lane(
        scenario="idempotent-resume",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=canned_invoker,
    )
    assert completed.status == "completed"

    run_dir = tmp_lane_store.run_dir_for(rid)
    transitions_before = tmp_lane_store.load_transitions(run_dir)
    outputs_before = sorted((run_dir / "lane_outputs").glob("*.json"))

    resumed = resume_swarm_lane(
        rid,
        tmp_lane_store,
        tmp_lease_manager,
        on_role_invoke=canned_invoker,
    )
    assert resumed.status == "completed"
    assert resumed.reason_code is None
    assert resumed.run_id == rid

    transitions_after = tmp_lane_store.load_transitions(run_dir)
    outputs_after = sorted((run_dir / "lane_outputs").glob("*.json"))
    assert transitions_after == transitions_before
    assert outputs_after == outputs_before


# ---------------------------------------------------------------------------
# 2. Swap-pressure: resume refused with BLOCKED + SWAP_DEGRADED
# ---------------------------------------------------------------------------


def test_resume_blocked_by_swap_degraded(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """``swap_percent_provider`` returning >= threshold -> BLOCKED + SWAP_DEGRADED.

    The lease must NOT be acquired (the swap check happens BEFORE lease
    acquisition per the runtime ordering -- "don't take the lease just to
    immediately drop it"). We assert no fresh lease row materializes.
    """
    rid = _build_partial_run(
        tmp_lane_store, canned_invoker, last_completed_role="planner",
    )

    resumed = resume_swarm_lane(
        rid,
        tmp_lane_store,
        tmp_lease_manager,
        holder_id="resume-holder",
        on_role_invoke=canned_invoker,
        swap_percent_provider=lambda: 75.0,
    )

    assert resumed.status == "blocked"
    assert resumed.reason_code == "SWAP_DEGRADED"

    # BLOCKED transition appended after the planner transition (chain
    # continues honestly).
    run_dir = tmp_lane_store.run_dir_for(rid)
    transitions = tmp_lane_store.load_transitions(run_dir)
    blocked = [t for t in transitions if t.lane_id == _BLOCKED_LANE_ID]
    assert len(blocked) == 1
    expected_hash = canonical_hash(
        {"blocked_reason": "SWAP_DEGRADED", "run_id": str(rid)}
    )
    assert blocked[0].output_content_hash == expected_hash
    assert tmp_lane_store.verify_chain(run_dir) is True

    # Lease row must still belong to the prior holder (we never acquired).
    lease = tmp_lane_store.get_lease(rid)
    assert lease is not None
    assert lease[1] == "prior-holder"


# ---------------------------------------------------------------------------
# 3. Lease held by other -> BLOCKED + LEASE_HELD_BY_OTHER
# ---------------------------------------------------------------------------


def test_resume_blocked_by_held_lease(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """Another holder owns an unexpired lease -> resume blocks honestly."""
    rid = _build_partial_run(
        tmp_lane_store, canned_invoker, last_completed_role="executor",
    )

    # Force a fresh, unexpired lease held by 'alice' (overwriting the
    # _build_partial_run-installed expired row).
    alice_decision = tmp_lease_manager.acquire(
        rid, holder_id="alice", ttl_seconds=300,
    )
    assert alice_decision.acquired is True

    resumed = resume_swarm_lane(
        rid,
        tmp_lane_store,
        tmp_lease_manager,
        holder_id="bob",
        on_role_invoke=canned_invoker,
    )
    assert resumed.status == "blocked"
    assert resumed.reason_code == "LEASE_HELD_BY_OTHER"

    # Alice's lease is intact.
    lease = tmp_lane_store.get_lease(rid)
    assert lease is not None
    assert lease[1] == "alice"

    # BLOCKED transition appended for the rejection.
    run_dir = tmp_lane_store.run_dir_for(rid)
    transitions = tmp_lane_store.load_transitions(run_dir)
    blocked = [t for t in transitions if t.lane_id == _BLOCKED_LANE_ID]
    assert len(blocked) == 1
    expected_hash = canonical_hash(
        {"blocked_reason": "LEASE_HELD_BY_OTHER", "run_id": str(rid)}
    )
    assert blocked[0].output_content_hash == expected_hash


# ---------------------------------------------------------------------------
# 4-7. Resume from each possible last_completed_role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("last_completed_role", "expected_remaining"),
    [
        ("planner", ("executor", "reviewer", "synthesizer")),
        ("executor", ("reviewer", "synthesizer")),
        ("reviewer", ("synthesizer",)),
    ],
)
def test_resume_from_each_role(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
    last_completed_role: Role,
    expected_remaining: tuple[Role, ...],
) -> None:
    """Resume from each interruption point continues the chain correctly.

    Asserts:
    * ``run.status == 'completed'`` after resume.
    * The canned invoker was called ONLY for the remaining roles (not
      re-invoked for already-completed ones).
    * ``checkpoint.json`` advances to ``last_completed_role='synthesizer'``.
    * ``lane_outputs/`` ends with exactly four files (one per role).
    * Hash chain still verifies end-to-end.
    """
    rid = _build_partial_run(
        tmp_lane_store, canned_invoker, last_completed_role=last_completed_role,
    )
    # Reset the canned invoker call log so we measure only the resume call.
    canned_invoker.calls.clear()

    resumed = resume_swarm_lane(
        rid,
        tmp_lane_store,
        tmp_lease_manager,
        holder_id="resume-holder",
        on_role_invoke=canned_invoker,
    )
    assert resumed.status == "completed"
    assert resumed.reason_code is None

    invoked_roles = tuple(c["role"] for c in canned_invoker.calls)
    assert invoked_roles == expected_remaining

    run_dir = tmp_lane_store.run_dir_for(rid)
    cp = tmp_lane_store.load_checkpoint(run_dir)
    assert cp is not None
    assert cp.last_completed_role == "synthesizer"

    output_files = sorted((run_dir / "lane_outputs").glob("*.json"))
    assert len(output_files) == 4
    assert tmp_lane_store.verify_chain(run_dir) is True

    # Lease released cleanly in the resume's `finally` block.
    assert tmp_lane_store.get_lease(rid) is None


def test_resume_from_synthesizer_short_circuits(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """Pre-completion checkpoint at synthesizer -> resume marks completed
    without invoking any role (the chain is already done; the SwarmRun row
    just never got marked).
    """
    rid = _build_partial_run(
        tmp_lane_store, canned_invoker, last_completed_role="synthesizer",
    )
    canned_invoker.calls.clear()

    resumed = resume_swarm_lane(
        rid,
        tmp_lane_store,
        tmp_lease_manager,
        holder_id="resume-holder",
        on_role_invoke=canned_invoker,
    )
    assert resumed.status == "completed"
    assert resumed.reason_code is None
    assert canned_invoker.calls == []


# ---------------------------------------------------------------------------
# 8. No checkpoint.json -> walk lane_outputs/ to infer resume point
# ---------------------------------------------------------------------------


def test_resume_no_checkpoint_walks_lane_outputs(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """When ``checkpoint.json`` is missing, ``_infer_last_completed_role``
    walks ``lane_outputs/`` and infers the resume point from the highest
    role with a persisted output.

    Seed: completed up through executor (planner + executor outputs on
    disk), but no checkpoint.json. Expect resume to call only reviewer +
    synthesizer.
    """
    rid = _build_partial_run(
        tmp_lane_store,
        canned_invoker,
        last_completed_role="executor",
        write_checkpoint=False,
    )
    # Sanity: no checkpoint on disk.
    run_dir = tmp_lane_store.run_dir_for(rid)
    assert not (run_dir / "checkpoint.json").exists()

    canned_invoker.calls.clear()
    resumed = resume_swarm_lane(
        rid,
        tmp_lane_store,
        tmp_lease_manager,
        holder_id="resume-holder",
        on_role_invoke=canned_invoker,
    )
    assert resumed.status == "completed"

    invoked_roles = tuple(c["role"] for c in canned_invoker.calls)
    assert invoked_roles == ("reviewer", "synthesizer")

    # Checkpoint now exists (resume writes one after each completed lane).
    cp = tmp_lane_store.load_checkpoint(run_dir)
    assert cp is not None
    assert cp.last_completed_role == "synthesizer"


# ---------------------------------------------------------------------------
# 9. Unknown run_id -> ValueError("RESUME_NO_CANDIDATE")
# ---------------------------------------------------------------------------


def test_resume_unknown_run_id_raises(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
) -> None:
    """A run_id with no SwarmRun row raises with the literal sentinel."""
    bogus = uuid4()
    with pytest.raises(ValueError, match="RESUME_NO_CANDIDATE"):
        resume_swarm_lane(bogus, tmp_lane_store, tmp_lease_manager)
