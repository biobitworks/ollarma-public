"""tests/test_macfind_searcher.py — unit tests for macfind_searcher.py.

No Ollama calls: _embed is mocked where the search path needs it.
All FTS5 database fixtures are built in-memory or under tmp_path.
"""
from __future__ import annotations

import pathlib
import sqlite3

import numpy as np
import orjson
import pytest

from ollarma.macfind_searcher import (
    MacFindHit,
    MacFindReceipt,
    MacFindSearcher,
    _cosine,
    _extract_snippet,
    _log_receipt,
)


# ---------------------------------------------------------------------------
# _cosine tests
# ---------------------------------------------------------------------------


def test_cosine_identical():
    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert _cosine(a, a) == pytest.approx(1.0)


def test_cosine_orthogonal():
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert _cosine(a, b) == pytest.approx(0.0)


def test_cosine_zero_vector():
    z = np.array([0.0, 0.0], dtype=np.float32)
    assert _cosine(z, z) == 0.0


# ---------------------------------------------------------------------------
# _extract_snippet tests
# ---------------------------------------------------------------------------


def test_extract_snippet_finds_term():
    text = "hello world foo bar"
    snippet = _extract_snippet(text, "world")
    assert "world" in snippet


def test_extract_snippet_max_len():
    text = "a" * 1000
    snippet = _extract_snippet(text, "z")  # term not found → head of text
    # max_len=200 + up to 3 chars for "..."
    assert len(snippet) <= 203


def test_extract_snippet_no_term_returns_head():
    text = "the quick brown fox"
    snippet = _extract_snippet(text, "zzz")
    assert snippet == text[:200].strip()


def test_extract_snippet_prefix_ellipsis():
    # Term is beyond first 50 chars → prefix "..." should appear
    text = "x" * 100 + "TARGET" + "y" * 100
    snippet = _extract_snippet(text, "target")
    assert snippet.startswith("...")


# ---------------------------------------------------------------------------
# MacFindSearcher — index_missing path
# ---------------------------------------------------------------------------


def test_search_index_missing(tmp_path):
    db_path = tmp_path / "nonexistent.db"
    searcher = MacFindSearcher(db_path=db_path)
    receipt = searcher.search("hello")
    assert receipt.status == "index_missing"
    assert receipt.hits == ()


def test_search_index_missing_no_receipt_logged(tmp_path):
    db_path = tmp_path / "nonexistent.db"
    receipts_path = tmp_path / "receipts.jsonl"
    searcher = MacFindSearcher(db_path=db_path)
    searcher.search("hello")
    # index_missing receipts are NOT logged (nothing actionable)
    assert not receipts_path.exists()


# ---------------------------------------------------------------------------
# MacFindReceipt — field validation
# ---------------------------------------------------------------------------


def test_receipt_has_required_fields():
    receipt = MacFindReceipt(
        query="test query",
        namespace="ns1",
        status="ok",
        query_at="2026-04-15T12:00:00Z",
        latency_ms=42.5,
    )
    assert receipt.query == "test query"
    assert receipt.namespace == "ns1"
    assert receipt.query_at == "2026-04-15T12:00:00Z"
    assert receipt.latency_ms == 42.5


def test_receipt_model_dump_json_serializable():
    receipt = MacFindReceipt(
        query="q",
        namespace="ns",
        status="no_match",
        no_match_reason="nothing",
        query_at="2026-04-15T00:00:00Z",
        latency_ms=1.0,
    )
    dumped = receipt.model_dump()
    # orjson must be able to serialize it
    serialized = orjson.dumps(dumped)
    parsed = orjson.loads(serialized)
    assert parsed["status"] == "no_match"
    assert parsed["query"] == "q"


# ---------------------------------------------------------------------------
# Fixture: minimal FTS5 database
# ---------------------------------------------------------------------------


def _make_test_db(db_path: pathlib.Path, chunk_text: str = "hello world test content") -> None:
    """Create a minimal SQLite FTS5 database with one chunk row."""
    db = sqlite3.connect(str(db_path))
    db.execute(
        "CREATE TABLE chunks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "path TEXT, chunk_index INTEGER, chunk_text TEXT, embedding BLOB)"
    )
    db.execute(
        "CREATE VIRTUAL TABLE chunks_fts USING fts5("
        "path, chunk_text, content=chunks, content_rowid=id)"
    )
    zero_emb = np.zeros(768, dtype=np.float32).tobytes()
    db.execute(
        "INSERT INTO chunks (path, chunk_index, chunk_text, embedding) VALUES (?,?,?,?)",
        ("test.txt", 0, chunk_text, zero_emb),
    )
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# MacFindSearcher — no_match below threshold
# ---------------------------------------------------------------------------


def test_no_match_below_threshold(tmp_path, monkeypatch):
    """With zero embedding (cosine=0) and a single BM25 candidate,
    hybrid = 0.5*bm25_norm + 0.5*0.0. With one row bm25_norm normalizes to 0
    (min==max → range forced to 1.0, so norm=(score-min)/1.0 = 0).
    Result: hybrid=0.0 < default threshold 0.25 → status=="no_match".
    """
    db_path = tmp_path / "index.db"
    _make_test_db(db_path)

    # Mock _embed to return a near-zero vector so cosine ≈ 0
    import ollarma.macfind_searcher as mod

    monkeypatch.setattr(mod, "_embed", lambda _: [0.0] * 768)

    searcher = MacFindSearcher(db_path=db_path)
    receipt = searcher.search("hello", confidence_threshold=0.25)
    # With a single row: bm25_norm=0 (range=1, min=max) and cosine=0 → hybrid=0.0 < 0.25
    assert receipt.status == "no_match"


# ---------------------------------------------------------------------------
# JSONL receipt logging
# ---------------------------------------------------------------------------


def test_receipt_logged_to_jsonl(tmp_path, monkeypatch):
    """After a search that produces no_match (no FTS candidates), receipt is logged."""
    db_path = tmp_path / "index.db"
    receipts_path = tmp_path / "receipts.jsonl"
    _make_test_db(db_path)

    import ollarma.macfind_searcher as mod

    monkeypatch.setattr(mod, "_embed", lambda _: [0.0] * 768)
    monkeypatch.setattr(mod, "RECEIPTS_PATH", receipts_path)

    searcher = MacFindSearcher(db_path=db_path)
    searcher.search("hello", confidence_threshold=0.25)

    # Receipt file should have been written
    assert receipts_path.exists()
    lines = receipts_path.read_bytes().strip().split(b"\n")
    assert len(lines) >= 1
    parsed = orjson.loads(lines[-1])
    assert parsed["query"] == "hello"
    assert parsed["status"] in ("no_match", "ok")


def test_log_receipt_non_fatal_on_bad_path():
    """_log_receipt must not raise even if the path is unwritable."""
    bad_path = pathlib.Path("/proc/definitely_does_not_exist/receipts.jsonl")
    receipt = MacFindReceipt(
        query="q",
        namespace="ns",
        status="ok",
        query_at="2026-04-15T00:00:00Z",
        latency_ms=0.0,
    )
    # Should not raise
    _log_receipt(receipt, receipts_path=bad_path)
