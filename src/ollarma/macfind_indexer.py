"""macfind_indexer.py — File walker and SQLite FTS5 + embedding indexer for find_on_mac."""
from __future__ import annotations

import fnmatch
import os
import pathlib
import sqlite3
import struct
from typing import Iterator

from ollarma.embeddings import EMBED_MODEL, embed_text
from ollarma.macfind_config import MacFindConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".tox", "dist", "build"}
SKIP_EXTENSIONS = {".pyc", ".pyo", ".class", ".o", ".a", ".so", ".dylib"}
SKIP_FILENAMES = {".env", ".DS_Store"}
SECRET_PATTERNS = ("*.key", "*.pem", "*.p12", "*.pfx", "*.secret", "id_rsa", "id_ed25519")
MAX_FILE_SIZE = 1_048_576  # 1MB

INDEX_DB_PATH = pathlib.Path.home() / ".cache" / "ollarma" / "macfind" / "index.db"

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64

# ---------------------------------------------------------------------------
# File walker helpers
# ---------------------------------------------------------------------------


def _is_binary(path: pathlib.Path) -> bool:
    """Return True if the file appears binary (first 512 bytes contain null bytes)."""
    try:
        return b"\x00" in path.read_bytes()[:512]
    except OSError:
        return True


def _should_skip(path: pathlib.Path) -> bool:
    """Return True if this file should be excluded from indexing."""
    name = path.name
    if name in SKIP_FILENAMES:
        return True
    if any(fnmatch.fnmatch(name, pat) for pat in SECRET_PATTERNS):
        return True
    if path.suffix in SKIP_EXTENSIONS:
        return True
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return True
    except OSError:
        return True
    if _is_binary(path):
        return True
    return False


def _walk_approved(dirs: tuple[pathlib.Path, ...]) -> Iterator[pathlib.Path]:
    """Walk approved dirs without following symlinks; yield indexable text files.

    Uses followlinks=False to prevent symlink loops. Also skips symlinked files
    individually via is_symlink() check. Deduplicates via inode tracking.
    """
    seen_inodes: set[int] = set()
    for root_dir in dirs:
        for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=False):
            # Prune skip dirs in-place so os.walk does not descend into them
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fname in filenames:
                fpath = pathlib.Path(dirpath) / fname
                if fpath.is_symlink():
                    continue  # skip symlinked files entirely
                try:
                    inode = fpath.stat().st_ino
                except OSError:
                    continue
                if inode in seen_inodes:
                    continue
                seen_inodes.add(inode)
                if not _should_skip(fpath):
                    yield fpath


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks of CHUNK_SIZE chars with CHUNK_OVERLAP."""
    if not text:
        return [""]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


def _embed(text: str) -> list[float]:
    """Embed text through Ollarma's shared pinned embedding path.

    Raises RuntimeError with a LOUD degraded reason when embedding is unavailable.
    """
    result = embed_text(text)
    if result.status != "ok":
        reason = result.reason_code or "EMBED_DEGRADED"
        detail = result.detail or "embedding unavailable"
        raise RuntimeError(f"{reason}: {detail}")
    return list(result.embedding)


def _floats_to_bytes(floats: list[float]) -> bytes:
    """Pack a list of float32 values to bytes for SQLite BLOB storage."""
    return struct.pack(f"{len(floats)}f", *floats)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding BLOB
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    path, chunk_text, content=chunks, content_rowid=id
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------


class MacFindIndexer:
    """SQLite FTS5 + embedding indexer for find_on_mac approved directories."""

    def __init__(self, db_path: pathlib.Path = INDEX_DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path))
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript(_SCHEMA_SQL)
        self._db.commit()

    def build_index(
        self,
        config: MacFindConfig,
        *,
        progress_cb=None,
    ) -> dict:
        """Walk approved dirs and (re)build the full index.

        Returns stats dict: {"files": int, "chunks": int, "skipped": int, "errors": list}.
        """
        # Clear existing index
        self._db.execute("DELETE FROM chunks")
        self._db.execute("DELETE FROM chunks_fts")
        self._db.commit()

        stats: dict = {"files": 0, "chunks": 0, "skipped": 0, "errors": []}

        for fpath in _walk_approved(config.approved_dirs):
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
                chunks = _chunk_text(text)
                for i, chunk in enumerate(chunks):
                    emb = _embed(chunk)
                    emb_bytes = _floats_to_bytes(emb)
                    self._db.execute(
                        "INSERT INTO chunks (path, chunk_index, chunk_text, embedding)"
                        " VALUES (?, ?, ?, ?)",
                        (str(fpath), i, chunk, emb_bytes),
                    )
                self._db.commit()
                stats["files"] += 1
                stats["chunks"] += len(chunks)
            except Exception as exc:
                stats["errors"].append({"path": str(fpath), "error": str(exc)})
                stats["skipped"] += 1
            if progress_cb is not None:
                progress_cb(fpath)

        # Rebuild FTS5 content index from chunks table
        self._db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        self._db.commit()

        return stats
