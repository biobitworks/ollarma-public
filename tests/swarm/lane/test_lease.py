"""test_lease.py -- LeaseManager unit-test coverage (Phase 66 plan 03).

Covers the four observable ``LeaseDecision.reason`` outcomes (``acquired``,
``refreshed``, ``held_by_other``, ``expired_reclaimed``), ``refresh`` /
``release`` ownership semantics, ``scan_stale``, and a concurrent two-thread
``acquire`` race that must produce exactly one winner.

Timing strategy
---------------
Two tests use a real ``time.sleep(1.1)`` to advance the wall clock past a
TTL=1s lease. We chose this over monkeypatching ``datetime.now`` because the
production code reads the wall clock from BOTH ``LeaseManager`` (in
``acquire``/``refresh``) and ``LaneStore.upsert_lease``; patching only one
gives an inconsistent virtual clock and risks false-passes. ~2.2s of real
wall in the lane test subdir is acceptable per the plan budget (~ 5s).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from ollarma.swarm.lane import LaneStore, LeaseManager


# ---------------------------------------------------------------------------
# Single-holder cases
# ---------------------------------------------------------------------------

def test_lease_acquire_first_call_succeeds(
    tmp_lease_manager: LeaseManager,
) -> None:
    """Fresh run_id, no prior holder -> acquired with reason='acquired'."""
    rid = uuid4()
    decision = tmp_lease_manager.acquire(rid, holder_id="alice", ttl_seconds=60)
    assert decision.acquired is True
    assert decision.holder_id == "alice"
    assert decision.reason == "acquired"
    assert decision.expires_at is not None


def test_lease_acquire_same_holder_is_idempotent(
    tmp_lease_manager: LeaseManager,
) -> None:
    """Re-acquire by the same holder bumps TTL and returns reason='refreshed'.

    Note (Wave 2 deviation): plan said reason="acquired" for second call by
    same holder; as-shipped lease.py returns reason="refreshed" to honestly
    distinguish refresh-by-self from cold-acquire.
    """
    rid = uuid4()
    first = tmp_lease_manager.acquire(rid, holder_id="alice", ttl_seconds=60)
    assert first.acquired is True
    assert first.reason == "acquired"

    second = tmp_lease_manager.acquire(rid, holder_id="alice", ttl_seconds=60)
    assert second.acquired is True
    assert second.reason == "refreshed"
    # Refresh should produce an expires_at >= the first one.
    assert second.expires_at is not None and first.expires_at is not None
    assert second.expires_at >= first.expires_at


def test_lease_acquire_different_holder_blocks(
    tmp_lease_manager: LeaseManager,
) -> None:
    """Second holder during an unexpired lease -> acquired=False, held_by_other."""
    rid = uuid4()
    tmp_lease_manager.acquire(rid, holder_id="alice", ttl_seconds=60)

    decision = tmp_lease_manager.acquire(rid, holder_id="bob", ttl_seconds=60)
    assert decision.acquired is False
    assert decision.holder_id == "alice"
    assert decision.reason == "held_by_other"


# ---------------------------------------------------------------------------
# Concurrency: two threads racing for the same lease
# ---------------------------------------------------------------------------

def test_lease_acquire_atomic_two_holders(
    tmp_lease_manager: LeaseManager,
) -> None:
    """Two different holders racing on the same run_id: exactly one wins.

    The ``LaneStore`` contract (store.py module docstring) is single-store
    in-process atomicity via the per-instance ``threading.Lock``; cross-
    process concurrency is explicitly NOT supported in v5.1. So the race
    here uses ONE shared ``LeaseManager`` across two threads -- which is the
    contract the orchestrator relies on. ``check_same_thread=False`` on the
    underlying SQLite connection makes this safe.
    """
    rid = uuid4()

    def _acquire_a():  # type: ignore[no-untyped-def]
        return tmp_lease_manager.acquire(rid, holder_id="alice", ttl_seconds=60)

    def _acquire_b():  # type: ignore[no-untyped-def]
        return tmp_lease_manager.acquire(rid, holder_id="bob", ttl_seconds=60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(_acquire_a)
        fut_b = pool.submit(_acquire_b)
        decision_a = fut_a.result()
        decision_b = fut_b.result()

    winners = [d for d in (decision_a, decision_b) if d.acquired]
    losers = [d for d in (decision_a, decision_b) if not d.acquired]
    assert len(winners) == 1, (
        f"expected exactly one winner; got {len(winners)} "
        f"(decisions={decision_a!r}, {decision_b!r})"
    )
    assert len(losers) == 1
    assert losers[0].reason == "held_by_other"
    # Winner reason must be 'acquired' (cold path); 'refreshed' would mean
    # the same holder won a race against itself, which the test doesn't do.
    assert winners[0].reason == "acquired"


# ---------------------------------------------------------------------------
# Refresh + release ownership
# ---------------------------------------------------------------------------

def test_lease_refresh_only_owner(
    tmp_lease_manager: LeaseManager,
) -> None:
    """``refresh`` returns False for a non-owner; True for the owner."""
    rid = uuid4()
    tmp_lease_manager.acquire(rid, holder_id="alice", ttl_seconds=60)

    assert tmp_lease_manager.refresh(rid, holder_id="bob", ttl_seconds=60) is False
    assert tmp_lease_manager.refresh(rid, holder_id="alice", ttl_seconds=60) is True


def test_lease_release_only_owner(
    tmp_lease_manager: LeaseManager,
    tmp_lane_store: LaneStore,
) -> None:
    """``release`` returns False for non-owner; True for owner; row gone after."""
    rid = uuid4()
    tmp_lease_manager.acquire(rid, holder_id="alice", ttl_seconds=60)

    # Bob can't release Alice's lease.
    assert tmp_lease_manager.release(rid, holder_id="bob") is False
    assert tmp_lane_store.get_lease(rid) is not None

    # Alice can.
    assert tmp_lease_manager.release(rid, holder_id="alice") is True
    assert tmp_lane_store.get_lease(rid) is None

    # Releasing again returns False (idempotent on the False side).
    assert tmp_lease_manager.release(rid, holder_id="alice") is False


# ---------------------------------------------------------------------------
# scan_stale + expired-reclaim (uses real time.sleep(1.1))
# ---------------------------------------------------------------------------

def test_lease_scan_stale_returns_expired_only(
    tmp_lease_manager: LeaseManager,
) -> None:
    """``scan_stale`` returns leases whose ``expires_at < now``.

    Inserts two TTL=1s leases plus one TTL=60s lease, sleeps 1.1s, then
    expects exactly the two short ones in the stale list.
    """
    short_a, short_b, long_c = uuid4(), uuid4(), uuid4()
    tmp_lease_manager.acquire(short_a, holder_id="alice", ttl_seconds=1)
    tmp_lease_manager.acquire(short_b, holder_id="bob", ttl_seconds=1)
    tmp_lease_manager.acquire(long_c, holder_id="carol", ttl_seconds=60)

    time.sleep(1.1)

    stale = tmp_lease_manager.scan_stale()
    stale_run_ids = {entry[0] for entry in stale}
    assert stale_run_ids == {short_a, short_b}
    # Long-lived lease must not appear.
    assert long_c not in stale_run_ids


def test_lease_acquire_reclaims_expired(
    tmp_lease_manager: LeaseManager,
) -> None:
    """A new holder can reclaim an expired lease; reason='expired_reclaimed'."""
    rid = uuid4()
    tmp_lease_manager.acquire(rid, holder_id="alice", ttl_seconds=1)

    time.sleep(1.1)

    decision = tmp_lease_manager.acquire(rid, holder_id="bob", ttl_seconds=60)
    assert decision.acquired is True
    assert decision.holder_id == "bob"
    assert decision.reason == "expired_reclaimed"
