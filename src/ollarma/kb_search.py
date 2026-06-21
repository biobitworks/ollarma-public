"""kb_search.py -- Read-only SQLite-backed KB status and search helpers."""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3

import orjson
from pydantic import BaseModel, ConfigDict

from ollarma.kb_contract import ProjectKnowledgeContract


class KBSearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    document_id: str
    path: str
    chunk_index: int
    authority: str
    source_kind: str
    score: float
    text: str
    tags: tuple[str, ...]


class KBStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    project: str
    status: str
    reason_code: str | None = None
    freshness_hours: int
    stale_behavior: str
    built_at: str | None = None
    artifact_root: str
    search_db_path: str
    document_count: int = 0
    chunk_count: int = 0


class KBSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    project: str
    query: str
    status: str
    reason_code: str | None = None
    hit_count: int = 0
    hits: tuple[KBSearchHit, ...] = ()


def _search_db_path(contract: ProjectKnowledgeContract) -> pathlib.Path:
    return pathlib.Path(contract.artifact_root.absolute_path) / "search.sqlite"


def _receipts_path(contract: ProjectKnowledgeContract) -> pathlib.Path:
    return pathlib.Path(contract.artifact_root.absolute_path) / "receipts.jsonl"


def _read_last_receipt(receipts_path: pathlib.Path) -> dict | None:
    if not receipts_path.exists():
        return None
    lines = [line for line in receipts_path.read_bytes().splitlines() if line.strip()]
    if not lines:
        return None
    return orjson.loads(lines[-1])


def load_kb_status(contract: ProjectKnowledgeContract) -> KBStatus:
    """Return read-only KB status from the contract plus artifact files."""
    search_path = _search_db_path(contract)
    repo_root = pathlib.Path(contract.project_root)
    search_ref = search_path.relative_to(repo_root).as_posix()
    if contract.status == "blocked" and contract.reason_code != "KB_NOT_BUILT":
        return KBStatus(
            project=contract.project,
            status=contract.status,
            reason_code=contract.reason_code,
            freshness_hours=contract.freshness_hours,
            stale_behavior=contract.stale_behavior,
            artifact_root=contract.artifact_root.repo_relative,
            search_db_path=search_ref,
        )

    if not search_path.exists():
        return KBStatus(
            project=contract.project,
            status="blocked",
            reason_code="KB_NOT_BUILT",
            freshness_hours=contract.freshness_hours,
            stale_behavior=contract.stale_behavior,
            artifact_root=contract.artifact_root.repo_relative,
            search_db_path=search_ref,
        )

    receipt = _read_last_receipt(_receipts_path(contract))
    built_at = receipt.get("built_at") if receipt else None
    if receipt is None or built_at is None:
        return KBStatus(
            project=contract.project,
            status="blocked",
            reason_code="KB_NOT_BUILT",
            freshness_hours=contract.freshness_hours,
            stale_behavior=contract.stale_behavior,
            artifact_root=contract.artifact_root.repo_relative,
            search_db_path=search_ref,
        )

    status = "ready"
    reason_code = None
    if built_at is not None:
        built_dt = dt.datetime.fromisoformat(built_at.replace("Z", "+00:00"))
        age = dt.datetime.now(dt.timezone.utc) - built_dt.astimezone(dt.timezone.utc)
        if age > dt.timedelta(hours=contract.freshness_hours):
            status = "stale"
            reason_code = "KB_STALE"

    conn = sqlite3.connect(f"file:{search_path}?mode=ro", uri=True)
    try:
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        conn.close()

    return KBStatus(
        project=contract.project,
        status=status,
        reason_code=reason_code,
        freshness_hours=contract.freshness_hours,
        stale_behavior=contract.stale_behavior,
        built_at=built_at,
        artifact_root=contract.artifact_root.repo_relative,
        search_db_path=search_ref,
        document_count=doc_count,
        chunk_count=chunk_count,
    )


def search_kb(
    contract: ProjectKnowledgeContract,
    query: str,
    *,
    limit: int = 5,
) -> KBSearchResult:
    """Search KB chunks read-only using deterministic substring ranking."""
    status = load_kb_status(contract)
    if status.status == "blocked":
        return KBSearchResult(
            project=contract.project,
            query=query,
            status=status.status,
            reason_code=status.reason_code,
        )
    if status.status == "stale" and contract.stale_behavior == "block":
        return KBSearchResult(
            project=contract.project,
            query=query,
            status="blocked",
            reason_code=status.reason_code,
        )

    search_path = _search_db_path(contract)
    conn = sqlite3.connect(f"file:{search_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                c.chunk_id,
                c.document_id,
                c.path,
                c.chunk_index,
                d.authority,
                d.source_kind,
                c.text,
                c.tags_json,
                instr(lower(c.text), lower(?)) AS position
            FROM chunks AS c
            JOIN documents AS d ON d.document_id = c.document_id
            WHERE position > 0
            ORDER BY position ASC, c.path ASC, c.chunk_index ASC
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    finally:
        conn.close()

    hits = tuple(
        KBSearchHit(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            path=row["path"],
            chunk_index=row["chunk_index"],
            authority=row["authority"],
            source_kind=row["source_kind"],
            score=float(1.0 / row["position"]),
            text=row["text"],
            tags=tuple(orjson.loads(row["tags_json"])),
        )
        for row in rows
    )
    return KBSearchResult(
        project=contract.project,
        query=query,
        status=status.status,
        reason_code=status.reason_code,
        hit_count=len(hits),
        hits=hits,
    )
