"""test_token_loss.py -- Phase 67 token-loss + checkpoint-emission coverage.

Covers the four observable token-loss / checkpoint behaviors as shipped in
Wave 2 (``a3373a2``):

1. ``TokenExhaustedError`` raised on the FIRST attempt of the executor lane:
   the runtime quarantines the partial with ``cause="token_exhausted"`` and
   restarts the lane ONCE with a fresh ``lane_id``. The restart succeeds and
   the run completes.
2. ``TokenExhaustedError`` raised on BOTH attempts of the executor lane:
   the runtime quarantines twice, then ends the SwarmRun as ``failed`` with
   ``reason_code="TOKEN_BUDGET_EXCEEDED"`` and emits a BLOCKED transition.
3. A generic ``RuntimeError`` raised by the executor still routes through
   the Phase 66 1-retry-then-quarantine path -- ``status="failed"``,
   ``reason_code="LANE_FAILED"``, quarantine record carries the default
   ``cause="lane_failed"`` (NOT ``"token_exhausted"``). This pins the
   Wave-2 ``cause`` kwarg behavior so a future refactor cannot accidentally
   collapse the two failure classes into one bucket.
4. ``checkpoint.json`` is updated EAGERLY after every successful lane (not
   only at the end). Verified by an instrumented invoker that, before
   producing role N's output, reads the on-disk checkpoint and asserts its
   ``last_completed_role`` matches role N-1 (or is ``None`` for the
   planner). This is the resume-correctness guarantee Phase 67 ships on.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import orjson

from ollarma.swarm.lane import (
    LaneStore,
    LeaseManager,
    TokenExhaustedError,
    run_swarm_lane,
)
from ollarma.swarm.lane.runtime import _BLOCKED_LANE_ID
from ollarma.swarm.lane.schemas import (
    ExecutorOutput,
    PlannerOutput,
    ReviewerOutput,
    ROLE_CHAIN,
    Role,
    SynthesizerOutput,
)


# ---------------------------------------------------------------------------
# 1. TokenExhaustedError on first attempt -> quarantine + restart -> completes
# ---------------------------------------------------------------------------


def test_token_exhausted_first_attempt_restarts(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    token_exhausting_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """Single TE on executor -> 1 quarantine entry + restart -> completed.

    Expectations (per Wave 2 deviation: ``_quarantine`` accepts ``cause``;
    token-loss path passes ``cause="token_exhausted"``):

    * Final SwarmRun status="completed", reason_code is None.
    * ``quarantine.jsonl`` has exactly 1 line, with ``cause="token_exhausted"``
      and ``role="executor"``.
    * The successful executor lane writes its own output -- the failed
      attempt does NOT (we only persist successful lane outputs). Total
      ``lane_outputs/`` files = 4 (one per role).
    * The successful executor lane has a DIFFERENT ``lane_id`` from the
      failed one -- the restart uses a fresh UUID. We assert this by
      reading both lane_ids from the invoker's call log.
    """
    invoker = token_exhausting_invoker(
        raise_on_role="executor", raise_count=1,
    )
    rid = uuid4()
    run = run_swarm_lane(
        scenario="token-exhausted-first-attempt",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=invoker,
    )

    assert run.status == "completed"
    assert run.reason_code is None

    run_dir = tmp_lane_store.run_dir_for(rid)

    # Quarantine: exactly one record, cause="token_exhausted", role="executor".
    quarantine_path = run_dir / "quarantine.jsonl"
    assert quarantine_path.exists()
    lines = [
        line for line in quarantine_path.read_bytes().split(b"\n") if line
    ]
    assert len(lines) == 1
    record = orjson.loads(lines[0])
    assert record["cause"] == "token_exhausted"
    assert record["role"] == "executor"
    assert record["exception_type"] == "TokenExhaustedError"
    assert record["run_id"] == str(rid)

    # Successful lane outputs: 4 files (one per role). Failed attempts are
    # NOT persisted to lane_outputs/.
    output_files = sorted((run_dir / "lane_outputs").glob("*.json"))
    assert len(output_files) == 4

    # Two distinct executor lane_ids in the invoker call log -- the restart
    # really did get a fresh lane_id.
    executor_calls = [c for c in invoker.calls if c["role"] == "executor"]
    assert len(executor_calls) == 2
    assert executor_calls[0]["lane_id"] != executor_calls[1]["lane_id"]

    # Quarantine line names the FAILED lane_id (the one that raised), not
    # the successful restart's lane_id.
    assert record["lane_id"] == str(executor_calls[0]["lane_id"])

    # Hash chain still verifies (4 successful transitions + 0 BLOCKED).
    transitions = tmp_lane_store.load_transitions(run_dir)
    role_transitions = [t for t in transitions if t.lane_id != _BLOCKED_LANE_ID]
    blocked_transitions = [t for t in transitions if t.lane_id == _BLOCKED_LANE_ID]
    assert len(role_transitions) == 4
    assert len(blocked_transitions) == 0
    assert tmp_lane_store.verify_chain(run_dir) is True


# ---------------------------------------------------------------------------
# 2. TokenExhaustedError on both attempts -> failed + TOKEN_BUDGET_EXCEEDED
# ---------------------------------------------------------------------------


def test_token_exhausted_both_attempts_fails(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    token_exhausting_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """TE on both attempts -> SwarmRun.failed + TOKEN_BUDGET_EXCEEDED.

    Expectations:

    * Final SwarmRun status="failed", reason_code="TOKEN_BUDGET_EXCEEDED".
    * ``quarantine.jsonl`` has 2 lines, both with ``cause="token_exhausted"``.
    * BLOCKED transition emitted with output_content_hash derived from
      ``{"blocked_reason": "TOKEN_BUDGET_EXCEEDED", "run_id": ...}``.
    * Hash chain still verifies (planner transition + BLOCKED transition).
    * Reviewer + synthesizer never invoked.
    """
    invoker = token_exhausting_invoker(
        raise_on_role="executor", raise_count=2,
    )
    rid = uuid4()
    run = run_swarm_lane(
        scenario="token-exhausted-both-attempts",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=invoker,
    )

    assert run.status == "failed"
    assert run.reason_code == "TOKEN_BUDGET_EXCEEDED"

    run_dir = tmp_lane_store.run_dir_for(rid)
    quarantine_path = run_dir / "quarantine.jsonl"
    assert quarantine_path.exists()
    lines = [
        line for line in quarantine_path.read_bytes().split(b"\n") if line
    ]
    assert len(lines) == 2
    causes = [orjson.loads(line)["cause"] for line in lines]
    assert causes == ["token_exhausted", "token_exhausted"]
    roles = [orjson.loads(line)["role"] for line in lines]
    assert roles == ["executor", "executor"]

    # Reviewer + synthesizer never invoked.
    invoked_roles = {c["role"] for c in invoker.calls}
    assert invoked_roles == {"planner", "executor"}

    # BLOCKED transition with the right reason hash.
    transitions = tmp_lane_store.load_transitions(run_dir)
    blocked = [t for t in transitions if t.lane_id == _BLOCKED_LANE_ID]
    assert len(blocked) == 1
    from ollarma.evidence import canonical_hash
    expected_hash = canonical_hash(
        {"blocked_reason": "TOKEN_BUDGET_EXCEEDED", "run_id": str(rid)}
    )
    assert blocked[0].output_content_hash == expected_hash

    # Chain integrity preserved.
    assert tmp_lane_store.verify_chain(run_dir) is True


# ---------------------------------------------------------------------------
# 3. Generic RuntimeError still routes through LANE_FAILED path
# ---------------------------------------------------------------------------


def test_generic_exception_still_uses_lane_failed_path(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    raising_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """Non-TE exception -> Phase 66 LANE_FAILED path (cause='lane_failed').

    This is the contract pin that prevents a future refactor from
    collapsing the two failure modes. Wave 2's ``_quarantine`` defaults
    ``cause="lane_failed"``; only the token-loss path passes
    ``cause="token_exhausted"``. Operators relying on
    ``cause`` for post-mortem split MUST be able to depend on this.
    """
    rid = uuid4()
    run = run_swarm_lane(
        scenario="generic-exception-lane-failed",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=raising_invoker,
    )

    assert run.status == "failed"
    assert run.reason_code == "LANE_FAILED"

    run_dir = tmp_lane_store.run_dir_for(rid)
    quarantine_path = run_dir / "quarantine.jsonl"
    assert quarantine_path.exists()
    lines = [
        line for line in quarantine_path.read_bytes().split(b"\n") if line
    ]
    assert len(lines) == 1
    record = orjson.loads(lines[0])
    # Wave 2 deviation pin: default cause is "lane_failed", NOT "token_exhausted".
    assert record["cause"] == "lane_failed"
    assert record["role"] == "executor"
    assert record["exception_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# 4. Checkpoint emitted after each lane completion
# ---------------------------------------------------------------------------


def test_checkpoint_emitted_after_each_lane(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
) -> None:
    """``checkpoint.json`` advances after EVERY lane, not only at the end.

    Strategy: build an instrumented invoker that, before producing role N's
    output, reads ``<run_dir>/checkpoint.json`` and asserts its
    ``last_completed_role`` matches role N-1 (or is ``None`` for the
    planner -- i.e., no file exists yet at the time the planner is
    invoked). The asserted-against state is captured per role for
    post-run inspection.

    This is the load-bearing guarantee for the Phase 67 resume orchestrator
    -- if the checkpoint were only written at the end of a happy-path run,
    a crash mid-chain would leave resume with no signal at all.
    """
    rid = uuid4()
    run_dir = tmp_lane_store.run_dir_for(rid)

    # Map role -> what the checkpoint SHOULD say at the moment that role is
    # invoked. ``None`` means the file should not yet exist (true only at
    # planner time).
    expected_pre_role: dict[Role, Role | None] = {
        "planner": None,
        "executor": "planner",
        "reviewer": "executor",
        "synthesizer": "reviewer",
    }
    observed_pre_role: dict[Role, Role | None] = {}

    def _instrumented(role, lane_run):  # type: ignore[no-untyped-def]
        # Inspect the checkpoint as it stands BEFORE this role runs.
        cp = tmp_lane_store.load_checkpoint(run_dir)
        observed_pre_role[role] = cp.last_completed_role if cp is not None else None

        if role == "planner":
            return PlannerOutput(
                run_id=lane_run.run_id,
                lane_id=lane_run.lane_id,
                plan_steps=["go"],
                rationale="ok",
            )
        if role == "executor":
            return ExecutorOutput(
                run_id=lane_run.run_id,
                lane_id=lane_run.lane_id,
                actions_taken=["did"],
                artifacts_written=[],
                success=True,
            )
        if role == "reviewer":
            return ReviewerOutput(
                run_id=lane_run.run_id,
                lane_id=lane_run.lane_id,
                findings=[],
                severity="pass",
                summary="ok",
            )
        if role == "synthesizer":
            return SynthesizerOutput(
                run_id=lane_run.run_id,
                lane_id=lane_run.lane_id,
                final_summary="ok",
                decisions=[],
                next_actions=[],
            )
        raise ValueError(f"unknown role: {role!r}")

    run = run_swarm_lane(
        scenario="checkpoint-eager-emission",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=_instrumented,
    )
    assert run.status == "completed"

    # Every role saw the expected pre-state of the checkpoint.
    for role in ROLE_CHAIN:
        assert role in observed_pre_role, f"role {role!r} never invoked"
        assert observed_pre_role[role] == expected_pre_role[role], (
            f"role {role!r}: expected pre-checkpoint last_completed_role="
            f"{expected_pre_role[role]!r}, got {observed_pre_role[role]!r}"
        )

    # Final state on disk: synthesizer.
    final_cp = tmp_lane_store.load_checkpoint(run_dir)
    assert final_cp is not None
    assert final_cp.last_completed_role == "synthesizer"
