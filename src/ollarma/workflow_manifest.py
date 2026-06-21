"""workflow_manifest.py -- Strict bounded manifest schema for workflow intake.

This module defines the manifest contract for validated workflow submissions.
Public workflow-facing references use stable ids, digests, and repo-relative
locators only; absolute paths are internal-only and are never returned by the
service-facing payload helpers here.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


MANIFEST_VERSION = "1.0"
TASK_CLASS_ALLOWLIST = frozenset(
    {
        "validated-script",
        "validated-notebook",
        "validated-pipeline-step",
        "validated-pytest-suite",
    }
)
_PROTECTED_SEGMENTS = frozenset({".git", ".planning"})
_SECRET_FILE_NAMES = frozenset(
    {".env", ".envrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "known_hosts"}
)
_SECRET_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_EXECUTION_SUBTREE_PREFIXES = (
    (".ollarma",),
    ("artifacts", "ollarma"),
    ("build", "ollarma"),
    ("runs", "ollarma"),
    ("tmp", "ollarma"),
    ("var", "ollarma"),
)
_STABLE_ID_SEARCH_PREFIXES = (
    PurePosixPath(".ollarma/manifests"),
    PurePosixPath("artifacts/ollarma/manifests"),
    PurePosixPath("runs/ollarma/manifests"),
)
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_STABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_DIGEST_RE = re.compile(r"^[a-z0-9]+:[0-9a-f]{8,128}$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)?$")


def _canonical_json(data: Any) -> str:
    """Return a deterministic JSON string for hashing."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _contains_path(path: pathlib.Path, root: pathlib.Path) -> bool:
    """Return whether path resolves within root."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _looks_like_secret(name: str) -> bool:
    """Reject common secret-bearing filenames from manifest read/write refs."""
    lowered = name.lower()
    return (
        lowered in _SECRET_FILE_NAMES
        or lowered.startswith(".env.")
        or lowered.startswith("secret")
        or "credential" in lowered
        or lowered.endswith(_SECRET_FILE_SUFFIXES)
    )


def _validate_repo_relative_locator(value: str) -> str:
    """Normalize and validate a portable repo-relative locator."""
    locator = value.strip()
    if not locator:
        raise ValueError("repo-relative locator must not be empty")
    if "\\" in locator:
        raise ValueError("repo-relative locators must use '/' separators")
    if locator.startswith("~") or locator.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(locator):
        raise ValueError("absolute path locators are forbidden in workflow payloads")

    pure = PurePosixPath(locator)
    normalized = pure.as_posix()
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError("repo-relative locators must not escape the repo root")
    if locator != normalized:
        raise ValueError("repo-relative locators must be normalized")
    return normalized


def _validate_task_class(task_class: str) -> str:
    """Enforce the manifest task-class allowlist."""
    normalized = task_class.strip()
    if normalized not in TASK_CLASS_ALLOWLIST:
        joined = ", ".join(sorted(TASK_CLASS_ALLOWLIST))
        raise ValueError(f"task_class must be one of: {joined}")
    return normalized


class ManifestRef(BaseModel):
    """Public workflow locator using stable ids, digests, and repo-relative refs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stable_id: str | None = None
    repo_relative: str | None = None
    digest: str | None = None

    @field_validator("stable_id")
    @classmethod
    def _validate_stable_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stable_id = value.strip()
        if not stable_id or not _STABLE_ID_RE.fullmatch(stable_id):
            raise ValueError("stable_id must use portable identifier characters only")
        return stable_id

    @field_validator("repo_relative")
    @classmethod
    def _validate_repo_relative(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_repo_relative_locator(value)

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        digest = value.strip().lower()
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError("digest must look like algo:hexdigest")
        return digest

    @model_validator(mode="after")
    def _require_portable_locator(self) -> ManifestRef:
        if self.stable_id is None and self.repo_relative is None and self.digest is None:
            raise ValueError("ManifestRef requires a stable_id, repo_relative, or digest")
        return self

    @classmethod
    def from_repo_path(
        cls,
        *,
        repo_root: pathlib.Path,
        path: pathlib.Path,
        stable_id: str | None = None,
        digest: str | None = None,
    ) -> ManifestRef:
        """Build a portable ref from a resolved repo-local path."""
        repo_relative = path.resolve().relative_to(repo_root.resolve()).as_posix()
        return cls(stable_id=stable_id, repo_relative=repo_relative, digest=digest)

    def to_service_payload(self) -> dict[str, str]:
        """Return a service-safe locator representation without absolute paths."""
        payload: dict[str, str] = {}
        if self.stable_id is not None:
            payload["stable_id"] = self.stable_id
        if self.repo_relative is not None:
            payload["repo_relative"] = self.repo_relative
        if self.digest is not None:
            payload["digest"] = self.digest
        return payload


ManifestRefInput = str | pathlib.Path | ManifestRef | Mapping[str, Any]


class ValidationContract(BaseModel):
    """Bounded validation requirements attached to a workflow manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: str = Field(min_length=1)
    spec_ref: ManifestRef
    success_criteria: tuple[str, ...] = Field(min_length=1)


class SummarySchema(BaseModel):
    """Required summary schema owned by the consumer repo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str = Field(min_length=1)
    format: Literal["json", "markdown", "yaml"]
    required_fields: tuple[str, ...] = Field(min_length=1)


class CheckpointPolicy(BaseModel):
    """Resume-safe checkpoint policy keyed by run_id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_root: ManifestRef
    resume_key: Literal["run_id"]
    max_retries: int = Field(default=1, ge=0, le=1)


class EscalationPolicy(BaseModel):
    """Escalation contract for deterministic rejections and handoff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handoff_target: str = Field(min_length=1)
    on_reason_codes: tuple[str, ...] = Field(min_length=1)


class ArtifactRoot(BaseModel):
    """Writable bounded execution subtree declared by the manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_name: str = Field(min_length=1)
    locator: ManifestRef


class ManifestStep(BaseModel):
    """One workflow step inside the validated manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str = Field(min_length=1)
    stage: Literal[
        "preflight",
        "scaffold/materialize",
        "execute",
        "validate",
        "summarize",
        "interpret/escalate",
    ]
    task_type: str
    inputs: tuple[ManifestRef, ...] = ()
    outputs: tuple[ManifestRef, ...] = Field(min_length=1)
    materialization_root: ManifestRef

    @field_validator("task_type")
    @classmethod
    def _validate_task_type(cls, value: str) -> str:
        return _validate_task_class(value)

    def to_service_payload(self) -> dict[str, Any]:
        """Return a service-safe representation of the step."""
        return {
            "step_id": self.step_id,
            "stage": self.stage,
            "task_type": self.task_type,
            "inputs": [ref.to_service_payload() for ref in self.inputs],
            "outputs": [ref.to_service_payload() for ref in self.outputs],
            "materialization_root": self.materialization_root.to_service_payload(),
        }


class ValidatedWorkflowManifest(BaseModel):
    """Strict manifest admitted onto the workflow execution lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: Literal["1.0"]
    consumer_repo: str = Field(min_length=1)
    adapter_name: str = Field(min_length=1)
    task_class: str
    run_id: str = Field(min_length=1)
    steps: tuple[ManifestStep, ...] = Field(min_length=1)
    validation_contract: ValidationContract
    summary_schema: SummarySchema
    checkpoint_policy: CheckpointPolicy
    escalation_policy: EscalationPolicy
    artifact_roots: tuple[ArtifactRoot, ...] = Field(min_length=1)

    _manifest_path: pathlib.Path | None = PrivateAttr(default=None)
    _repo_root: pathlib.Path | None = PrivateAttr(default=None)
    _manifest_ref: ManifestRef | None = PrivateAttr(default=None)

    @field_validator("consumer_repo", "adapter_name")
    @classmethod
    def _validate_repo_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not _REPO_NAME_RE.fullmatch(cleaned):
            raise ValueError("repo identifiers must be portable names, not filesystem paths")
        return cleaned

    @field_validator("task_class")
    @classmethod
    def _validate_manifest_task_class(cls, value: str) -> str:
        return _validate_task_class(value)

    @model_validator(mode="after")
    def _validate_step_ids(self) -> ValidatedWorkflowManifest:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step_id values must be unique")
        artifact_root_names = [root.root_name for root in self.artifact_roots]
        if len(artifact_root_names) != len(set(artifact_root_names)):
            raise ValueError("artifact root names must be unique")
        return self

    def bind_resolution(
        self,
        *,
        manifest_path: pathlib.Path,
        repo_root: pathlib.Path,
        manifest_ref: ManifestRef,
    ) -> ValidatedWorkflowManifest:
        """Attach internal resolution context after loader validation."""
        object.__setattr__(self, "_manifest_path", manifest_path)
        object.__setattr__(self, "_repo_root", repo_root)
        object.__setattr__(self, "_manifest_ref", manifest_ref)
        return self

    def manifest_digest(self) -> str:
        """Return a stable content digest for the validated manifest."""
        digest = hashlib.sha256(_canonical_json(self.model_dump(mode="json")).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def to_service_payload(self) -> dict[str, Any]:
        """Return a service-safe payload without absolute filesystem paths."""
        payload = {
            "manifest_version": self.manifest_version,
            "consumer_repo": self.consumer_repo,
            "adapter_name": self.adapter_name,
            "task_class": self.task_class,
            "run_id": self.run_id,
            "manifest_digest": self.manifest_digest(),
            "steps": [step.to_service_payload() for step in self.steps],
            "validation_contract": {
                "contract_id": self.validation_contract.contract_id,
                "spec_ref": self.validation_contract.spec_ref.to_service_payload(),
                "success_criteria": list(self.validation_contract.success_criteria),
            },
            "summary_schema": {
                "schema_id": self.summary_schema.schema_id,
                "format": self.summary_schema.format,
                "required_fields": list(self.summary_schema.required_fields),
            },
            "checkpoint_policy": {
                "checkpoint_root": self.checkpoint_policy.checkpoint_root.to_service_payload(),
                "resume_key": self.checkpoint_policy.resume_key,
                "max_retries": self.checkpoint_policy.max_retries,
            },
            "escalation_policy": {
                "handoff_target": self.escalation_policy.handoff_target,
                "on_reason_codes": list(self.escalation_policy.on_reason_codes),
            },
            "artifact_roots": [
                {"root_name": root.root_name, "locator": root.locator.to_service_payload()}
                for root in self.artifact_roots
            ],
        }
        if self._manifest_ref is not None:
            payload["manifest_ref"] = self._manifest_ref.to_service_payload()
        return payload


def _normalize_allowlisted_roots(allowlisted_roots: list[pathlib.Path]) -> list[pathlib.Path]:
    """Resolve and validate the allowlisted repo roots."""
    resolved: list[pathlib.Path] = []
    seen: set[pathlib.Path] = set()
    for root in allowlisted_roots:
        candidate = pathlib.Path(root).resolve()
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(f"allowlisted root does not exist: {candidate}")
        if candidate not in seen:
            resolved.append(candidate)
            seen.add(candidate)
    if not resolved:
        raise ValueError("allowlisted_roots must not be empty")
    return resolved


def _ensure_no_symlink_segments(repo_root: pathlib.Path, repo_relative: str) -> pathlib.Path:
    """Reject path traversal through symlinked path segments."""
    current = repo_root
    for part in PurePosixPath(repo_relative).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError(f"symlink locators are forbidden: {repo_relative}")
    resolved = current.resolve(strict=False)
    if not _contains_path(resolved, repo_root):
        raise ValueError(f"path escapes allowlisted root: {repo_relative}")
    return current


def _protected_path_reason(repo_relative: str) -> str | None:
    """Return the protected-surface reason for a locator, if any."""
    parts = PurePosixPath(repo_relative).parts
    if any(part in _PROTECTED_SEGMENTS for part in parts):
        return "protected repo control path"
    if _looks_like_secret(parts[-1]):
        return "secret-bearing file"
    return None


def _is_execution_subtree(repo_relative: str) -> bool:
    """Return whether a repo-relative path lives in a dedicated execution subtree."""
    parts = PurePosixPath(repo_relative).parts
    return any(parts[: len(prefix)] == prefix for prefix in _EXECUTION_SUBTREE_PREFIXES)


def _validate_manifest_ref_path(
    ref: ManifestRef,
    *,
    repo_root: pathlib.Path,
    label: str,
    writable: bool,
) -> pathlib.Path | None:
    """Validate a ManifestRef against repo boundaries and protected path rules."""
    if ref.repo_relative is None:
        if writable:
            raise ValueError(f"{label} must provide a repo-relative locator")
        return None

    protected_reason = _protected_path_reason(ref.repo_relative)
    if protected_reason is not None:
        raise ValueError(f"{label} targets a {protected_reason}: {ref.repo_relative}")
    if writable and not _is_execution_subtree(ref.repo_relative):
        raise ValueError(f"{label} must stay inside a dedicated execution subtree")

    return _ensure_no_symlink_segments(repo_root, ref.repo_relative)


def _resolve_manifest_from_repo_relative(
    repo_relative: str,
    allowlisted_roots: list[pathlib.Path],
) -> tuple[pathlib.Path, pathlib.Path]:
    """Resolve a repo-relative manifest path against the allowlisted roots."""
    matches: list[tuple[pathlib.Path, pathlib.Path]] = []
    for root in allowlisted_roots:
        candidate = root / repo_relative
        if candidate.exists():
            matches.append((candidate.resolve(), root))
    if not matches:
        raise FileNotFoundError(f"workflow manifest not found: {repo_relative}")
    if len(matches) > 1:
        raise ValueError(f"workflow manifest ref is ambiguous across allowlisted roots: {repo_relative}")
    manifest_path, repo_root = matches[0]
    _validate_manifest_ref_path(
        ManifestRef(repo_relative=repo_relative),
        repo_root=repo_root,
        label="manifest_ref",
        writable=False,
    )
    return manifest_path, repo_root


def _resolve_manifest_from_stable_id(
    stable_id: str,
    allowlisted_roots: list[pathlib.Path],
) -> tuple[pathlib.Path, pathlib.Path]:
    """Resolve a stable manifest id using the bounded manifest search roots."""
    matches: list[tuple[pathlib.Path, pathlib.Path]] = []
    for root in allowlisted_roots:
        for prefix in _STABLE_ID_SEARCH_PREFIXES:
            for candidate in (
                root / prefix / f"{stable_id}.json",
                root / prefix / stable_id / "manifest.json",
            ):
                if candidate.exists():
                    matches.append((candidate.resolve(), root))
    if not matches:
        raise FileNotFoundError(f"workflow manifest stable id is not materialized: {stable_id}")
    if len(matches) > 1:
        raise ValueError(f"workflow manifest stable id is ambiguous: {stable_id}")
    return matches[0]


def _resolve_manifest_input(
    manifest_ref: ManifestRefInput,
    allowlisted_roots: list[pathlib.Path],
) -> tuple[pathlib.Path, pathlib.Path, str | None, str | None]:
    """Resolve a manifest ref into a repo-local manifest path."""
    if isinstance(manifest_ref, pathlib.Path):
        if manifest_ref.is_absolute():
            manifest_path = manifest_ref.resolve()
            for root in allowlisted_roots:
                if _contains_path(manifest_path, root):
                    repo_relative = manifest_path.relative_to(root).as_posix()
                    _validate_manifest_ref_path(
                        ManifestRef(repo_relative=repo_relative),
                        repo_root=root,
                        label="manifest_ref",
                        writable=False,
                    )
                    return manifest_path, root, None, None
            raise ValueError(f"manifest path is outside allowlisted roots: {manifest_path}")
        return (*_resolve_manifest_from_repo_relative(manifest_ref.as_posix(), allowlisted_roots), None, None)

    if isinstance(manifest_ref, ManifestRef) or isinstance(manifest_ref, Mapping):
        ref = manifest_ref if isinstance(manifest_ref, ManifestRef) else ManifestRef.model_validate(dict(manifest_ref))
        if ref.repo_relative is not None:
            return (*_resolve_manifest_from_repo_relative(ref.repo_relative, allowlisted_roots), ref.stable_id, ref.digest)
        if ref.stable_id is not None:
            return (*_resolve_manifest_from_stable_id(ref.stable_id, allowlisted_roots), ref.stable_id, ref.digest)
        raise ValueError("manifest_ref object must provide repo_relative or stable_id")

    raw_ref = manifest_ref.strip()
    if not raw_ref:
        raise ValueError("manifest_ref must not be empty")
    if raw_ref.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(raw_ref):
        raise ValueError("absolute path manifest refs are forbidden; use a repo-relative locator")
    if "/" in raw_ref or raw_ref.endswith(".json") or raw_ref.startswith("."):
        repo_relative = _validate_repo_relative_locator(raw_ref)
        return (*_resolve_manifest_from_repo_relative(repo_relative, allowlisted_roots), None, None)

    stable_id = ManifestRef(stable_id=raw_ref).stable_id
    assert stable_id is not None
    return (*_resolve_manifest_from_stable_id(stable_id, allowlisted_roots), stable_id, None)


def _validate_execution_roots(
    manifest: ValidatedWorkflowManifest,
    repo_root: pathlib.Path,
) -> None:
    """Enforce dedicated execution subtree rules across materialization and outputs."""
    artifact_paths: list[pathlib.Path] = []
    for artifact_root in manifest.artifact_roots:
        path = _validate_manifest_ref_path(
            artifact_root.locator,
            repo_root=repo_root,
            label=f"artifact_roots[{artifact_root.root_name}]",
            writable=True,
        )
        assert path is not None
        artifact_paths.append(path.resolve(strict=False))

    checkpoint_path = _validate_manifest_ref_path(
        manifest.checkpoint_policy.checkpoint_root,
        repo_root=repo_root,
        label="checkpoint_policy.checkpoint_root",
        writable=True,
    )
    assert checkpoint_path is not None
    if not any(_contains_path(checkpoint_path.resolve(strict=False), root) for root in artifact_paths):
        raise ValueError("checkpoint root must stay inside a declared artifact root")

    _validate_manifest_ref_path(
        manifest.validation_contract.spec_ref,
        repo_root=repo_root,
        label="validation_contract.spec_ref",
        writable=False,
    )

    for step in manifest.steps:
        materialization_path = _validate_manifest_ref_path(
            step.materialization_root,
            repo_root=repo_root,
            label=f"steps[{step.step_id}].materialization_root",
            writable=True,
        )
        assert materialization_path is not None
        materialization_resolved = materialization_path.resolve(strict=False)
        if not any(_contains_path(materialization_resolved, root) for root in artifact_paths):
            raise ValueError(f"step {step.step_id} materialization_root must stay inside a declared artifact root")

        for ref in step.inputs:
            _validate_manifest_ref_path(
                ref,
                repo_root=repo_root,
                label=f"steps[{step.step_id}].inputs",
                writable=False,
            )
        for ref in step.outputs:
            output_path = _validate_manifest_ref_path(
                ref,
                repo_root=repo_root,
                label=f"steps[{step.step_id}].outputs",
                writable=True,
            )
            assert output_path is not None
            output_resolved = output_path.resolve(strict=False)
            if not (
                _contains_path(output_resolved, materialization_resolved)
                or any(_contains_path(output_resolved, root) for root in artifact_paths)
            ):
                raise ValueError(f"step {step.step_id} outputs must stay inside materialization_root or artifact_roots")


def load_workflow_manifest(
    manifest_ref: ManifestRefInput,
    *,
    allowlisted_roots: list[pathlib.Path],
) -> ValidatedWorkflowManifest:
    """Load and validate a workflow manifest from a portable manifest_ref."""
    resolved_roots = _normalize_allowlisted_roots(allowlisted_roots)
    manifest_path, repo_root, stable_id, expected_digest = _resolve_manifest_input(manifest_ref, resolved_roots)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"workflow manifest is not a file: {manifest_path}")

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = ValidatedWorkflowManifest.model_validate(payload)
    _validate_execution_roots(manifest, repo_root)
    actual_digest = manifest.manifest_digest()
    if expected_digest is not None and expected_digest != actual_digest:
        raise ValueError(f"workflow manifest digest mismatch: expected {expected_digest}, got {actual_digest}")

    bound_ref = ManifestRef.from_repo_path(repo_root=repo_root, path=manifest_path, stable_id=stable_id)
    bound_ref = bound_ref.model_copy(update={"digest": actual_digest})
    return manifest.bind_resolution(
        manifest_path=manifest_path,
        repo_root=repo_root,
        manifest_ref=bound_ref,
    )
