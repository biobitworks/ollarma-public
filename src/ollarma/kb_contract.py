"""kb_contract.py -- Typed knowledge-base contract models and path resolution."""
from __future__ import annotations

import pathlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _normalize_repo_relative_path(value: str, *, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")

    pure = pathlib.PurePosixPath(text)
    if pure.is_absolute():
        raise ValueError(f"{field_name} must be repo-relative, got absolute path: {value!r}")
    if ".." in pure.parts:
        raise ValueError(f"{field_name} must not escape the repo root: {value!r}")

    normalized = pure.as_posix()
    if normalized == ".":
        return normalized
    return normalized.rstrip("/")


class DatabaseConfig(BaseModel):
    """Typed structured-data reference kept backward-compatible with dict-like access."""

    model_config = ConfigDict(frozen=True, extra="allow")

    type: str = ""
    host: str = "localhost"
    port: int = 8531
    db: str = ""
    path: str = ""
    authority: Literal["canonical", "reference"] = "reference"
    read_only: bool = True

    def get(self, key: str, default=None):
        """Provide dict-like reads for existing fleet tool code paths."""
        if hasattr(self, key):
            return getattr(self, key)
        extra = getattr(self, "__pydantic_extra__", None) or {}
        return extra.get(key, default)


class KnowledgeBaseSource(BaseModel):
    """One declared KB source rooted inside the adapter project."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: Literal["documents", "workflow", "structured_snapshot", "notes", "tests", "config"] = "documents"
    authority: Literal["canonical", "reference"] = "reference"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    recursive: bool = True
    note: str = ""  # optional human annotation for the source

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("source path must not be empty")
        return text.rstrip("/") or "."


class KnowledgeBaseConfig(BaseModel):
    """Top-level KB declaration attached to an adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_root: str = ".ollarma/kb"
    freshness_hours: int = Field(default=24, ge=1, le=24 * 30)
    stale_behavior: Literal["escalate", "block", "warn"] = "escalate"
    sources: tuple[KnowledgeBaseSource, ...] = ()

    @field_validator("artifact_root")
    @classmethod
    def _validate_artifact_root(cls, value: str) -> str:
        return _normalize_repo_relative_path(value, field_name="artifact_root")


class KBPathRef(BaseModel):
    """Stable resolved path reference for service/read surfaces."""

    model_config = ConfigDict(frozen=True)

    absolute_path: str
    repo_relative: str
    exists: bool


class ResolvedKnowledgeBaseSource(BaseModel):
    """Resolved service-facing view of one KB source."""

    model_config = ConfigDict(frozen=True)

    kind: str
    authority: str
    recursive: bool
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    path_ref: KBPathRef


class ProjectKnowledgeContract(BaseModel):
    """Resolved project KB contract for service/read surfaces."""

    model_config = ConfigDict(frozen=True)

    project: str
    project_root: str
    artifact_root: KBPathRef
    freshness_hours: int
    stale_behavior: str
    status: Literal["ready", "blocked"]
    reason_code: str | None = None
    sources: tuple[ResolvedKnowledgeBaseSource, ...]
    databases: tuple[DatabaseConfig, ...]


def _resolve_repo_path(project_root: pathlib.Path, raw_path: str) -> pathlib.Path:
    candidate = pathlib.Path(raw_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (project_root / candidate).resolve()
    if not resolved.is_relative_to(project_root):
        raise ValueError(
            f"KB path {raw_path!r} escapes project root {project_root}"
        )
    return resolved


def _to_path_ref(project_root: pathlib.Path, resolved: pathlib.Path) -> KBPathRef:
    return KBPathRef(
        absolute_path=str(resolved),
        repo_relative=resolved.relative_to(project_root).as_posix(),
        exists=resolved.exists(),
    )


def build_project_knowledge_contract(
    project_name: str,
    project_root: str,
    knowledge_base: KnowledgeBaseConfig,
    databases: list[DatabaseConfig] | tuple[DatabaseConfig, ...] | None = None,
) -> ProjectKnowledgeContract:
    """Resolve repo-local KB paths into a service-facing contract."""
    root = pathlib.Path(project_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Project root does not exist: {project_root}")

    artifact_root = _resolve_repo_path(root, knowledge_base.artifact_root)
    resolved_sources: list[ResolvedKnowledgeBaseSource] = []
    for source in knowledge_base.sources:
        resolved = _resolve_repo_path(root, source.path)
        resolved_sources.append(
            ResolvedKnowledgeBaseSource(
                kind=source.kind,
                authority=source.authority,
                recursive=source.recursive,
                include=source.include,
                exclude=source.exclude,
                path_ref=_to_path_ref(root, resolved),
            )
        )

    status = "ready"
    reason_code = None
    if not resolved_sources:
        status = "blocked"
        reason_code = "KB_SOURCES_UNDECLARED"
    elif not all(source.path_ref.exists for source in resolved_sources):
        status = "blocked"
        reason_code = "KB_SOURCE_MISSING"
    elif not artifact_root.exists():
        status = "blocked"
        reason_code = "KB_NOT_BUILT"

    return ProjectKnowledgeContract(
        project=project_name,
        project_root=str(root),
        artifact_root=_to_path_ref(root, artifact_root),
        freshness_hours=knowledge_base.freshness_hours,
        stale_behavior=knowledge_base.stale_behavior,
        status=status,
        reason_code=reason_code,
        sources=tuple(resolved_sources),
        databases=tuple(databases or ()),
    )
