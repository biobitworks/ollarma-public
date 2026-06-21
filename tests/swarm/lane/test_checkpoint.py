"""test_checkpoint.py -- Phase 67 plan 01 coverage.

Covers:

* :class:`Checkpoint` and :class:`ResumeCandidate` schema construction.
* :meth:`LaneStore.write_checkpoint` -> :meth:`load_checkpoint` round-trip.
* Atomic write contract (no half-written file under temp+rename).
* :meth:`LaneStore.load_checkpoint` returns ``None`` for missing file.
* :meth:`LaneStore.list_resumable_runs` filter:
    - includes status='in_progress' + lease-expired runs
    - excludes status='completed' (regardless of lease state)
    - excludes status='in_progress' + lease-not-yet-expired runs
    - sorted by lease_expired_at ascending (oldest first)
    - last_completed_role plumbed from on-disk checkpoint.json
    - last_completed_role=None when checkpoint.json missing
* Re-export sanity: ``from ollarma.swarm.lane import Checkpoint, ResumeCandidate``.
* ReasonCode additions: TOKEN_BUDGET_EXCEEDED, RESUME_NO_CANDIDATE.
* _runtime_contract additions: DEFAULT_LANE_TOKEN_BUDGET, SWAP_DEGRADED_PCT_THRESHOLD.

No Ollama, no real I/O beyond pytest tmp_path. Runtime budget < 1s.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import orjson
import pytest

from ollarma.escalation import ReasonCode
from ollarma.swarm._runtime_contract import (
    DEFAULT_LANE_TOKEN_BUDGET,
    SWAP_DEGRADED_PCT_THRESHOLD,
)
from ollarma.swarm.lane import Checkpoint, ResumeCandidate
from ollarma.swarm.lane.schemas import SwarmRun
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
# Schema sanity
# ---------------------------------------------------------------------------

def test_checkpoint_schema_defaults_and_freeze() -> None:
    rid = uuid4()
    ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
    cp = Checkpoint(
        run_id=rid,
        last_completed_role="executor",
        holder_id="agent-1",
        last_checkpoint_at=ts,
    )
    assert cp.schema_version == 1
    assert cp.last_completed_role == "executor"
    # frozen
    with pytest.raises(Exception):
        cp.holder_id = "other"  # type: ignore[misc]


def test_checkpoint_allows_none_role() -> None:
    """``last_completed_role=None`` means 'no lane has completed yet'."""
    cp = Checkpoint(
        run_id=uuid4(),
        last_completed_role=None,
        holder_id="agent-1",
        last_checkpoint_at=datetime.now(timezone.utc),
    )
    assert cp.last_completed_role is None


def test_resume_candidate_schema_defaults() -> None:
    rc = ResumeCandidate(
        run_id=uuid4(),
        last_completed_role="planner",
        prior_holder_id="agent-x",
        lease_expired_at=datetime(2026, 5, 6, 11, 0, 0, tzinfo=timezone.utc),
        candidate_age_seconds=3600.0,
    )
    assert rc.schema_version == 1
    assert rc.candidate_age_seconds == 3600.0


# ---------------------------------------------------------------------------
# Round-trip + atomic write
# ---------------------------------------------------------------------------

def test_checkpoint_round_trip(store: LaneStore, fixed_now: datetime) -> None:
    rid = uuid4()
    run_dir = store.run_dir_for(rid)
    cp = Checkpoint(
        run_id=rid,
        last_completed_role="reviewer",
        holder_id="agent-1",
        last_checkpoint_at=fixed_now,
    )
    written = store.write_checkpoint(cp, run_dir)
    assert written.exists()
    assert written.name == "checkpoint.json"

    loaded = store.load_checkpoint(run_dir)
    assert loaded is not None
    assert loaded.run_id == rid
    assert loaded.last_completed_role == "reviewer"
    assert loaded.holder_id == "agent-1"
    assert loaded.last_checkpoint_at == fixed_now
    assert loaded.schema_version == 1


def test_checkpoint_overwrite_is_atomic(
    store: LaneStore, fixed_now: datetime,
) -> None:
    """Overwriting a checkpoint never leaves a half-written file behind."""
    rid = uuid4()
    run_dir = store.run_dir_for(rid)

    cp_v1 = Checkpoint(
        run_id=rid,
        last_completed_role="planner",
        holder_id="agent-1",
        last_checkpoint_at=fixed_now,
    )
    store.write_checkpoint(cp_v1, run_dir)

    cp_v2 = Checkpoint(
        run_id=rid,
        last_completed_role="synthesizer",
        holder_id="agent-2",
        last_checkpoint_at=fixed_now + timedelta(seconds=5),
    )
    store.write_checkpoint(cp_v2, run_dir)

    # No leftover .tmp.* siblings (atomic-write contract).
    siblings = list(run_dir.glob("checkpoint.json.tmp.*"))
    assert siblings == []

    loaded = store.load_checkpoint(run_dir)
    assert loaded is not None
    assert loaded.last_completed_role == "synthesizer"
    assert loaded.holder_id == "agent-2"


def test_load_checkpoint_missing_returns_none(store: LaneStore) -> None:
    rid = uuid4()
    run_dir = store.run_dir_for(rid)
    assert store.load_checkpoint(run_dir) is None


def test_load_checkpoint_corrupted_raises(
    store: LaneStore, fixed_now: datetime,
) -> None:
    rid = uuid4()
    run_dir = store.run_dir_for(rid)
    (run_dir / "checkpoint.json").write_bytes(b"{not json")
    with pytest.raises(LaneStoreError):
        store.load_checkpoint(run_dir)


def test_load_checkpoint_schema_violation_raises(
    store: LaneStore,
) -> None:
    rid = uuid4()
    run_dir = store.run_dir_for(rid)
    # Valid JSON but missing required fields.
    (run_dir / "checkpoint.json").write_bytes(
        orjson.dumps({"run_id": str(rid)})
    )
    with pytest.raises(LaneStoreError):
        store.load_checkpoint(run_dir)


# ---------------------------------------------------------------------------
# list_resumable_runs filter
# ---------------------------------------------------------------------------

def _seed_run_with_lease(
    store: LaneStore,
    *,
    status: str,
    lease_expires_at: datetime,
    holder_id: str,
    requested_at: datetime,
    last_completed_role: str | None = None,
) -> UUID:
    """Helper: seed a SwarmRun + a lease row + (optionally) a checkpoint."""
    run = SwarmRun(
        requested_at=requested_at,
        status=status,  # type: ignore[arg-type]
    )
    store.upsert_run(run)
    # Force the run row's status (upsert above writes the model's status,
    # but model_validate may not accept arbitrary strings -- use the literal).
    # Use a fresh SwarmRun built with the requested status; pydantic Literal
    # validation ensures only valid statuses pass.
    rid = run.run_id

    # Insert a lease row directly via upsert_lease then mutate expires_at via
    # SQL to land in the past (we can't pass a negative ttl). Easier: write
    # via raw SQL.
    from ollarma.swarm.lane.store import _isoformat  # type: ignore[attr-defined]
    with store._lock:  # type: ignore[attr-defined]
        store._conn.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO leases (run_id, lane_id, holder_id,
                                acquired_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                lane_id = excluded.lane_id,
                holder_id = excluded.holder_id,
                acquired_at = excluded.acquired_at,
                expires_at = excluded.expires_at
            """,
            (
                str(rid),
                str(uuid4()),
                holder_id,
                _isoformat(lease_expires_at - timedelta(seconds=60)),
                _isoformat(lease_expires_at),
            ),
        )

    if last_completed_role is not None:
        store.write_checkpoint(
            Checkpoint(
                run_id=rid,
                last_completed_role=last_completed_role,  # type: ignore[arg-type]
                holder_id=holder_id,
                last_checkpoint_at=lease_expires_at,
            ),
            store.run_dir_for(rid),
        )
    return rid


def test_list_resumable_runs_empty(store: LaneStore, fixed_now: datetime) -> None:
    assert store.list_resumable_runs(now=fixed_now) == []


def test_list_resumable_runs_includes_in_progress_and_expired(
    store: LaneStore, fixed_now: datetime,
) -> None:
    rid = _seed_run_with_lease(
        store,
        status="in_progress",
        lease_expires_at=fixed_now - timedelta(seconds=30),
        holder_id="agent-A",
        requested_at=fixed_now - timedelta(seconds=300),
        last_completed_role="executor",
    )
    candidates = store.list_resumable_runs(now=fixed_now)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.run_id == rid
    assert c.prior_holder_id == "agent-A"
    assert c.last_completed_role == "executor"
    assert c.candidate_age_seconds == pytest.approx(30.0, abs=0.5)


def test_list_resumable_runs_excludes_completed(
    store: LaneStore, fixed_now: datetime,
) -> None:
    """status='completed' must NOT appear, even if lease is expired."""
    _seed_run_with_lease(
        store,
        status="completed",
        lease_expires_at=fixed_now - timedelta(seconds=30),
        holder_id="agent-A",
        requested_at=fixed_now - timedelta(seconds=300),
    )
    assert store.list_resumable_runs(now=fixed_now) == []


def test_list_resumable_runs_excludes_lease_not_expired(
    store: LaneStore, fixed_now: datetime,
) -> None:
    """status='in_progress' but lease still valid -> not resumable."""
    _seed_run_with_lease(
        store,
        status="in_progress",
        lease_expires_at=fixed_now + timedelta(seconds=30),  # future
        holder_id="agent-A",
        requested_at=fixed_now - timedelta(seconds=60),
    )
    assert store.list_resumable_runs(now=fixed_now) == []


def test_list_resumable_runs_sorted_by_lease_expired_at_asc(
    store: LaneStore, fixed_now: datetime,
) -> None:
    rid_old = _seed_run_with_lease(
        store,
        status="in_progress",
        lease_expires_at=fixed_now - timedelta(seconds=300),
        holder_id="agent-old",
        requested_at=fixed_now - timedelta(seconds=600),
    )
    rid_recent = _seed_run_with_lease(
        store,
        status="in_progress",
        lease_expires_at=fixed_now - timedelta(seconds=30),
        holder_id="agent-recent",
        requested_at=fixed_now - timedelta(seconds=120),
    )
    candidates = store.list_resumable_runs(now=fixed_now)
    assert [c.run_id for c in candidates] == [rid_old, rid_recent]


def test_list_resumable_runs_missing_checkpoint_role_is_none(
    store: LaneStore, fixed_now: datetime,
) -> None:
    rid = _seed_run_with_lease(
        store,
        status="in_progress",
        lease_expires_at=fixed_now - timedelta(seconds=30),
        holder_id="agent-A",
        requested_at=fixed_now - timedelta(seconds=120),
        last_completed_role=None,  # no checkpoint.json written
    )
    candidates = store.list_resumable_runs(now=fixed_now)
    assert len(candidates) == 1
    assert candidates[0].run_id == rid
    assert candidates[0].last_completed_role is None


def test_list_resumable_runs_corrupted_checkpoint_yields_none_role(
    store: LaneStore, fixed_now: datetime,
) -> None:
    """Corrupt checkpoint.json shouldn't hide a resumable run from operators."""
    rid = _seed_run_with_lease(
        store,
        status="in_progress",
        lease_expires_at=fixed_now - timedelta(seconds=30),
        holder_id="agent-A",
        requested_at=fixed_now - timedelta(seconds=120),
    )
    # Now drop a corrupt checkpoint.
    (store.run_dir_for(rid) / "checkpoint.json").write_bytes(b"{garbage")

    candidates = store.list_resumable_runs(now=fixed_now)
    assert len(candidates) == 1
    assert candidates[0].run_id == rid
    assert candidates[0].last_completed_role is None


def test_list_resumable_runs_default_now_uses_wall_clock(
    store: LaneStore,
) -> None:
    """Calling with ``now=None`` uses ``datetime.now(timezone.utc)``."""
    past = datetime.now(timezone.utc) - timedelta(seconds=120)
    rid = _seed_run_with_lease(
        store,
        status="in_progress",
        lease_expires_at=past,
        holder_id="agent-A",
        requested_at=past - timedelta(seconds=60),
    )
    candidates = store.list_resumable_runs()  # no now=
    assert len(candidates) == 1
    assert candidates[0].run_id == rid


# ---------------------------------------------------------------------------
# Cross-cutting constants + ReasonCode
# ---------------------------------------------------------------------------

def test_runtime_contract_constants_present() -> None:
    assert DEFAULT_LANE_TOKEN_BUDGET == 4096
    assert SWAP_DEGRADED_PCT_THRESHOLD == 50


def test_reason_code_additions_present() -> None:
    assert ReasonCode.TOKEN_BUDGET_EXCEEDED.value == "TOKEN_BUDGET_EXCEEDED"
    assert ReasonCode.RESUME_NO_CANDIDATE.value == "RESUME_NO_CANDIDATE"
    # SWAP_DEGRADED already existed (Phase 18); confirm not re-broken.
    assert ReasonCode.SWAP_DEGRADED.value == "SWAP_DEGRADED"
