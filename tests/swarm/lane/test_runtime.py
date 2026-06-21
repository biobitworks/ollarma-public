"""test_runtime.py -- ``run_swarm_lane`` integration coverage (Phase 66 plan 03).

Exercises the orchestrator's six observable paths with stub invokers (no
Ollama). Per Wave 2 deviations:

* runtime.py emits **4** transitions per happy-path run (one per role
  completion), with the first having ``from_role=None``.
* ``SwarmRun.metadata`` always contains an auto-injected ``"scenario"`` key.
* BLOCKED transitions use a sentinel zero-UUID lane_id and an
  ``output_content_hash`` derived from ``{"blocked_reason", "run_id"}``.
"""
from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import orjson

from ollarma.evidence import canonical_hash
from ollarma.swarm.lane import (
    LaneStore,
    LeaseManager,
    run_swarm_lane,
)
from ollarma.swarm.lane.runtime import _BLOCKED_LANE_ID


# ---------------------------------------------------------------------------
# Happy path: planner -> executor -> reviewer -> synthesizer
# ---------------------------------------------------------------------------

def test_runtime_happy_path(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """End-to-end happy path with a canned invoker.

    Asserts:
    * ``status == "completed"``, ``reason_code is None``
    * ``metadata["scenario"]`` is set (auto-injected by runtime)
    * exactly 4 transitions persisted (one per role completion)
    * first transition has ``from_role=None`` (planner-in)
    * last transition has ``to_role="synthesizer"``
    * ``store.verify_chain`` returns True
    """
    rid = uuid4()
    run = run_swarm_lane(
        scenario="happy-path-smoke",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=canned_invoker,
    )

    assert run.status == "completed"
    assert run.reason_code is None
    assert run.metadata["scenario"] == "happy-path-smoke"

    run_dir = tmp_lane_store.run_dir_for(rid)
    transitions = tmp_lane_store.load_transitions(run_dir)
    assert len(transitions) == 4

    # Sequence: None -> planner, planner -> executor, executor -> reviewer,
    # reviewer -> synthesizer.
    assert transitions[0].from_role is None
    assert transitions[0].to_role == "planner"
    assert transitions[-1].to_role == "synthesizer"
    # Chain verifies.
    assert tmp_lane_store.verify_chain(run_dir) is True


def test_runtime_lane_outputs_persisted(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """After a happy-path run, lane_outputs/ has 4 distinct files."""
    rid = uuid4()
    run_swarm_lane(
        scenario="outputs-persist-test",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=canned_invoker,
    )

    run_dir = tmp_lane_store.run_dir_for(rid)
    outputs_dir = run_dir / "lane_outputs"
    json_files = sorted(outputs_dir.glob("*.json"))
    assert len(json_files) == 4

    # Each file should be valid JSON with a recognized role and the
    # matching run_id.
    seen_roles: set[str] = set()
    for path in json_files:
        record = orjson.loads(path.read_bytes())
        assert record["run_id"] == str(rid)
        seen_roles.add(record["role"])
    assert seen_roles == {"planner", "executor", "reviewer", "synthesizer"}


def test_runtime_full_upstream_visibility(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """Each role sees all prior outputs (planner=0, executor=1, reviewer=2, synthesizer=3)."""
    rid = uuid4()
    run_swarm_lane(
        scenario="upstream-visibility",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        on_role_invoke=canned_invoker,
    )

    calls = canned_invoker.calls
    assert len(calls) == 4
    by_role = {c["role"]: c["upstream_count"] for c in calls}
    assert by_role == {
        "planner": 0,
        "executor": 1,
        "reviewer": 2,
        "synthesizer": 3,
    }


# ---------------------------------------------------------------------------
# Lease-held: another holder owns the lease before we try to acquire
# ---------------------------------------------------------------------------

def test_runtime_lease_held_by_other_blocks(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """If alice holds the lease, bob's run_swarm_lane returns blocked."""
    rid = uuid4()
    # Alice grabs the lease first.
    alice_decision = tmp_lease_manager.acquire(
        rid, holder_id="alice", ttl_seconds=60,
    )
    assert alice_decision.acquired is True

    # Bob tries to run; should be blocked, single BLOCKED transition emitted.
    run = run_swarm_lane(
        scenario="lease-blocked",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        holder_id="bob",
        on_role_invoke=canned_invoker,
    )

    assert run.status == "blocked"
    assert run.reason_code == "LEASE_HELD_BY_OTHER"

    # Canned invoker should NOT have been called -- we never started a lane.
    assert canned_invoker.calls == []

    # Receipt-chain integrity: a single BLOCKED transition, with the sentinel
    # zero-lane_id and a content hash derived from blocked_reason+run_id.
    run_dir = tmp_lane_store.run_dir_for(rid)
    transitions = tmp_lane_store.load_transitions(run_dir)
    assert len(transitions) == 1
    blocked = transitions[0]
    assert blocked.lane_id == _BLOCKED_LANE_ID
    assert blocked.from_role is None
    assert blocked.to_role is None
    expected_hash = canonical_hash(
        {"blocked_reason": "LEASE_HELD_BY_OTHER", "run_id": str(rid)}
    )
    assert blocked.output_content_hash == expected_hash
    assert blocked.parent_hash is None


# ---------------------------------------------------------------------------
# Lease expires mid-run: TTL fires before the executor lane
# ---------------------------------------------------------------------------

def test_runtime_lease_expires_mid_run(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    canned_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """Lease TTL fires mid-run -> status='failed', reason_code='LEASE_EXPIRED'.

    Strategy: run with a TTL=1s lease and an invoker whose first call (planner)
    sleeps long enough for the lease to expire before the runtime checks it
    again at the top of the executor lane. Total wall: ~1.2s.
    """
    rid = uuid4()

    def _slow_planner_invoker(role, lane_run):  # type: ignore[no-untyped-def]
        # Delegate to canned for everything, but the planner sleeps long
        # enough for the TTL=1s lease to expire before the runtime's
        # next-lane lease check.
        if role == "planner":
            time.sleep(1.2)
        return canned_invoker(role, lane_run)

    run = run_swarm_lane(
        scenario="lease-expires-mid-run",
        store=tmp_lane_store,
        lease_manager=tmp_lease_manager,
        run_id=rid,
        ttl_seconds=1,
        on_role_invoke=_slow_planner_invoker,
    )

    assert run.status == "failed"
    assert run.reason_code == "LEASE_EXPIRED"

    # Receipt chain: planner transition + BLOCKED transition.
    run_dir = tmp_lane_store.run_dir_for(rid)
    transitions = tmp_lane_store.load_transitions(run_dir)
    # At minimum the BLOCKED transition is present; the planner transition
    # may or may not have been written depending on exactly when the TTL
    # fired. Validate that a BLOCKED transition exists with the right
    # content_hash.
    blocked = [
        t for t in transitions if t.lane_id == _BLOCKED_LANE_ID
    ]
    assert len(blocked) == 1
    expected_hash = canonical_hash(
        {"blocked_reason": "LEASE_EXPIRED", "run_id": str(rid)}
    )
    assert blocked[0].output_content_hash == expected_hash
    # And the chain still verifies overall.
    assert tmp_lane_store.verify_chain(run_dir) is True


# ---------------------------------------------------------------------------
# Role exception: 1 retry then quarantine + LANE_FAILED
# ---------------------------------------------------------------------------

def test_runtime_role_exception_quarantines(
    tmp_lane_store: LaneStore,
    tmp_lease_manager: LeaseManager,
    raising_invoker,  # type: ignore[no-untyped-def]
) -> None:
    """Executor raises twice -> quarantine.jsonl entry + status='failed'.

    Asserts:
    * ``status == "failed"``, ``reason_code == "LANE_FAILED"``
    * ``quarantine.jsonl`` contains exactly one record naming the executor
    * planner transition was persisted before the quarantine
    * a final BLOCKED transition closes the chain
    """
    rid = uuid4()
    run = run_swarm_lane(
        scenario="role-exception-quarantines",
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
    assert record["role"] == "executor"
    assert record["exception_type"] == "RuntimeError"
    assert record["run_id"] == str(rid)

    # Planner transition + BLOCKED transition; no executor transition.
    transitions = tmp_lane_store.load_transitions(run_dir)
    role_transitions = [
        t for t in transitions if t.lane_id != _BLOCKED_LANE_ID
    ]
    blocked_transitions = [
        t for t in transitions if t.lane_id == _BLOCKED_LANE_ID
    ]
    assert len(role_transitions) == 1
    assert role_transitions[0].to_role == "planner"
    assert len(blocked_transitions) == 1
    expected_hash = canonical_hash(
        {"blocked_reason": "LANE_FAILED", "run_id": str(rid)}
    )
    assert blocked_transitions[0].output_content_hash == expected_hash
    assert tmp_lane_store.verify_chain(run_dir) is True

    # Invoker call log: planner once + executor twice (initial + 1 retry).
    invoker_calls = raising_invoker.calls
    role_call_counts: dict[str, int] = {}
    for c in invoker_calls:
        role_call_counts[c["role"]] = role_call_counts.get(c["role"], 0) + 1
    assert role_call_counts == {"planner": 1, "executor": 2}
