"""macfind_searcher.py — BM25+cosine hybrid search over macfind index."""
from __future__ import annotations

import datetime as dt
import pathlib
import re as _re
import sqlite3
import time
from typing import Any

import numpy as np
import orjson
from pydantic import BaseModel

from ollarma.macfind_indexer import INDEX_DB_PATH, _embed

# ---------------------------------------------------------------------------
# Receipt logging path
# ---------------------------------------------------------------------------

RECEIPTS_PATH = INDEX_DB_PATH.parent / "receipts.jsonl"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class MacFindHit(BaseModel):
    model_config = {"frozen": True}
    path: str
    snippet: str
    bm25_score: float
    cosine_score: float
    hybrid_score: float


class MacFindReceipt(BaseModel):
    model_config = {"frozen": True}
    query: str
    namespace: str
    status: str  # "ok" | "no_match" | "index_missing" | "not_configured"
    no_match_reason: str | None = None
    hits: tuple[MacFindHit, ...] = ()
    redaction_events: tuple[str, ...] = ()
    query_at: str
    latency_ms: float


# ---------------------------------------------------------------------------
# Secret pattern + Antigence redaction (SAFE-01)
# ---------------------------------------------------------------------------

_SECRET_PATTERN = _re.compile(
    r"(?i)(api[_\s]?key|password|secret|token|private[_\s]?key|bearer\s+\w+|"
    r"aws_secret|gh[op]_\w{10,})",
    _re.IGNORECASE,
)

REDACT_DEBOUNCE_PATTERN = _SECRET_PATTERN  # alias for clarity


def _apply_antigence_redaction(
    hits: list,
    gate: "Any | None",
    redaction_events: list[str],
) -> list:
    """Apply regex prefilter + optional guardrail gate to MacFindHit list (SAFE-01).

    Regex fast-path: if no secret pattern in snippet, skip gate entirely.
    block -> exclude hit, log REDACTED event.
    flag -> keep hit, log FLAGGED event.
    Returns filtered hits list.
    """
    if gate is None:
        return hits

    filtered = []
    for hit in hits:
        text = hit.snippet
        if not _SECRET_PATTERN.search(text):
            filtered.append(hit)
            continue
        # Potential secret — run through guardrail gate
        tristate, _ = gate.validate(text)
        if tristate == "block":
            redaction_events.append(f"REDACTED:{hit.path}")
        elif tristate == "flag":
            redaction_events.append(f"FLAGGED:{hit.path}")
            filtered.append(hit)
        else:
            filtered.append(hit)
    return filtered


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity. Returns 0.0 if either vector is zero."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _extract_snippet(chunk_text: str, query: str, max_len: int = 200) -> str:
    """Return a short snippet near query terms."""
    terms = query.lower().split()
    text_lower = chunk_text.lower()
    idx = -1
    for term in terms:
        idx = text_lower.find(term)
        if idx != -1:
            break
    if idx == -1:
        return chunk_text[:max_len].strip()
    start = max(0, idx - 50)
    prefix = "..." if start > 0 else ""
    return (prefix + chunk_text[start : start + max_len].strip())[:max_len + 3]


# ---------------------------------------------------------------------------
# Searcher
# ---------------------------------------------------------------------------


class MacFindSearcher:
    """Hybrid BM25+cosine searcher over the macfind SQLite FTS5 index."""

    def __init__(self, db_path: pathlib.Path = INDEX_DB_PATH) -> None:
        self._db_path = db_path

    def search(
        self,
        query: str,
        *,
        namespace: str = "__unscoped__",
        max_results: int = 10,
        confidence_threshold: float = 0.25,
        gate: "Any | None" = None,
    ) -> MacFindReceipt:
        start = time.monotonic()
        query_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # --- Guard: index must exist ---
        if not self._db_path.exists():
            receipt = MacFindReceipt(
                query=query,
                namespace=namespace,
                status="index_missing",
                no_match_reason="Run `ollarma macfind reindex` first",
                query_at=query_at,
                latency_ms=0.0,
            )
            # Do not log receipts for missing index — nothing actionable to store.
            return receipt

        # --- Step 1: FTS5 BM25 retrieval (top 50 candidates) ---
        db = sqlite3.connect(str(self._db_path))
        try:
            rows = db.execute(
                """SELECT c.id, c.path, c.chunk_text, c.embedding,
                          bm25(chunks_fts) AS bm25_raw
                   FROM chunks_fts
                   JOIN chunks c ON c.id = chunks_fts.rowid
                   WHERE chunks_fts MATCH ?
                   ORDER BY bm25_raw
                   LIMIT 50""",
                (query,),
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS5 syntax error from user query — treat as no match
            rows = []
        except Exception:
            rows = []
            import logging
            logging.getLogger(__name__).warning("macfind FTS5 query failed", exc_info=True)
        finally:
            db.close()

        if not rows:
            latency_ms = (time.monotonic() - start) * 1000
            receipt = MacFindReceipt(
                query=query,
                namespace=namespace,
                status="no_match",
                no_match_reason="No FTS5 candidates",
                query_at=query_at,
                latency_ms=latency_ms,
            )
            _log_receipt(receipt)
            return receipt

        # --- Step 2: Embed query for cosine reranking ---
        try:
            query_emb: np.ndarray | None = np.array(_embed(query), dtype=np.float32)
        except Exception:
            query_emb = None  # Ollama unavailable — cosine defaults to 0.0

        # --- Step 3: Hybrid scoring ---
        # FTS5 bm25() returns negative values; negate so higher = better.
        bm25_scores = [-row[4] for row in rows]
        bm25_min = min(bm25_scores)
        bm25_max = max(bm25_scores)
        bm25_range = bm25_max - bm25_min if bm25_max != bm25_min else 1.0

        scored: list[tuple[float, float, float, str, str]] = []
        for row, raw_bm25 in zip(rows, bm25_scores):
            _chunk_id, path, chunk_text, emb_blob, _raw = row
            bm25_norm = (raw_bm25 - bm25_min) / bm25_range

            cosine = 0.0
            if query_emb is not None and emb_blob:
                chunk_emb = np.frombuffer(emb_blob, dtype=np.float32)
                cosine = _cosine(query_emb, chunk_emb)

            hybrid = 0.5 * bm25_norm + 0.5 * cosine
            scored.append((hybrid, bm25_norm, cosine, path, chunk_text))

        scored.sort(key=lambda x: -x[0])
        top = scored[:max_results]

        latency_ms = (time.monotonic() - start) * 1000

        # --- Step 4: Threshold check ---
        if not top or top[0][0] < confidence_threshold:
            reason = (
                f"Best hybrid score {top[0][0]:.3f} below threshold {confidence_threshold}"
                if top
                else "No scored results"
            )
            receipt = MacFindReceipt(
                query=query,
                namespace=namespace,
                status="no_match",
                no_match_reason=reason,
                query_at=query_at,
                latency_ms=latency_ms,
            )
            _log_receipt(receipt)
            return receipt

        # --- Step 5: Build hits ---
        hits_list: list[MacFindHit] = [
            MacFindHit(
                path=path,
                snippet=_extract_snippet(chunk_text, query),
                bm25_score=bm25_norm,
                cosine_score=cosine,
                hybrid_score=hybrid,
            )
            for hybrid, bm25_norm, cosine, path, chunk_text in top
        ]

        # --- Step 6: Antigence redaction (SAFE-01) ---
        redaction_events: list[str] = []
        hits_list = _apply_antigence_redaction(hits_list, gate, redaction_events)

        receipt = MacFindReceipt(
            query=query,
            namespace=namespace,
            status="ok",
            hits=tuple(hits_list),
            redaction_events=tuple(redaction_events),
            query_at=query_at,
            latency_ms=latency_ms,
        )
        _log_receipt(receipt)
        return receipt


# ---------------------------------------------------------------------------
# Receipt logging
# ---------------------------------------------------------------------------


def _log_receipt(
    receipt: MacFindReceipt, receipts_path: pathlib.Path | None = None
) -> None:
    """Append receipt as a JSONL line. Non-fatal on OSError.

    Uses module-level RECEIPTS_PATH by default so monkeypatching the module
    attribute in tests is reflected at call time.
    """
    import ollarma.macfind_searcher as _self

    path = receipts_path if receipts_path is not None else _self.RECEIPTS_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as f:
            f.write(orjson.dumps(receipt.model_dump()) + b"\n")
    except OSError:
        import logging
        logging.getLogger(__name__).debug("macfind receipt logging failed", exc_info=True)
