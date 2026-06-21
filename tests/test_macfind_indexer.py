"""tests/test_macfind_indexer.py — tests for macfind_config + macfind_indexer."""
from __future__ import annotations
import pathlib
import sqlite3

import orjson
import pytest


# ---------------------------------------------------------------------------
# Task 1: MacFindConfig tests
# ---------------------------------------------------------------------------


def test_load_config_missing_raises(tmp_path):
    """No config file at path → ValueError with MACFIND_NOT_CONFIGURED."""
    from ollarma.macfind_config import load_macfind_config, MACFIND_NOT_CONFIGURED

    missing = tmp_path / "no_such_file.json"
    with pytest.raises(ValueError, match=MACFIND_NOT_CONFIGURED):
        load_macfind_config(missing)


def test_load_config_basic(tmp_path):
    """Valid JSON config → MacFindConfig with correct approved_dirs."""
    from ollarma.macfind_config import load_macfind_config

    config_file = tmp_path / "macfind.json"
    config_file.write_bytes(orjson.dumps({"approved_dirs": [str(tmp_path)]}))

    cfg = load_macfind_config(config_file)
    assert len(cfg.approved_dirs) == 1
    assert cfg.approved_dirs[0] == tmp_path.resolve()


def test_load_config_defaults(tmp_path):
    """Config with only approved_dirs → default threshold=0.25 and max_results=10."""
    from ollarma.macfind_config import load_macfind_config

    config_file = tmp_path / "macfind.json"
    config_file.write_bytes(orjson.dumps({"approved_dirs": [str(tmp_path)]}))

    cfg = load_macfind_config(config_file)
    assert cfg.confidence_threshold == 0.25
    assert cfg.max_results == 10


def test_load_config_empty_dirs_raises(tmp_path):
    """Config with empty approved_dirs → ValueError with MACFIND_NOT_CONFIGURED."""
    from ollarma.macfind_config import load_macfind_config, MACFIND_NOT_CONFIGURED

    config_file = tmp_path / "macfind.json"
    config_file.write_bytes(orjson.dumps({"approved_dirs": []}))

    with pytest.raises(ValueError, match=MACFIND_NOT_CONFIGURED):
        load_macfind_config(config_file)


# ---------------------------------------------------------------------------
# Task 2: File walker tests (no Ollama required)
# ---------------------------------------------------------------------------


def test_walk_skips_dotgit(tmp_path):
    """Files inside .git/ inside an approved dir are NOT yielded."""
    from ollarma.macfind_indexer import _walk_approved

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    secret = git_dir / "config"
    secret.write_text("git config contents")

    # also put a normal file at the top level
    normal = tmp_path / "README.md"
    normal.write_text("hello")

    yielded = list(_walk_approved((tmp_path,)))
    paths = [p.name for p in yielded]
    assert "config" not in paths
    assert "README.md" in paths


def test_walk_skips_env_file(tmp_path):
    """.env file in approved dir is NOT yielded."""
    from ollarma.macfind_indexer import _walk_approved

    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=abc")

    normal = tmp_path / "main.py"
    normal.write_text("print('hi')")

    yielded = list(_walk_approved((tmp_path,)))
    names = [p.name for p in yielded]
    assert ".env" not in names
    assert "main.py" in names


def test_walk_skips_large_binary(tmp_path):
    """File larger than 1MB is NOT yielded."""
    from ollarma.macfind_indexer import _walk_approved

    large = tmp_path / "big_file.bin"
    # Write 1MB + 1 byte of non-null data (to avoid binary detection)
    large.write_bytes(b"A" * (1_048_576 + 1))

    yielded = list(_walk_approved((tmp_path,)))
    names = [p.name for p in yielded]
    assert "big_file.bin" not in names


def test_walk_skips_binary_content(tmp_path):
    """File with null bytes in first 512 bytes is NOT yielded."""
    from ollarma.macfind_indexer import _walk_approved

    binary = tmp_path / "data.bin"
    binary.write_bytes(b"header\x00data")

    normal = tmp_path / "text.txt"
    normal.write_text("hello world")

    yielded = list(_walk_approved((tmp_path,)))
    names = [p.name for p in yielded]
    assert "data.bin" not in names
    assert "text.txt" in names


def test_chunk_text_size(tmp_path):
    """A 1000-char text is split into chunks each ≤ 512 chars."""
    from ollarma.macfind_indexer import _chunk_text

    text = "x" * 1000
    chunks = _chunk_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 512


def test_embed_delegates_to_shared_pinned_embedding_path(monkeypatch):
    """macfind uses the RTB-REQ-25 shared embed path instead of keep_alive=0s."""
    import ollarma.macfind_indexer as mod
    from ollarma.embeddings import EMBED_MODEL, EmbedResult

    seen = {}

    def fake_embed_text(text: str) -> EmbedResult:
        seen["text"] = text
        return EmbedResult(model=EMBED_MODEL, status="ok", embedding=(0.1, 0.2), dim=2)

    monkeypatch.setattr(mod, "embed_text", fake_embed_text)

    assert mod._embed("needle") == [0.1, 0.2]
    assert seen["text"] == "needle"


def test_embed_degraded_raises_loud_reason(monkeypatch):
    """macfind fails loudly when the shared embed path reports degraded."""
    import ollarma.macfind_indexer as mod
    from ollarma.embeddings import EMBED_BRIDGE_DOWN, EMBED_MODEL, EmbedResult

    def fake_embed_text(text: str) -> EmbedResult:
        return EmbedResult(
            model=EMBED_MODEL,
            status="degraded",
            reason_code=EMBED_BRIDGE_DOWN,
            detail="bridge down",
        )

    monkeypatch.setattr(mod, "embed_text", fake_embed_text)

    with pytest.raises(RuntimeError, match=EMBED_BRIDGE_DOWN):
        mod._embed("needle")


def test_indexer_creates_tables(tmp_path):
    """MacFindIndexer with tmp db path creates the expected SQLite tables."""
    from ollarma.macfind_indexer import MacFindIndexer

    db_path = tmp_path / "test_index.db"
    indexer = MacFindIndexer(db_path=db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        vtables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='shadow' OR name LIKE '%fts%'"
        ).fetchall()}
    finally:
        conn.close()
        indexer._db.close()

    assert "chunks" in tables
    assert "meta" in tables
