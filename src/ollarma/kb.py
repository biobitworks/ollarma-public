"""kb.py -- Deterministic repo-local KB artifact builder."""
from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import pathlib
import sqlite3
from typing import Iterable

import orjson
from pydantic import BaseModel, ConfigDict

from ollarma.evidence import canonical_hash
from ollarma.kb_contract import ProjectKnowledgeContract, ResolvedKnowledgeBaseSource


_TEXT_FILE_SUFFIXES = {
    ".md",
    ".mdx",
    ".txt",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".bash",
    ".ipynb",
}
_MAX_CHUNK_CHARS = 1200


class KBDocumentRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    source_kind: str
    authority: str
    path: str
    content_digest: str
    size_bytes: int
    tags: tuple[str, ...]


class KBChunkRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    document_id: str
    path: str
    chunk_index: int
    content_digest: str
    text: str
    tags: tuple[str, ...]


class KBBuildManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str
    project: str
    artifact_root: str
    freshness_hours: int
    stale_behavior: str
    source_count: int
    document_count: int
    chunk_count: int
    document_set_hash: str
    chunk_set_hash: str


class KBBuildReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    built_at: str
    project: str
    artifact_root: str
    reason: str
    source_count: int
    document_count: int
    chunk_count: int
    manifest_hash: str
    source_paths: tuple[str, ...]


class KBBuildArtifacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest: KBBuildManifest
    documents: tuple[KBDocumentRecord, ...]
    chunks: tuple[KBChunkRecord, ...]
    tags: dict[str, tuple[str, ...]]
    receipt: KBBuildReceipt
    artifact_root: str
    manifest_path: str
    documents_path: str
    chunks_path: str
    tags_path: str
    receipts_path: str
    search_db_path: str


def _read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _text_digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _should_include(path: pathlib.Path, source_root: pathlib.Path, source: ResolvedKnowledgeBaseSource) -> bool:
    if path.is_dir():
        return False
    if not path.exists():
        return False
    if path.suffix.lower() not in _TEXT_FILE_SUFFIXES:
        return False

    relative = path.relative_to(source_root).as_posix()
    if source.include and not any(fnmatch.fnmatch(relative, pattern) for pattern in source.include):
        return False
    if source.exclude and any(fnmatch.fnmatch(relative, pattern) for pattern in source.exclude):
        return False
    return True


def _iter_source_files(project_root: pathlib.Path, source: ResolvedKnowledgeBaseSource) -> list[pathlib.Path]:
    root = project_root / source.path_ref.repo_relative
    if not root.exists():
        return []
    if root.is_file():
        return [root] if _should_include(root, root.parent, source) else []

    pattern_iter: Iterable[pathlib.Path]
    if source.recursive:
        pattern_iter = root.rglob("*")
    else:
        pattern_iter = root.glob("*")

    files = [path for path in pattern_iter if _should_include(path, root, source)]
    return sorted(files, key=lambda path: path.relative_to(project_root).as_posix())


def _derive_tags(path: str, source: ResolvedKnowledgeBaseSource) -> tuple[str, ...]:
    relative = pathlib.PurePosixPath(path)
    suffix = relative.suffix.lstrip(".").lower() or "none"
    tags = {
        f"kind:{source.kind}",
        f"authority:{source.authority}",
        f"ext:{suffix}",
    }
    if relative.parts:
        tags.add(f"topdir:{relative.parts[0]}")
    if "tests" in relative.parts:
        tags.add("role:test")
    if relative.suffix == ".py":
        tags.add("role:script")
    if relative.suffix == ".ipynb":
        tags.add("role:notebook")
    if relative.suffix in {".md", ".mdx", ".txt"}:
        tags.add("role:docs")
    if relative.name.startswith("test_"):
        tags.add("role:test")
    return tuple(sorted(tags))


def _chunk_text(text: str) -> list[str]:
    if len(text) <= _MAX_CHUNK_CHARS:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current and current_len + len(line) > _MAX_CHUNK_CHARS:
            chunks.append("".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks or [text]


def _write_json(path: pathlib.Path, payload: dict | list) -> None:
    path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _write_jsonl(path: pathlib.Path, rows: Iterable[BaseModel]) -> None:
    serialized = b"".join(
        orjson.dumps(row.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS) + b"\n"
        for row in rows
    )
    path.write_bytes(serialized)


def _write_search_db(
    path: pathlib.Path,
    manifest: KBBuildManifest,
    documents: list[KBDocumentRecord],
    chunks: list[KBChunkRecord],
) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE documents (
                document_id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                authority TEXT NOT NULL,
                content_digest TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                tags_json TEXT NOT NULL
            );
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content_digest TEXT NOT NULL,
                text TEXT NOT NULL,
                tags_json TEXT NOT NULL
            );
            CREATE INDEX idx_chunks_path ON chunks(path, chunk_index);
            CREATE INDEX idx_documents_path ON documents(path);
            """
        )
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("project", manifest.project),
                ("artifact_root", manifest.artifact_root),
                ("freshness_hours", str(manifest.freshness_hours)),
                ("stale_behavior", manifest.stale_behavior),
                ("document_count", str(manifest.document_count)),
                ("chunk_count", str(manifest.chunk_count)),
                ("schema_version", manifest.schema_version),
            ],
        )
        conn.executemany(
            """
            INSERT INTO documents(
                document_id, path, source_kind, authority, content_digest, size_bytes, tags_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row.document_id,
                    row.path,
                    row.source_kind,
                    row.authority,
                    row.content_digest,
                    row.size_bytes,
                    orjson.dumps(list(row.tags)).decode("utf-8"),
                )
                for row in documents
            ],
        )
        conn.executemany(
            """
            INSERT INTO chunks(
                chunk_id, document_id, path, chunk_index, content_digest, text, tags_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row.chunk_id,
                    row.document_id,
                    row.path,
                    row.chunk_index,
                    row.content_digest,
                    row.text,
                    orjson.dumps(list(row.tags)).decode("utf-8"),
                )
                for row in chunks
            ],
        )
        conn.commit()
    finally:
        conn.close()


def build_kb_artifacts(
    contract: ProjectKnowledgeContract,
    *,
    reason: str = "manual",
) -> KBBuildArtifacts:
    """Materialize deterministic KB artifacts under the repo-local artifact root."""
    if contract.reason_code in {"KB_SOURCES_UNDECLARED", "KB_SOURCE_MISSING"}:
        raise ValueError(f"KB build blocked: {contract.reason_code}")

    project_root = pathlib.Path(contract.project_root)
    artifact_root = pathlib.Path(contract.artifact_root.absolute_path)
    artifact_root.mkdir(parents=True, exist_ok=True)

    documents: list[KBDocumentRecord] = []
    chunks: list[KBChunkRecord] = []
    tags_map: dict[str, tuple[str, ...]] = {}

    for source in contract.sources:
        for file_path in _iter_source_files(project_root, source):
            repo_relative = file_path.relative_to(project_root).as_posix()
            text = _read_text(file_path)
            digest = _text_digest(text)
            tags = _derive_tags(repo_relative, source)
            document_id = f"doc:{canonical_hash({'path': repo_relative, 'kind': source.kind, 'authority': source.authority})}"
            doc = KBDocumentRecord(
                document_id=document_id,
                source_kind=source.kind,
                authority=source.authority,
                path=repo_relative,
                content_digest=digest,
                size_bytes=len(text.encode("utf-8")),
                tags=tags,
            )
            documents.append(doc)
            tags_map[document_id] = tags

            for index, chunk_text in enumerate(_chunk_text(text)):
                chunk_digest = _text_digest(chunk_text)
                chunk_id = f"chunk:{canonical_hash({'document_id': document_id, 'chunk_index': index, 'content_digest': chunk_digest})}"
                chunks.append(
                    KBChunkRecord(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        path=repo_relative,
                        chunk_index=index,
                        content_digest=chunk_digest,
                        text=chunk_text,
                        tags=tags,
                    )
                )

    documents.sort(key=lambda row: (row.path, row.document_id))
    chunks.sort(key=lambda row: (row.path, row.chunk_index, row.chunk_id))
    tags_payload = {doc_id: list(tags) for doc_id, tags in sorted(tags_map.items())}

    manifest = KBBuildManifest(
        schema_version="1",
        project=contract.project,
        artifact_root=contract.artifact_root.repo_relative,
        freshness_hours=contract.freshness_hours,
        stale_behavior=contract.stale_behavior,
        source_count=len(contract.sources),
        document_count=len(documents),
        chunk_count=len(chunks),
        document_set_hash=f"sha256:{canonical_hash([row.model_dump(mode='json') for row in documents])}" if documents else "sha256:empty",
        chunk_set_hash=f"sha256:{canonical_hash([row.model_dump(mode='json') for row in chunks])}" if chunks else "sha256:empty",
    )

    manifest_path = artifact_root / "manifest.json"
    documents_path = artifact_root / "documents.jsonl"
    chunks_path = artifact_root / "chunks.jsonl"
    tags_path = artifact_root / "tags.json"
    receipts_path = artifact_root / "receipts.jsonl"
    search_db_path = artifact_root / "search.sqlite"

    _write_json(manifest_path, manifest.model_dump(mode="json"))
    _write_jsonl(documents_path, documents)
    _write_jsonl(chunks_path, chunks)
    _write_json(tags_path, tags_payload)
    _write_search_db(search_db_path, manifest, documents, chunks)

    receipt = KBBuildReceipt(
        built_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        project=contract.project,
        artifact_root=contract.artifact_root.repo_relative,
        reason=reason,
        source_count=len(contract.sources),
        document_count=len(documents),
        chunk_count=len(chunks),
        manifest_hash=f"sha256:{canonical_hash(manifest.model_dump(mode='json'))}",
        source_paths=tuple(source.path_ref.repo_relative for source in contract.sources),
    )
    with receipts_path.open("ab") as handle:
        handle.write(
            orjson.dumps(receipt.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS) + b"\n"
        )

    return KBBuildArtifacts(
        manifest=manifest,
        documents=tuple(documents),
        chunks=tuple(chunks),
        tags={key: tuple(value) for key, value in tags_payload.items()},
        receipt=receipt,
        artifact_root=str(artifact_root),
        manifest_path=str(manifest_path),
        documents_path=str(documents_path),
        chunks_path=str(chunks_path),
        tags_path=str(tags_path),
        receipts_path=str(receipts_path),
        search_db_path=str(search_db_path),
    )
