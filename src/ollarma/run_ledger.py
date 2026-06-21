"""run_ledger.py -- Append-only workflow receipts and restart-safe checkpoints."""
from __future__ import annotations

import datetime as dt
import pathlib
import re
from pathlib import PurePosixPath
from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict, Field

from ollarma.evidence import canonical_hash


_GENESIS_RECEIPT_HASH = "0" * 64
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
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_STAGE_ORDER = (
    "preflight",
    "scaffold/materialize",
    "execute",
    "validate",
    "summarize",
    "interpret/escalate",
)


class RunReceipt(BaseModel):
    """Append-only receipt for one workflow stage transition."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    stage: str
    step_id: str
    task_or_command: str
    lane: str
    status: str
    inputs: tuple[dict[str, Any], ...] = ()
    outputs: tuple[dict[str, Any], ...] = ()
    duration_s: float = 0.0
    retry_count: int = 0
    reason_code: str | None = None
    namespace: str = ""
    checkpoint_ref: dict[str, str] | None = None
    governance_refs: tuple[str, ...] = ()
    """Content-addressed governance payload digests for this receipt (SAFE-04)."""
    created_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    parent_hash: str = _GENESIS_RECEIPT_HASH
    receipt_hash: str = ""


class CheckpointState(BaseModel):
    """Mutable restart-safe checkpoint state for one run_id."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    current_stage: str
    last_validated_stage: str | None = None
    last_receipt_hash: str
    retry_budget_remaining: int = Field(ge=0, default=0)
    resume_from_step: str | None = None


class ResumePoint(BaseModel):
    """Resolved restart entry for an existing workflow run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    next_stage: str | None = None
    resume_from_step: str | None = None
    checkpoint_ref: dict[str, str] | None = None
    last_receipt_hash: str = _GENESIS_RECEIPT_HASH


def _looks_like_secret(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in _SECRET_FILE_NAMES
        or lowered.startswith(".env.")
        or lowered.startswith("secret")
        or "credential" in lowered
        or lowered.endswith(_SECRET_FILE_SUFFIXES)
    )


def _contains_path(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_execution_subtree(repo_relative: str) -> bool:
    parts = PurePosixPath(repo_relative).parts
    return any(parts[: len(prefix)] == prefix for prefix in _EXECUTION_SUBTREE_PREFIXES)


def _validate_repo_relative(repo_relative: str) -> str:
    locator = repo_relative.strip()
    if not locator:
        raise ValueError("repo-relative path must not be empty")
    if "\\" in locator:
        raise ValueError("repo-relative path must use '/' separators")
    if locator.startswith("/") or locator.startswith("~") or _WINDOWS_ABSOLUTE_RE.match(locator):
        raise ValueError("absolute path writes are forbidden in the run ledger")
    pure = PurePosixPath(locator)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError("repo-relative path must not escape the repo root")
    normalized = pure.as_posix()
    if locator != normalized:
        raise ValueError("repo-relative path must be normalized")
    if any(part in _PROTECTED_SEGMENTS for part in pure.parts):
        raise ValueError(f"protected path forbidden in run ledger: {repo_relative}")
    if _looks_like_secret(pure.name):
        raise ValueError(f"secret-bearing path forbidden in run ledger: {repo_relative}")
    if not _is_execution_subtree(normalized):
        raise ValueError(f"path must stay inside a dedicated execution subtree: {repo_relative}")
    return normalized


def _validate_execution_path(
    *,
    repo_root: pathlib.Path,
    path: pathlib.Path,
    label: str,
) -> tuple[pathlib.Path, str]:
    repo_root = repo_root.resolve()
    candidate = path if path.is_absolute() else repo_root / path
    repo_relative = _validate_repo_relative(candidate.relative_to(repo_root).as_posix())

    current = repo_root
    for part in PurePosixPath(repo_relative).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError(f"{label} contains a symlink escape: {repo_relative}")
    resolved = candidate.resolve(strict=False)
    if not _contains_path(resolved, repo_root):
        raise ValueError(f"{label} escapes the repo root: {repo_relative}")
    return candidate, repo_relative


def _digest_text(value: str) -> str:
    return f"sha256:{canonical_hash([value])}"


def _sanitize_locator(item: dict[str, Any]) -> dict[str, str]:
    if (
        set(item).issubset({"stable_id", "repo_relative", "digest", "kind"})
        and isinstance(item.get("digest"), str)
    ):
        preserved = {
            key: value
            for key, value in item.items()
            if key in {"stable_id", "repo_relative", "digest", "kind"} and isinstance(value, str)
        }
        if preserved:
            return preserved

    safe: dict[str, str] = {}
    stable_id = item.get("stable_id")
    digest = item.get("digest")
    repo_relative = item.get("repo_relative")
    kind = item.get("kind")

    if isinstance(stable_id, str) and stable_id.strip():
        safe["stable_id"] = stable_id.strip()
    if isinstance(digest, str) and digest.strip():
        safe["digest"] = digest.strip().lower()
    if isinstance(repo_relative, str):
        try:
            safe["repo_relative"] = _validate_repo_relative(repo_relative)
        except ValueError:
            safe["digest"] = safe.get("digest", _digest_text(repo_relative))
            safe["kind"] = "redacted-path"
    if safe:
        if isinstance(kind, str) and kind.strip():
            safe["kind"] = kind.strip()
        return safe
    return {"digest": _digest_text(orjson.dumps(item).decode("utf-8")), "kind": "typed-summary"}


def sanitize_receipt_payload(items: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None) -> tuple[dict[str, Any], ...]:
    """Sanitize receipt I/O payloads into durable, portable summaries only."""
    sanitized: list[dict[str, Any]] = []
    for item in items or ():
        if isinstance(item, dict):
            sanitized.append(_sanitize_locator(item))
            continue
        if isinstance(item, str):
            sanitized.append({"digest": _digest_text(item), "kind": "raw-text"})
            continue
        sanitized.append({"digest": _digest_text(repr(item)), "kind": "typed-summary"})
    return tuple(sanitized)


def _receipt_payload(receipt: RunReceipt) -> dict[str, Any]:
    payload = receipt.model_dump(mode="json")
    payload.pop("receipt_hash", None)
    return payload


def _materialize_receipt(receipt: RunReceipt, *, parent_hash: str) -> RunReceipt:
    sanitized = receipt.model_copy(
        update={
            "inputs": sanitize_receipt_payload(receipt.inputs),
            "outputs": sanitize_receipt_payload(receipt.outputs),
            "parent_hash": parent_hash,
        }
    )
    receipt_hash = canonical_hash(_receipt_payload(sanitized))
    return sanitized.model_copy(update={"receipt_hash": receipt_hash})


def _load_receipts(receipts_path: pathlib.Path) -> list[RunReceipt]:
    if not receipts_path.exists():
        return []
    raw = orjson.loads(receipts_path.read_bytes())
    return [RunReceipt.model_validate(item) for item in raw]


def _verify_receipt_chain(receipts: list[RunReceipt]) -> str:
    parent_hash = _GENESIS_RECEIPT_HASH
    for receipt in receipts:
        expected = _materialize_receipt(receipt.model_copy(update={"receipt_hash": ""}), parent_hash=parent_hash)
        if receipt.parent_hash != expected.parent_hash or receipt.receipt_hash != expected.receipt_hash:
            raise ValueError("existing receipt chain is not append-only")
        parent_hash = receipt.receipt_hash
    return parent_hash


def append_run_receipt(
    *,
    repo_root: pathlib.Path,
    receipts_path: pathlib.Path,
    receipt: RunReceipt,
) -> RunReceipt:
    """Append one verified receipt without mutating existing history."""
    target_path, _repo_relative = _validate_execution_path(
        repo_root=repo_root,
        path=receipts_path,
        label="receipts_path",
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_receipts(target_path)
    parent_hash = _verify_receipt_chain(existing)
    appended = _materialize_receipt(receipt, parent_hash=parent_hash)
    existing.append(appended)
    target_path.write_bytes(
        orjson.dumps([item.model_dump(mode="json") for item in existing], option=orjson.OPT_INDENT_2)
    )
    return appended


def write_checkpoint_state(
    *,
    repo_root: pathlib.Path,
    checkpoint_path: pathlib.Path,
    state: CheckpointState,
) -> pathlib.Path:
    """Write mutable checkpoint state without editing historical receipts."""
    target_path, _repo_relative = _validate_execution_path(
        repo_root=repo_root,
        path=checkpoint_path,
        label="checkpoint_path",
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(orjson.dumps(state.model_dump(mode="json"), option=orjson.OPT_INDENT_2))
    return target_path


def apply_receipt_to_checkpoint(
    checkpoint: CheckpointState,
    receipt: RunReceipt,
) -> CheckpointState:
    """Advance mutable checkpoint state from an append-only receipt."""
    last_validated_stage = checkpoint.last_validated_stage
    if receipt.status in {"accepted", "queued", "completed", "passed"}:
        last_validated_stage = receipt.stage

    retry_budget = checkpoint.retry_budget_remaining
    if receipt.status == "retryable_failure":
        retry_budget = max(0, retry_budget - 1)

    return checkpoint.model_copy(
        update={
            "current_stage": receipt.stage,
            "last_validated_stage": last_validated_stage,
            "last_receipt_hash": receipt.receipt_hash,
            "retry_budget_remaining": retry_budget,
            "resume_from_step": receipt.step_id,
        }
    )


def _next_stage_after(stage: str | None) -> str | None:
    if stage is None:
        return None
    try:
        idx = _STAGE_ORDER.index(stage)
    except ValueError:
        return stage
    if idx + 1 >= len(_STAGE_ORDER):
        return stage
    return _STAGE_ORDER[idx + 1]


def resolve_resume_point(
    *,
    repo_root: pathlib.Path,
    receipts_path: pathlib.Path,
    checkpoint_path: pathlib.Path,
) -> ResumePoint:
    """Resolve the next resumable workflow stage from receipts plus checkpoint state."""
    receipts_target, _ = _validate_execution_path(repo_root=repo_root, path=receipts_path, label="receipts_path")
    checkpoint_target, checkpoint_relative = _validate_execution_path(
        repo_root=repo_root,
        path=checkpoint_path,
        label="checkpoint_path",
    )
    receipts = _load_receipts(receipts_target)
    tail_hash = _verify_receipt_chain(receipts)
    if not checkpoint_target.exists():
        raise FileNotFoundError(f"checkpoint not found: {checkpoint_target}")
    checkpoint = CheckpointState.model_validate(orjson.loads(checkpoint_target.read_bytes()))
    if checkpoint.last_receipt_hash != tail_hash:
        raise ValueError("checkpoint mismatch with last_receipt_hash")

    next_stage = _next_stage_after(checkpoint.last_validated_stage) or checkpoint.current_stage
    return ResumePoint(
        run_id=checkpoint.run_id,
        next_stage=next_stage,
        resume_from_step=checkpoint.resume_from_step,
        checkpoint_ref={"repo_relative": checkpoint_relative},
        last_receipt_hash=checkpoint.last_receipt_hash,
    )


def resume_workflow_run(
    run_id: str,
    *,
    repo_root: pathlib.Path,
    receipts_path: pathlib.Path,
    checkpoint_path: pathlib.Path,
) -> ResumePoint:
    """Resume the same run_id only if checkpoint state matches the receipt tail."""
    resume_point = resolve_resume_point(
        repo_root=repo_root,
        receipts_path=receipts_path,
        checkpoint_path=checkpoint_path,
    )
    if resume_point.run_id != run_id:
        raise ValueError(f"resume run_id mismatch: expected {run_id}, got {resume_point.run_id}")
    return resume_point


def _run_root(repo_root: pathlib.Path, run_kind: str) -> pathlib.Path:
    if run_kind == "workflow":
        return repo_root / ".ollarma" / "runs"
    if run_kind == "autopilot":
        return repo_root / ".ollarma" / "autopilot"
    raise ValueError(f"unknown run kind: {run_kind}")


def _load_checkpoint_summary(
    *,
    repo_root: pathlib.Path,
    checkpoint_path: pathlib.Path,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    if not checkpoint_path.exists():
        return None, None
    checkpoint_target, checkpoint_relative = _validate_execution_path(
        repo_root=repo_root,
        path=checkpoint_path,
        label="checkpoint_path",
    )
    checkpoint = CheckpointState.model_validate(orjson.loads(checkpoint_target.read_bytes()))
    return checkpoint.model_dump(mode="json"), {"repo_relative": checkpoint_relative}


def _load_run_detail(
    *,
    repo_root: pathlib.Path,
    run_dir: pathlib.Path,
    run_kind: str,
) -> dict[str, Any]:
    receipts_path = run_dir / "receipts.json"
    receipts_target, receipts_relative = _validate_execution_path(
        repo_root=repo_root,
        path=receipts_path,
        label="receipts_path",
    )
    receipts = _load_receipts(receipts_target)
    tail_hash = _verify_receipt_chain(receipts)
    latest = receipts[-1] if receipts else None
    checkpoint, checkpoint_ref = _load_checkpoint_summary(
        repo_root=repo_root,
        checkpoint_path=run_dir / "checkpoint.json",
    )
    return {
        "run_id": run_dir.name,
        "run_kind": run_kind,
        "current_stage": checkpoint["current_stage"] if checkpoint else (latest.stage if latest else None),
        "last_validated_stage": checkpoint["last_validated_stage"] if checkpoint else None,
        "status": latest.status if latest else None,
        "reason_code": latest.reason_code if latest else None,
        "step_id": latest.step_id if latest else None,
        "updated_at": latest.created_at if latest else None,
        "receipt_count": len(receipts),
        "last_receipt_hash": tail_hash,
        "receipt_ref": {"repo_relative": receipts_relative},
        "checkpoint_ref": checkpoint_ref,
        "checkpoint": checkpoint,
        "receipts": [receipt.model_dump(mode="json") for receipt in receipts[-10:]],
    }


def _list_runs(
    *,
    repo_root: pathlib.Path,
    run_kind: str,
    limit: int,
) -> list[dict[str, Any]]:
    root = _run_root(repo_root.resolve(), run_kind)
    if not root.exists():
        return []

    details: list[dict[str, Any]] = []
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        try:
            details.append(_load_run_detail(repo_root=repo_root, run_dir=run_dir, run_kind=run_kind))
        except (FileNotFoundError, ValueError, OSError):
            continue
    details.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return details[:limit]


def list_workflow_runs(
    *,
    repo_root: pathlib.Path,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return recent workflow run summaries from the bounded run ledger."""
    return _list_runs(repo_root=repo_root, run_kind="workflow", limit=limit)


def load_workflow_run_detail(
    *,
    repo_root: pathlib.Path,
    run_id: str,
) -> dict[str, Any]:
    """Return one workflow run detail from the bounded run ledger."""
    run_dir = _run_root(repo_root.resolve(), "workflow") / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"workflow run not found: {run_id}")
    return _load_run_detail(repo_root=repo_root, run_dir=run_dir, run_kind="workflow")


def list_autopilot_runs(
    *,
    repo_root: pathlib.Path,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return recent autopilot run summaries from the bounded run ledger."""
    return _list_runs(repo_root=repo_root, run_kind="autopilot", limit=limit)


def load_autopilot_run_detail(
    *,
    repo_root: pathlib.Path,
    run_id: str,
) -> dict[str, Any]:
    """Return one autopilot run detail from the bounded run ledger."""
    run_dir = _run_root(repo_root.resolve(), "autopilot") / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"autopilot run not found: {run_id}")
    return _load_run_detail(repo_root=repo_root, run_dir=run_dir, run_kind="autopilot")
