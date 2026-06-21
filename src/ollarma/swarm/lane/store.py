"""store.py -- Split-storage primitive for the execution-lane swarm runtime.

Storage layout (under ``base_dir``)::

    base_dir/
    |-- runs.sqlite                      # cross-run index + lease rows
    `-- runs/
        `-- <run_id>/
            |-- swarm_run.json           # SwarmRun snapshot (atomic-write)
            |-- lane_transitions.jsonl   # hash-chained handoff receipts
            `-- lane_outputs/
                |-- <lane_id_1>.json     # LaneOutputBase (atomic-write)
                `-- <lane_id_2>.json

SQLite tables
-------------
``runs``     -- one row per SwarmRun (the JSONL/JSON files are the source of
                truth for full content; the row is an index for query).
``leases``   -- one row per active lease, keyed by ``run_id``. Atomic UPSERT
                semantics so ownership transfers are race-free.

Hash chain
----------
``lane_transitions.jsonl`` is append-only, hash-chained. Every line's
``parent_hash`` equals the previous line's ``transition_hash``. The first
line's ``parent_hash`` is ``None`` (matches the schema). Hashes are computed
via ``ollarma.evidence.canonical_hash`` -- the single hashing entry point
across the codebase (Pitfall 6).

Atomic-write contract
---------------------
* JSONL append: ``open(... "ab")`` + ``write(line + b"\\n")`` + ``fsync``.
  Atomicity is per-line on POSIX under append mode for writes below
  PIPE_BUF (4096 on Linux, 512 on macOS). Records here are well under
  4KB after sorted-keys orjson serialization.
* JSON file write: write to a sibling ``<name>.tmp.<pid>`` then ``os.replace``
  -- atomic rename on the same filesystem on POSIX.
* SQLite: WAL journal mode + ``synchronous=NORMAL``. UPSERT via
  ``INSERT ... ON CONFLICT(run_id) DO UPDATE``.

Concurrency
-----------
A single ``LaneStore`` instance is safe for in-process concurrent use thanks
to a per-instance ``threading.Lock`` on JSONL appends. SQLite handles its
own locking. Cross-process use is NOT supported in v5.1 (matches the
GatewayReceiptStore single-process contract).

Stdlib only
-----------
No third-party storage deps. ``sqlite3``, ``tempfile``, ``os``, ``pathlib``,
``threading``, ``uuid`` from stdlib; ``orjson`` and ``pydantic`` from the
existing top-level project deps.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import orjson
import pydantic

from ollarma.evidence import canonical_hash
from ollarma.swarm.lane.schemas import (
    Checkpoint,
    LaneOutputBase,
    LaneTransition,
    ResumeCandidate,
    SwarmRun,
)


__all__ = ["LaneStore", "LaneStoreError"]


class LaneStoreError(Exception):
    """Raised on chain corruption, schema violation, or required-field misses."""


_SQLITE_FILENAME = "runs.sqlite"
_RUNS_DIRNAME = "runs"
_TRANSITIONS_FILENAME = "lane_transitions.jsonl"
_OUTPUTS_DIRNAME = "lane_outputs"
_SWARM_RUN_FILENAME = "swarm_run.json"
_CHECKPOINT_FILENAME = "checkpoint.json"


# ---------------------------------------------------------------------------
# DDL (kept local so the schema lives next to the code that reads it)
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    requested_at    TEXT NOT NULL,
    status          TEXT NOT NULL,
    reason_code     TEXT,
    metadata_json   TEXT NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS leases (
    run_id          TEXT PRIMARY KEY,
    lane_id         TEXT NOT NULL,
    holder_id       TEXT NOT NULL,
    acquired_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_status      ON runs(status);
CREATE INDEX IF NOT EXISTS idx_leases_expires   ON leases(expires_at);
"""


def _isoformat(t: datetime) -> str:
    """Stable ISO-8601 with explicit UTC offset."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.isoformat()


def _parse_isoformat(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _hash_transition(payload: dict[str, Any]) -> str:
    """Canonical hash of a transition payload with ``transition_hash`` stripped.

    Mirrors ``GatewayReceiptStore._hash_payload`` (Phase 57) so the chain
    semantics are identical: the receipt's own self-referential hash field
    is excluded from its own input.
    """
    body = {k: v for k, v in payload.items() if k != "transition_hash"}
    return canonical_hash(body)


# ---------------------------------------------------------------------------
# LaneStore
# ---------------------------------------------------------------------------

class LaneStore:
    """SQLite (index + leases) + JSONL (transitions) + atomic JSON (outputs).

    Construct once per process per ``base_dir``. Opens (or creates) the
    SQLite database, the ``runs/`` directory, and the leases/runs tables.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)
        (self._base_dir / _RUNS_DIRNAME).mkdir(parents=True, exist_ok=True)
        self._db_path = self._base_dir / _SQLITE_FILENAME
        self._lock = threading.Lock()
        # ``check_same_thread=False`` lets the caller pass the store across
        # threads safely (the per-instance ``self._lock`` serializes writes,
        # and SQLite WAL handles concurrent readers).
        self._conn = sqlite3.connect(
            self._db_path, check_same_thread=False, isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_DDL)

    # -- Public path helpers --------------------------------------------------

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def run_dir_for(self, run_id: UUID) -> Path:
        """Return the per-run directory ``<base_dir>/runs/<run_id>/``.

        Creates the directory (and the ``lane_outputs/`` subdirectory)
        idempotently. Plan 66-02's orchestrator calls this to stage a run
        before writing transitions/outputs.
        """
        d = self._base_dir / _RUNS_DIRNAME / str(run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / _OUTPUTS_DIRNAME).mkdir(parents=True, exist_ok=True)
        return d

    # -- Lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection. Idempotent."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass

    # -- SQLite: runs index ---------------------------------------------------

    def upsert_run(self, run: SwarmRun) -> None:
        """Insert-or-replace a SwarmRun row (and persist the JSON snapshot).

        The JSON snapshot lives at ``runs/<run_id>/swarm_run.json`` and is
        the source of truth for the full record (the SQLite row is an index
        for query). Both writes are atomic.
        """
        metadata_json = orjson.dumps(
            run.metadata, option=orjson.OPT_SORT_KEYS,
        ).decode("utf-8")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs (run_id, requested_at, status, reason_code,
                                  metadata_json, schema_version)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    requested_at   = excluded.requested_at,
                    status         = excluded.status,
                    reason_code    = excluded.reason_code,
                    metadata_json  = excluded.metadata_json,
                    schema_version = excluded.schema_version
                """,
                (
                    str(run.run_id),
                    _isoformat(run.requested_at),
                    run.status,
                    run.reason_code,
                    metadata_json,
                    run.schema_version,
                ),
            )

        # Persist the canonical JSON snapshot under the per-run dir.
        run_dir = self.run_dir_for(run.run_id)
        snapshot_path = run_dir / _SWARM_RUN_FILENAME
        self._atomic_write_json(
            snapshot_path,
            run.model_dump(mode="json"),
        )

    def get_run(self, run_id: UUID) -> SwarmRun | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT requested_at, status, reason_code, metadata_json, "
                "schema_version FROM runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
        if row is None:
            return None
        requested_at, status, reason_code, metadata_json, schema_version = row
        try:
            metadata = orjson.loads(metadata_json) if metadata_json else {}
        except orjson.JSONDecodeError as exc:
            raise LaneStoreError(
                f"corrupted metadata_json for run {run_id}: {exc}"
            ) from exc
        try:
            return SwarmRun(
                run_id=run_id,
                requested_at=_parse_isoformat(requested_at),
                status=status,
                reason_code=reason_code,
                metadata=metadata,
                schema_version=schema_version,
            )
        except pydantic.ValidationError as exc:
            raise LaneStoreError(
                f"failed to materialize SwarmRun for {run_id}: {exc}"
            ) from exc

    # -- SQLite: leases -------------------------------------------------------

    def upsert_lease(
        self,
        run_id: UUID,
        lane_id: UUID,
        holder_id: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> None:
        """Atomic UPSERT of the lease row for ``run_id``.

        A second upsert with a different ``holder_id`` overwrites the first --
        this is the point of UPSERT semantics (Phase 67's lease-manager will
        reject conflicting holders BEFORE calling here; this primitive is
        race-safe but not policy-aware).
        """
        if ttl_seconds <= 0:
            raise LaneStoreError(
                f"ttl_seconds must be > 0, got {ttl_seconds}"
            )
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        # Build the expires_at via timedelta so we don't depend on calendar
        # arithmetic across DST etc. -- UTC throughout.
        from datetime import timedelta
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO leases (run_id, lane_id, holder_id,
                                    acquired_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    lane_id      = excluded.lane_id,
                    holder_id    = excluded.holder_id,
                    acquired_at  = excluded.acquired_at,
                    expires_at   = excluded.expires_at
                """,
                (
                    str(run_id),
                    str(lane_id),
                    holder_id,
                    _isoformat(now),
                    _isoformat(expires_at),
                ),
            )

    def try_insert_lease(
        self,
        run_id: UUID,
        lane_id: UUID,
        holder_id: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Atomic conditional INSERT -- returns True iff the row was created.

        Unlike :meth:`upsert_lease`, this is the cold-path-acquire primitive:
        if a row already exists for ``run_id`` (any holder), the INSERT is a
        no-op and we return ``False``. The caller (LeaseManager.acquire) uses
        the False return to fall back to the held-by-other / refresh /
        expired-reclaim branches.

        Atomicity boundary: SQLite's ``INSERT ... ON CONFLICT DO NOTHING``
        is a single statement -- between two threads that both see no row,
        exactly one will succeed (rowcount=1) and the other will see
        rowcount=0. This is the race-free primitive ``acquire`` needs.
        """
        if ttl_seconds <= 0:
            raise LaneStoreError(
                f"ttl_seconds must be > 0, got {ttl_seconds}"
            )
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        from datetime import timedelta
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO leases (run_id, lane_id, holder_id,
                                    acquired_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (
                    str(run_id),
                    str(lane_id),
                    holder_id,
                    _isoformat(now),
                    _isoformat(expires_at),
                ),
            )
            return cur.rowcount > 0

    def replace_expired_lease(
        self,
        run_id: UUID,
        lane_id: UUID,
        holder_id: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Atomic compare-and-swap reclaim of an EXPIRED lease.

        Updates the row IFF its current ``expires_at < now``. Returns True
        if the swap happened (we now own the lease) or False if the row is
        no longer expired (someone else reclaimed it first, or the original
        holder refreshed in the gap).

        Together with :meth:`try_insert_lease`, this gives ``LeaseManager``
        a race-free pair of primitives for cold-acquire and expired-reclaim
        without TOCTOU windows.
        """
        if ttl_seconds <= 0:
            raise LaneStoreError(
                f"ttl_seconds must be > 0, got {ttl_seconds}"
            )
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        from datetime import timedelta
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE leases
                   SET lane_id     = ?,
                       holder_id   = ?,
                       acquired_at = ?,
                       expires_at  = ?
                 WHERE run_id     = ?
                   AND expires_at  < ?
                """,
                (
                    str(lane_id),
                    holder_id,
                    _isoformat(now),
                    _isoformat(expires_at),
                    str(run_id),
                    _isoformat(now),
                ),
            )
            return cur.rowcount > 0

    def get_lease(
        self, run_id: UUID,
    ) -> tuple[UUID, str, datetime] | None:
        """Return ``(lane_id, holder_id, expires_at)`` or ``None`` if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT lane_id, holder_id, expires_at FROM leases "
                "WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
        if row is None:
            return None
        lane_id, holder_id, expires_at = row
        return (UUID(lane_id), holder_id, _parse_isoformat(expires_at))

    def list_stale_leases(
        self, now: datetime | None = None,
    ) -> list[tuple[UUID, UUID, str, datetime]]:
        """Leases whose ``expires_at < now``.

        Returns ``(run_id, lane_id, holder_id, expires_at)`` tuples,
        ordered by ``expires_at`` ascending (oldest expired first).
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, lane_id, holder_id, expires_at FROM leases "
                "WHERE expires_at < ? ORDER BY expires_at ASC",
                (_isoformat(now),),
            ).fetchall()
        return [
            (UUID(r[0]), UUID(r[1]), r[2], _parse_isoformat(r[3]))
            for r in rows
        ]

    def release_lease(self, run_id: UUID, holder_id: str) -> bool:
        """Conditional delete -- returns True only if the holder matches.

        Prevents a stale or impersonating holder from releasing someone
        else's lease. The lease-manager (plan 66-02) uses this to enforce
        ownership on graceful release.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM leases WHERE run_id = ? AND holder_id = ?",
                (str(run_id), holder_id),
            )
            return cur.rowcount > 0

    # -- JSONL: lane_transitions ---------------------------------------------

    def append_transition(
        self,
        transition: LaneTransition,
        run_dir: Path,
    ) -> LaneTransition:
        """Append one ``LaneTransition`` to ``<run_dir>/lane_transitions.jsonl``.

        Computes ``transition_hash`` over the canonical (sorted-keys) JSON
        of the transition (excluding ``transition_hash`` itself, since that
        field is the chain's self-referential terminal). Returns a new
        ``LaneTransition`` instance with ``transition_hash`` populated --
        the input is unchanged (frozen pydantic model).

        Chain rule: if the JSONL file already has lines, the new transition's
        ``parent_hash`` MUST equal the last line's ``transition_hash``. The
        caller is responsible for setting ``parent_hash`` correctly before
        calling; this method validates and raises ``LaneStoreError`` on
        mismatch (so a buggy caller fails loud rather than silently breaking
        the chain).
        """
        path = run_dir / _TRANSITIONS_FILENAME
        with self._lock:
            tail_hash = self._tail_transition_hash(path)
            if transition.parent_hash != tail_hash:
                raise LaneStoreError(
                    f"parent_hash mismatch: expected {tail_hash!r}, "
                    f"got {transition.parent_hash!r}"
                )
            payload = transition.model_dump(mode="json")
            payload["transition_hash"] = ""
            computed = _hash_transition(payload)
            payload["transition_hash"] = computed

            try:
                materialized = LaneTransition.model_validate(payload)
            except pydantic.ValidationError as exc:
                raise LaneStoreError(
                    f"materialized transition failed re-validation: {exc}"
                ) from exc

            line = orjson.dumps(
                materialized.model_dump(mode="json"),
                option=orjson.OPT_SORT_KEYS,
            )
            if b"\n" in line:
                raise LaneStoreError(
                    "serialized transition contains an embedded newline"
                )
            try:
                with path.open("ab") as fh:
                    fh.write(line)
                    fh.write(b"\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                raise LaneStoreError(
                    f"failed to append transition to {path}: {exc}"
                ) from exc

            return materialized

    def _tail_transition_hash(self, path: Path) -> str | None:
        """Return the last line's ``transition_hash``, or ``None`` if empty."""
        if not path.exists():
            return None
        last: bytes | None = None
        try:
            with path.open("rb") as fh:
                for raw in fh:
                    stripped = raw.rstrip(b"\n")
                    if stripped:
                        last = stripped
        except OSError as exc:
            raise LaneStoreError(f"failed to read {path}: {exc}") from exc
        if last is None:
            return None
        try:
            record = orjson.loads(last)
        except orjson.JSONDecodeError as exc:
            raise LaneStoreError(
                f"tail of {path.name} is not valid JSON: {exc}"
            ) from exc
        tail = record.get("transition_hash")
        if not isinstance(tail, str) or not tail:
            raise LaneStoreError(
                f"tail of {path.name} missing populated transition_hash"
            )
        return tail

    def load_transitions(self, run_dir: Path) -> list[LaneTransition]:
        """Read every transition for a run, in order. Empty list if missing."""
        path = run_dir / _TRANSITIONS_FILENAME
        if not path.exists():
            return []
        out: list[LaneTransition] = []
        try:
            with path.open("rb") as fh:
                for i, raw in enumerate(fh, start=1):
                    stripped = raw.rstrip(b"\n")
                    if not stripped:
                        continue
                    try:
                        record = orjson.loads(stripped)
                    except orjson.JSONDecodeError as exc:
                        raise LaneStoreError(
                            f"{path.name} line {i} is not valid JSON: {exc}"
                        ) from exc
                    try:
                        out.append(LaneTransition.model_validate(record))
                    except pydantic.ValidationError as exc:
                        raise LaneStoreError(
                            f"{path.name} line {i} failed schema validation: "
                            f"{exc}"
                        ) from exc
        except OSError as exc:
            raise LaneStoreError(f"failed to read {path}: {exc}") from exc
        return out

    def verify_chain(self, run_dir: Path) -> bool:
        """Walk transitions; True iff every parent_hash + transition_hash matches.

        Catches any out-of-band JSONL mutation (line edited, line removed,
        line inserted) -- the recomputed hash will not match either the
        stored ``transition_hash`` or the next line's ``parent_hash``.
        """
        path = run_dir / _TRANSITIONS_FILENAME
        if not path.exists():
            return True  # vacuously true; no chain to break
        try:
            with path.open("rb") as fh:
                lines = [
                    raw.rstrip(b"\n") for raw in fh if raw.rstrip(b"\n")
                ]
        except OSError:
            return False
        if not lines:
            return True
        parent: str | None = None
        for raw in lines:
            try:
                record = orjson.loads(raw)
            except orjson.JSONDecodeError:
                return False
            if record.get("parent_hash") != parent:
                return False
            stored_hash = record.get("transition_hash")
            if not isinstance(stored_hash, str) or not stored_hash:
                return False
            expected = _hash_transition(record)
            if expected != stored_hash:
                return False
            parent = stored_hash
        return True

    # -- JSON: per-lane outputs ----------------------------------------------

    def write_lane_output(
        self,
        output: LaneOutputBase,
        run_dir: Path,
    ) -> Path:
        """Atomically write a lane's typed output to ``lane_outputs/<lane_id>.json``.

        Returns the absolute path written. Uses temp+``os.replace`` so a
        reader never sees a half-written file.
        """
        outputs_dir = run_dir / _OUTPUTS_DIRNAME
        outputs_dir.mkdir(parents=True, exist_ok=True)
        target = outputs_dir / f"{output.lane_id}.json"
        self._atomic_write_json(target, output.model_dump(mode="json"))
        return target

    def load_lane_output(
        self,
        run_dir: Path,
        lane_id: UUID,
    ) -> dict[str, Any] | None:
        """Read a lane output back as a dict (caller picks the concrete model).

        Returns ``None`` if the file is missing. Returns the raw JSON dict
        (not a pydantic model) so the caller can dispatch on ``role`` to
        the right ``*Output`` subclass.
        """
        path = run_dir / _OUTPUTS_DIRNAME / f"{lane_id}.json"
        if not path.exists():
            return None
        try:
            return orjson.loads(path.read_bytes())
        except (OSError, orjson.JSONDecodeError) as exc:
            raise LaneStoreError(
                f"failed to read lane output {path}: {exc}"
            ) from exc

    # -- Phase 67: checkpoint primitives -------------------------------------

    def write_checkpoint(
        self,
        checkpoint: Checkpoint,
        run_dir: Path,
    ) -> Path:
        """Atomically write ``<run_dir>/checkpoint.json``.

        Reuses the same temp+rename invariant as :meth:`write_lane_output`
        and :meth:`upsert_run` (their internal :meth:`_atomic_write_json`).
        Returns the final on-disk path.

        The Phase 67 resume orchestrator (plan 67-02) calls this after every
        lane completion so a crashed/expired run always has the freshest
        possible "where did we stop" pointer on disk.
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / _CHECKPOINT_FILENAME
        self._atomic_write_json(target, checkpoint.model_dump(mode="json"))
        return target

    def load_checkpoint(self, run_dir: Path) -> Checkpoint | None:
        """Read ``<run_dir>/checkpoint.json`` back into a :class:`Checkpoint`.

        Returns ``None`` if the file is missing -- callers (resume
        orchestrator, :meth:`list_resumable_runs`) treat that case as
        "fall back to walking lane_outputs/".
        """
        path = run_dir / _CHECKPOINT_FILENAME
        if not path.exists():
            return None
        try:
            payload = orjson.loads(path.read_bytes())
        except (OSError, orjson.JSONDecodeError) as exc:
            raise LaneStoreError(
                f"failed to read checkpoint {path}: {exc}"
            ) from exc
        try:
            return Checkpoint.model_validate(payload)
        except pydantic.ValidationError as exc:
            raise LaneStoreError(
                f"checkpoint at {path} failed schema validation: {exc}"
            ) from exc

    def list_resumable_runs(
        self, now: datetime | None = None,
    ) -> list[ResumeCandidate]:
        """Return runs that are safely resumable: in-progress + lease-expired.

        Joins ``runs`` and ``leases`` on ``run_id`` and filters to rows where
        ``runs.status = 'in_progress'`` AND ``leases.expires_at < now``.
        Sorted by ``lease_expired_at`` ascending (oldest expired first --
        most stale candidate at the head).

        ``last_completed_role`` is filled in best-effort by reading the
        on-disk ``checkpoint.json`` for each candidate run via
        :meth:`load_checkpoint`. This is per-call file I/O but is acceptable
        because :meth:`list_resumable_runs` is operator-facing (CLI / dashboard
        listing) -- not a hot path. Missing checkpoint -> ``None``.
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT runs.run_id, leases.holder_id, leases.expires_at
                  FROM runs
                  JOIN leases ON leases.run_id = runs.run_id
                 WHERE runs.status = 'in_progress'
                   AND leases.expires_at < ?
                 ORDER BY leases.expires_at ASC
                """,
                (_isoformat(now),),
            ).fetchall()

        candidates: list[ResumeCandidate] = []
        for run_id_str, holder_id, expires_at_str in rows:
            run_id = UUID(run_id_str)
            lease_expired_at = _parse_isoformat(expires_at_str)
            if lease_expired_at.tzinfo is None:
                lease_expired_at = lease_expired_at.replace(tzinfo=timezone.utc)
            age_seconds = (now - lease_expired_at).total_seconds()

            # Best-effort: read last_completed_role from checkpoint.json.
            run_dir = self.run_dir_for(run_id)
            try:
                checkpoint = self.load_checkpoint(run_dir)
            except LaneStoreError:
                # Corrupted checkpoint shouldn't hide an otherwise-resumable
                # run from operator listing. Treat as "no checkpoint".
                checkpoint = None
            last_completed_role = (
                checkpoint.last_completed_role if checkpoint is not None else None
            )

            candidates.append(
                ResumeCandidate(
                    run_id=run_id,
                    last_completed_role=last_completed_role,
                    prior_holder_id=holder_id,
                    lease_expired_at=lease_expired_at,
                    candidate_age_seconds=age_seconds,
                )
            )
        return candidates

    # -- Internals ------------------------------------------------------------

    def _atomic_write_json(self, target: Path, payload: Any) -> None:
        """Atomic-write JSON: temp file + ``os.replace``.

        ``os.replace`` is atomic on POSIX for same-filesystem renames. We
        place the temp sibling next to the target so they share a filesystem.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        line = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2)
        tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
        try:
            with tmp.open("wb") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise LaneStoreError(
                f"atomic write failed for {target}: {exc}"
            ) from exc
