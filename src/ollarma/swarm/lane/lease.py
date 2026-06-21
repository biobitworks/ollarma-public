"""lease.py -- Atomic lease ownership over ``LaneStore`` SQLite (Phase 66 plan 02).

A ``LeaseManager`` is a thin policy layer above the lower-level UPSERT/SELECT/
DELETE primitives in ``LaneStore``. It enforces the v5.1 ownership semantics:

* ``acquire`` returns a :class:`LeaseDecision` that distinguishes the four
  observable outcomes (acquired-fresh, refreshed-by-self, held-by-other,
  expired-and-reclaimed) without lying about which path was taken. Callers
  (the orchestrator in ``runtime.py``) treat any ``acquired=True`` as a
  green light to proceed.
* ``refresh`` is conditional: it bumps ``expires_at`` IFF the current holder
  in SQLite still matches ``holder_id``. If a different holder has taken
  over (after a stale-lease scan reclaim, say), refresh returns False and
  the caller MUST treat the lease as lost.
* ``release`` is conditional on holder match (mirrors
  ``LaneStore.release_lease``); a wrong holder gets False and the row stays.
* ``scan_stale`` is a pass-through to ``LaneStore.list_stale_leases`` so the
  caller picks the policy (release them, escalate them, page on them).

All methods are synchronous. No retries. No sleeps. Caller decides policy.
The ``LaneStore`` SQLite UPSERT is the atomicity boundary; two concurrent
``acquire`` calls for the same ``run_id`` from different holders will see
exactly one return ``acquired=True`` (the loser will see the winner's row
on its read-back and return ``acquired=False, reason="held_by_other"``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from ollarma.swarm.lane.store import LaneStore


__all__ = ["LeaseDecision", "LeaseManager"]


@dataclass(frozen=True)
class LeaseDecision:
    """Outcome of a single ``LeaseManager.acquire`` call.

    ``reason`` is one of:
    * ``"acquired"``           -- no prior holder; row inserted.
    * ``"refreshed"``          -- caller already held it; ``expires_at`` bumped.
    * ``"held_by_other"``      -- a different holder owns an unexpired lease;
                                  ``acquired=False``.
    * ``"expired_reclaimed"``  -- prior holder's lease expired; reclaimed
                                  for the caller; row overwritten.
    """

    acquired: bool
    holder_id: str | None
    expires_at: datetime | None
    reason: str


class LeaseManager:
    """Atomic lease ownership over LaneStore SQLite.

    All methods are synchronous + atomic at the SQLite UPSERT boundary. No
    retries, no sleeps -- caller decides policy.
    """

    def __init__(self, store: LaneStore) -> None:
        self._store = store

    def acquire(
        self,
        run_id: UUID,
        holder_id: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> LeaseDecision:
        """Try to acquire the lease for ``run_id``.

        Decision table:

        =====================================  ===============  ==========================
        Current state                          acquired         reason
        =====================================  ===============  ==========================
        no row                                 True             ``"acquired"``
        row.holder_id == ``holder_id``         True             ``"refreshed"``
        row.holder_id != caller, not expired   False            ``"held_by_other"``
        row.holder_id != caller, expired       True             ``"expired_reclaimed"``
        =====================================  ===============  ==========================

        ``lane_id`` is auto-generated per acquire (a fresh UUID v4). It is
        recorded in the SQLite row for forensic purposes; the orchestrator
        does not depend on it (lane identities for outputs are tracked on
        the ``LaneRun`` records).
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # 1. Cold-acquire path: try a single-statement
        #    ``INSERT ... ON CONFLICT DO NOTHING``. If it succeeds, we won;
        #    if it returns False, a row already existed and we fall through
        #    to refresh / reclaim / held-by-other handling below. This is
        #    race-free across threads (and across connections sharing a
        #    SQLite file) -- exactly one concurrent caller sees rowcount=1.
        new_lane_id = uuid4()
        if self._store.try_insert_lease(
            run_id, new_lane_id, holder_id, ttl_seconds, now=now,
        ):
            confirmed = self._store.get_lease(run_id)
            if confirmed is None:
                # Defensive -- our INSERT just succeeded.
                return LeaseDecision(
                    acquired=False,
                    holder_id=None,
                    expires_at=None,
                    reason="held_by_other",
                )
            _, _, confirmed_expires = confirmed
            return LeaseDecision(
                acquired=True,
                holder_id=holder_id,
                expires_at=confirmed_expires,
                reason="acquired",
            )

        # 2. Existing row -- read it. (May have been written by a concurrent
        #    cold-acquire that beat us by a microsecond.)
        existing = self._store.get_lease(run_id)
        if existing is None:
            # Race: row vanished between INSERT failing and SELECT (a
            # concurrent release_lease). Retry the cold-path once.
            if self._store.try_insert_lease(
                run_id, new_lane_id, holder_id, ttl_seconds, now=now,
            ):
                confirmed = self._store.get_lease(run_id)
                if confirmed is not None:
                    return LeaseDecision(
                        acquired=True,
                        holder_id=holder_id,
                        expires_at=confirmed[2],
                        reason="acquired",
                    )
            return LeaseDecision(
                acquired=False,
                holder_id=None,
                expires_at=None,
                reason="held_by_other",
            )

        existing_lane_id, existing_holder, existing_expires = existing
        if existing_expires.tzinfo is None:
            existing_expires = existing_expires.replace(tzinfo=timezone.utc)

        # 3. Same holder -- refresh.
        if existing_holder == holder_id:
            self._store.upsert_lease(
                run_id, existing_lane_id, holder_id, ttl_seconds, now=now,
            )
            confirmed = self._store.get_lease(run_id)
            assert confirmed is not None  # we just upserted
            return LeaseDecision(
                acquired=True,
                holder_id=holder_id,
                expires_at=confirmed[2],
                reason="refreshed",
            )

        # 4. Different holder, but their lease has expired -- atomic CAS reclaim.
        if existing_expires < now:
            if self._store.replace_expired_lease(
                run_id, new_lane_id, holder_id, ttl_seconds, now=now,
            ):
                confirmed = self._store.get_lease(run_id)
                if confirmed is None:
                    # Defensive -- UPDATE just succeeded.
                    return LeaseDecision(
                        acquired=False,
                        holder_id=None,
                        expires_at=None,
                        reason="held_by_other",
                    )
                _, confirmed_holder, confirmed_expires = confirmed
                if confirmed_holder == holder_id:
                    return LeaseDecision(
                        acquired=True,
                        holder_id=holder_id,
                        expires_at=confirmed_expires,
                        reason="expired_reclaimed",
                    )
                # Another caller reclaimed in the same UPDATE window --
                # they won; we lost.
                return LeaseDecision(
                    acquired=False,
                    holder_id=confirmed_holder,
                    expires_at=confirmed_expires,
                    reason="held_by_other",
                )
            # Conditional UPDATE matched 0 rows -- someone else reclaimed
            # first, or the original holder refreshed before our UPDATE.
            confirmed = self._store.get_lease(run_id)
            if confirmed is None:
                return LeaseDecision(
                    acquired=False,
                    holder_id=None,
                    expires_at=None,
                    reason="held_by_other",
                )
            return LeaseDecision(
                acquired=False,
                holder_id=confirmed[1],
                expires_at=confirmed[2],
                reason="held_by_other",
            )

        # 5. Different holder, lease still valid.
        return LeaseDecision(
            acquired=False,
            holder_id=existing_holder,
            expires_at=existing_expires,
            reason="held_by_other",
        )

    def refresh(
        self,
        run_id: UUID,
        holder_id: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Refresh ``expires_at`` IFF this holder still owns the row.

        Returns True on successful refresh; False if a different holder has
        taken over (caller should treat as lease loss).
        """
        existing = self._store.get_lease(run_id)
        if existing is None:
            return False
        existing_lane_id, existing_holder, _ = existing
        if existing_holder != holder_id:
            return False
        now = now or datetime.now(timezone.utc)
        self._store.upsert_lease(
            run_id, existing_lane_id, holder_id, ttl_seconds, now=now,
        )
        # Confirm we're still the holder after the upsert (race guard).
        confirmed = self._store.get_lease(run_id)
        if confirmed is None or confirmed[1] != holder_id:
            return False
        return True

    def release(self, run_id: UUID, holder_id: str) -> bool:
        """Release iff this holder owns the lease. Returns True if released."""
        return self._store.release_lease(run_id, holder_id)

    def scan_stale(
        self, now: datetime | None = None,
    ) -> list[tuple[UUID, UUID, str, datetime]]:
        """Pass-through to ``LaneStore.list_stale_leases``.

        Returns ``(run_id, lane_id, holder_id, expires_at)`` tuples,
        ordered by ``expires_at`` ascending (oldest first). Caller decides
        whether to release them, escalate, or page.
        """
        return self._store.list_stale_leases(now=now)
