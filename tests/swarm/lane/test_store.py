"""test_store.py -- LaneStore SQLite + JSONL + atomic-write coverage.

Phase 66 plan 01 coverage of ``ollarma.swarm.lane.store``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import orjson
import pytest

from ollarma.swarm.lane.schemas import (
    LaneTransition,
    PlannerOutput,
    ReviewerOutput,
    SwarmRun,
)
from ollarma.swarm.lane.store import LaneStore, LaneStoreError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> LaneStore:
    return LaneStore(base_dir=tmp_path / "swarm")


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Construction + run_dir_for
# ---------------------------------------------------------------------------

def test_lane_store_construction_creates_paths(tmp_path: Path) -> None:
    base = tmp_path / "swarm"
    s = LaneStore(base_dir=base)
    assert (base / "runs.sqlite").exists()
    assert (base / "runs").is_dir()
    assert s.base_dir == base.resolve()


def test_lane_store_run_dir_for_creates_outputs_subdir(
    store: LaneStore,
) -> None:
    rid = uuid4()
    rd = store.run_dir_for(rid)
    assert rd.exists()
    assert rd.is_dir()
    assert rd.name == str(rid)
    assert (rd / "lane_outputs").is_dir()


# ---------------------------------------------------------------------------
# SQLite: runs index
# ---------------------------------------------------------------------------

def test_lane_store_upsert_and_get_run(
    store: LaneStore, fixed_now: datetime,
) -> None:
    run = SwarmRun(
        requested_at=fixed_now,
        status="in_progress",
        metadata={"phase": "66"},
    )
    store.upsert_run(run)

    fetched = store.get_run(run.run_id)
    assert fetched is not None
    assert fetched.run_id == run.run_id
    assert fetched.status == "in_progress"
    assert fetched.metadata == {"phase": "66"}
    assert fetched.requested_at == fixed_now

    # JSON snapshot also written.
    snap = store.run_dir_for(run.run_id) / "swarm_run.json"
    assert snap.exists()


def test_lane_store_upsert_run_replaces(
    store: LaneStore, fixed_now: datetime,
) -> None:
    rid = uuid4()
    run_v1 = SwarmRun(
        run_id=rid, requested_at=fixed_now, status="pending",
    )
    store.upsert_run(run_v1)
    run_v2 = SwarmRun(
        run_id=rid, requested_at=fixed_now, status="completed",
    )
    store.upsert_run(run_v2)
    fetched = store.get_run(rid)
    assert fetched is not None
    assert fetched.status == "completed"


def test_lane_store_get_run_missing_returns_none(
    store: LaneStore,
) -> None:
    assert store.get_run(uuid4()) is None


# ---------------------------------------------------------------------------
# SQLite: leases
# ---------------------------------------------------------------------------

def test_lane_store_upsert_lease_atomic(
    store: LaneStore, fixed_now: datetime,
) -> None:
    """Second upsert with a different holder overwrites the first."""
    rid = uuid4()
    lane_a = uuid4()
    lane_b = uuid4()
    store.upsert_lease(rid, lane_a, "holder-a", ttl_seconds=60, now=fixed_now)
    store.upsert_lease(rid, lane_b, "holder-b", ttl_seconds=60, now=fixed_now)

    lease = store.get_lease(rid)
    assert lease is not None
    fetched_lane, fetched_holder, fetched_expires = lease
    assert fetched_lane == lane_b
    assert fetched_holder == "holder-b"
    assert fetched_expires == fixed_now + timedelta(seconds=60)


def test_lane_store_upsert_lease_rejects_nonpositive_ttl(
    store: LaneStore,
) -> None:
    with pytest.raises(LaneStoreError):
        store.upsert_lease(uuid4(), uuid4(), "h", ttl_seconds=0)


def test_lane_store_release_lease_only_owner(
    store: LaneStore, fixed_now: datetime,
) -> None:
    rid = uuid4()
    store.upsert_lease(rid, uuid4(), "alice", ttl_seconds=60, now=fixed_now)
    # Wrong holder -- no row deleted, returns False.
    assert store.release_lease(rid, "mallory") is False
    assert store.get_lease(rid) is not None
    # Right holder -- row deleted, returns True.
    assert store.release_lease(rid, "alice") is True
    assert store.get_lease(rid) is None
    # Releasing again returns False (already gone).
    assert store.release_lease(rid, "alice") is False


def test_lane_store_list_stale_leases(
    store: LaneStore, fixed_now: datetime,
) -> None:
    # Acquire two leases at fixed_now -- one with 10s TTL, one with 600s.
    rid_short = uuid4()
    rid_long = uuid4()
    store.upsert_lease(
        rid_short, uuid4(), "short", ttl_seconds=10, now=fixed_now,
    )
    store.upsert_lease(
        rid_long, uuid4(), "long", ttl_seconds=600, now=fixed_now,
    )
    # Probe at fixed_now + 60s: only the short one is expired.
    probe = fixed_now + timedelta(seconds=60)
    stale = store.list_stale_leases(now=probe)
    assert len(stale) == 1
    stale_rid, _, stale_holder, _ = stale[0]
    assert stale_rid == rid_short
    assert stale_holder == "short"


# ---------------------------------------------------------------------------
# JSONL: transitions chain
# ---------------------------------------------------------------------------

def test_lane_store_append_transition_chains(store: LaneStore) -> None:
    rid = uuid4()
    rd = store.run_dir_for(rid)

    # Three transitions: planner-in, planner->executor, executor->reviewer.
    t1 = LaneTransition(
        run_id=rid, lane_id=uuid4(),
        from_role=None, to_role="planner",
        output_content_hash="a" * 64, parent_hash=None,
    )
    m1 = store.append_transition(t1, rd)
    assert m1.transition_hash != ""
    assert m1.parent_hash is None

    t2 = LaneTransition(
        run_id=rid, lane_id=uuid4(),
        from_role="planner", to_role="executor",
        output_content_hash="b" * 64,
        parent_hash=m1.transition_hash,
    )
    m2 = store.append_transition(t2, rd)
    assert m2.parent_hash == m1.transition_hash

    t3 = LaneTransition(
        run_id=rid, lane_id=uuid4(),
        from_role="executor", to_role="reviewer",
        output_content_hash="c" * 64,
        parent_hash=m2.transition_hash,
    )
    m3 = store.append_transition(t3, rd)
    assert m3.parent_hash == m2.transition_hash

    loaded = store.load_transitions(rd)
    assert [t.transition_hash for t in loaded] == [
        m1.transition_hash, m2.transition_hash, m3.transition_hash,
    ]
    assert store.verify_chain(rd) is True


def test_lane_store_append_transition_rejects_wrong_parent(
    store: LaneStore,
) -> None:
    rid = uuid4()
    rd = store.run_dir_for(rid)
    t1 = LaneTransition(
        run_id=rid, lane_id=uuid4(),
        from_role=None, to_role="planner",
        output_content_hash="a" * 64, parent_hash=None,
    )
    store.append_transition(t1, rd)
    # Second transition with a fabricated parent_hash should be rejected.
    t2_bad = LaneTransition(
        run_id=rid, lane_id=uuid4(),
        from_role="planner", to_role="executor",
        output_content_hash="b" * 64,
        parent_hash="deadbeef" * 8,  # wrong length but unique-bad
    )
    with pytest.raises(LaneStoreError):
        store.append_transition(t2_bad, rd)


def test_lane_store_verify_chain_detects_break(store: LaneStore) -> None:
    rid = uuid4()
    rd = store.run_dir_for(rid)
    t1 = LaneTransition(
        run_id=rid, lane_id=uuid4(),
        from_role=None, to_role="planner",
        output_content_hash="a" * 64, parent_hash=None,
    )
    m1 = store.append_transition(t1, rd)
    t2 = LaneTransition(
        run_id=rid, lane_id=uuid4(),
        from_role="planner", to_role="executor",
        output_content_hash="b" * 64,
        parent_hash=m1.transition_hash,
    )
    store.append_transition(t2, rd)
    assert store.verify_chain(rd) is True

    # Tamper: rewrite the JSONL with a mutated second-line content hash.
    path = rd / "lane_transitions.jsonl"
    raw_lines = path.read_bytes().split(b"\n")
    second = orjson.loads(raw_lines[1])
    second["output_content_hash"] = "z" * 64  # mutate post-write
    raw_lines[1] = orjson.dumps(second, option=orjson.OPT_SORT_KEYS)
    path.write_bytes(b"\n".join(raw_lines))

    assert store.verify_chain(rd) is False


def test_lane_store_load_transitions_empty_for_missing(
    store: LaneStore,
) -> None:
    rd = store.run_dir_for(uuid4())
    assert store.load_transitions(rd) == []
    assert store.verify_chain(rd) is True


# ---------------------------------------------------------------------------
# Atomic JSON: lane outputs
# ---------------------------------------------------------------------------

def test_lane_store_write_lane_output_atomic(
    store: LaneStore,
) -> None:
    rid = uuid4()
    rd = store.run_dir_for(rid)
    out = PlannerOutput(
        run_id=rid, lane_id=uuid4(),
        plan_steps=["a", "b"], rationale="rationale",
    )
    path = store.write_lane_output(out, rd)
    assert path.exists()
    assert path.parent.name == "lane_outputs"
    # No leftover temp siblings.
    leftover = [
        p for p in path.parent.iterdir() if p.name.startswith(path.name + ".tmp")
    ]
    assert leftover == []
    # Round-trip via load_lane_output.
    loaded = store.load_lane_output(rd, out.lane_id)
    assert loaded is not None
    rebuilt = PlannerOutput.model_validate(loaded)
    assert rebuilt == out


def test_lane_store_load_lane_output_missing_returns_none(
    store: LaneStore,
) -> None:
    rd = store.run_dir_for(uuid4())
    assert store.load_lane_output(rd, uuid4()) is None


def test_lane_store_write_multiple_outputs_distinct_files(
    store: LaneStore,
) -> None:
    rid = uuid4()
    rd = store.run_dir_for(rid)
    p = PlannerOutput(
        run_id=rid, lane_id=uuid4(), plan_steps=[], rationale="",
    )
    r = ReviewerOutput(
        run_id=rid, lane_id=uuid4(),
        findings=["x"], severity="warn", summary="s",
    )
    pp = store.write_lane_output(p, rd)
    rp = store.write_lane_output(r, rd)
    assert pp != rp
    assert pp.exists() and rp.exists()


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------

def test_lane_store_restart_recovery(
    tmp_path: Path, fixed_now: datetime,
) -> None:
    base = tmp_path / "swarm"
    s1 = LaneStore(base_dir=base)
    rid = uuid4()
    run = SwarmRun(run_id=rid, requested_at=fixed_now, status="in_progress")
    s1.upsert_run(run)
    s1.upsert_lease(rid, uuid4(), "alice", ttl_seconds=60, now=fixed_now)
    rd = s1.run_dir_for(rid)
    t1 = LaneTransition(
        run_id=rid, lane_id=uuid4(),
        from_role=None, to_role="planner",
        output_content_hash="a" * 64, parent_hash=None,
    )
    m1 = s1.append_transition(t1, rd)
    t2 = LaneTransition(
        run_id=rid, lane_id=uuid4(),
        from_role="planner", to_role="executor",
        output_content_hash="b" * 64,
        parent_hash=m1.transition_hash,
    )
    s1.append_transition(t2, rd)
    s1.close()

    # Reopen at the same base_dir; prior data + chain still readable.
    s2 = LaneStore(base_dir=base)
    fetched = s2.get_run(rid)
    assert fetched is not None
    assert fetched.status == "in_progress"
    lease = s2.get_lease(rid)
    assert lease is not None
    assert lease[1] == "alice"
    rd2 = s2.run_dir_for(rid)
    transitions = s2.load_transitions(rd2)
    assert len(transitions) == 2
    assert s2.verify_chain(rd2) is True


# ---------------------------------------------------------------------------
# WAL mode + close idempotency
# ---------------------------------------------------------------------------

def test_lane_store_close_is_idempotent(store: LaneStore) -> None:
    store.close()
    store.close()  # must not raise


def test_lane_store_uses_wal_mode(store: LaneStore) -> None:
    # WAL mode leaves a -wal sidecar after the first write.
    sr = SwarmRun()
    store.upsert_run(sr)
    wal_path = store.base_dir / "runs.sqlite-wal"
    # Sidecar may be absent if the WAL was checkpointed; instead query the
    # journal_mode pragma to assert directly.
    cur = store._conn.execute("PRAGMA journal_mode")  # type: ignore[attr-defined]
    mode = cur.fetchone()[0]
    assert mode.lower() == "wal", f"expected WAL, got {mode}"
    # Touch the sidecar reference to silence unused-var lint.
    _ = wal_path
