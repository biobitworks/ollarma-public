"""idempotency.py -- Per-namespace SQLite idempotency store with 24h TTL (NS-04).

Compound key formula:
    canonical_hash({"namespace": ns, "transport": t, "content_hash": canonical_hash(body)})

Storage: ~/.cache/ollarma/idempotency/<sanitized_namespace>.sqlite
TTL: 24h (default), lazy eviction on every write path.
Row cap: 100,000 rows per namespace file; oldest 10,000 evicted when cap is reached.

Race safety: INSERT OR IGNORE is atomic in SQLite WAL mode. Concurrent identical
requests race at insert time; the losing writer reads back the winning row and
both callers receive the same stored result without exception.
"""
from __future__ import annotations

import datetime
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CACHE_BASE: Path = Path.home() / ".cache" / "ollarma" / "idempotency"
_ROW_CAP: int = 100_000
_EVICT_BATCH: int = 10_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_cache (
    compound_key  TEXT PRIMARY KEY,
    transport     TEXT NOT NULL,
    result_json   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expires ON idempotency_cache(expires_at);
"""

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sanitize_prefix(prefix: str) -> str:
    """Map namespace prefix to a safe filename component.

    Examples:
        "ollarma-demo:" -> "ollarma-demo_"
        ""              -> "__unscoped__"
        "ns-alpha:"     -> "ns-alpha_"
    """
    cleaned = re.sub(r"[^a-z0-9-]", "_", prefix.lower())
    return cleaned or "__unscoped__"


def _now_utc() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_plus_hours(hours: float) -> str:
    """Return current UTC time + hours in ISO 8601 format."""
    dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Compound key composition
# ---------------------------------------------------------------------------

def make_compound_key(
    namespace_prefix: str,
    transport: str,
    canonical_body: dict,
) -> str:
    """Compute the compound idempotency key for a request.

    Args:
        namespace_prefix: The validated namespace prefix (e.g. "ollarma-demo:").
            Use "" for unscoped requests.
        transport: Transport label — "http", "mcp", or "cli".
        canonical_body: Stable, semantic request fields only. Exclude run_id,
            created_at, timing fields, and any value that varies per call
            but not per semantic request.

    Returns:
        Hex SHA-256 digest of the canonical key composition.

    Key formula:
        canonical_hash({
            "namespace": namespace_prefix,
            "transport": transport,
            "content_hash": canonical_hash(canonical_body),
        })
    """
    # Late import to avoid circular imports at module init time.
    from ollarma.evidence import canonical_hash  # noqa: PLC0415

    content_hash = canonical_hash(canonical_body)
    return canonical_hash({
        "namespace": namespace_prefix,
        "transport": transport,
        "content_hash": content_hash,
    })


# ---------------------------------------------------------------------------
# Idempotency store
# ---------------------------------------------------------------------------

class IdempotencyStore:
    """Thread-safe per-namespace idempotency store backed by SQLite WAL mode.

    One instance per namespace prefix; backed by a single SQLite file at
    _CACHE_BASE / <sanitized_prefix>.sqlite.

    Thread safety: a per-instance threading.Lock guards all write operations.
    The underlying SQLite connection is opened with check_same_thread=False
    and WAL journal mode, which makes concurrent reads from multiple threads
    safe without holding the lock.
    """

    def __init__(self, namespace_prefix: str) -> None:
        self._prefix = namespace_prefix
        self._sanitized = _sanitize_prefix(namespace_prefix)
        self._path: Path = _CACHE_BASE / f"{self._sanitized}.sqlite"
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def _connect(self) -> sqlite3.Connection:
        """Open and initialise the SQLite connection (lazy, idempotent)."""
        if self._conn is not None:
            return self._conn
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        return conn

    def get(self, key: str) -> Optional[str]:
        """Return stored result_json if key exists and has not expired; else None.

        Does NOT delete expired rows on read (eviction is on the write path).
        """
        conn = self._connect()
        now = _now_utc()
        row = conn.execute(
            "SELECT result_json FROM idempotency_cache "
            "WHERE compound_key = ? AND expires_at > ?",
            (key, now),
        ).fetchone()
        return row[0] if row else None

    def put(
        self,
        key: str,
        result_json: str,
        transport: str = "",
        ttl_hours: float = 24.0,
    ) -> None:
        """Store result_json under key with the given TTL.

        Write-path side effects (all under the instance lock):
          1. Delete all expired rows (lazy TTL eviction).
          2. If row count >= _ROW_CAP, evict the oldest _EVICT_BATCH rows.
          3. INSERT OR IGNORE the new row (concurrent identical inserts are no-ops).
        """
        now = _now_utc()
        expires_at = _utc_plus_hours(ttl_hours)
        with self._lock:
            conn = self._connect()
            # Step 1: Lazy TTL eviction — purge all expired rows.
            conn.execute(
                "DELETE FROM idempotency_cache WHERE expires_at < ?",
                (now,),
            )
            # Step 2: Row cap backstop — evict oldest batch when over cap.
            count: int = conn.execute(
                "SELECT COUNT(*) FROM idempotency_cache"
            ).fetchone()[0]
            if count >= _ROW_CAP:
                conn.execute(
                    "DELETE FROM idempotency_cache WHERE compound_key IN "
                    "(SELECT compound_key FROM idempotency_cache "
                    " ORDER BY created_at ASC LIMIT ?)",
                    (_EVICT_BATCH,),
                )
            # Step 3: Race-safe insert. Concurrent thread with same key: no-op.
            conn.execute(
                "INSERT OR IGNORE INTO idempotency_cache "
                "(compound_key, transport, result_json, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, transport, result_json, now, expires_at),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Process-level store registry
# ---------------------------------------------------------------------------

_stores: dict[str, IdempotencyStore] = {}
_stores_lock = threading.Lock()


def get_store(namespace_prefix: str) -> IdempotencyStore:
    """Return (or create) the per-namespace IdempotencyStore singleton.

    Double-checked locking ensures exactly one store per namespace prefix
    is created per process lifetime, matching the _scheduler singleton
    pattern in service.py.
    """
    if namespace_prefix in _stores:
        return _stores[namespace_prefix]
    with _stores_lock:
        if namespace_prefix not in _stores:
            _stores[namespace_prefix] = IdempotencyStore(namespace_prefix)
        return _stores[namespace_prefix]
