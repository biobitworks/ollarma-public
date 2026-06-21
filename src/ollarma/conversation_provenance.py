"""Local conversation provenance capture for Ollarma interactions.

Bridge events are operational metadata. This module owns raw local transcript
records, redacted review records, and the small index Antigence can use to
decide what may become a labeled training candidate.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ConversationSurface = Literal[
    "http_chat",
    "http_route",
    "mcp_chat",
    "mcp_route",
    "cli_chat",
    "agent",
    "fleet_agent",
    "swarm",
]
ConversationRole = Literal["user", "model", "tool", "system", "event"]
RedactionStatus = Literal["raw_local", "redacted", "not_applicable", "failed"]
ReviewStatus = Literal["not_reviewed", "review_candidate", "in_review", "reviewed"]
TrainingEligibility = Literal[
    "do_not_train",
    "review_only",
    "training_candidate",
    "holdout_candidate",
]
CaptureStatus = Literal["captured", "not_implemented"]


_SECRETISH_RE = re.compile(
    r"(?i)\b(?:"
    r"sk|ghp|gho|ghu|github_pat|pat|api[_-]?key|token|secret|password"
    r")[_=:.-]?[A-Za-z0-9_./+=-]{8,}\b"
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:(?:/Users|/home)/[^\s\"']+|/[A-Za-z0-9_.-]+(?:/[^\s\"']+){1,})"
)


class ConversationProvenanceError(ValueError):
    """Raised when a record cannot be exported under provenance policy."""


class ToolCallArtifact(BaseModel):
    """Redacted, hash-linked summary of one proposed tool call."""

    tool_name: str
    arguments_hash: str
    result_hash: str | None = None
    guardrail_status: str = "not_applicable"
    mutation_flag: bool = False

    model_config = ConfigDict(frozen=True)


class ConversationTurn(BaseModel):
    """Durable per-turn provenance index record."""

    schema_version: Literal[1] = 1
    conversation_id: str
    turn_id: str
    parent_turn_id: str | None = None
    created_at: str
    surface: ConversationSurface
    role: ConversationRole
    project: str | None = None
    model: str | None = None
    lane: str | None = None
    reason_code: str | None = None
    prompt_hash: str | None = None
    response_hash: str | None = None
    raw_ref: str | None = None
    redacted_ref: str | None = None
    artifact_refs: tuple[str, ...] = ()
    bridge_event_refs: tuple[str, ...] = ()
    route_receipt_ref: str | None = None
    gateway_receipt_ref: str | None = None
    agent_receipt_ref: str | None = None
    kb_evidence_refs: tuple[str, ...] = ()
    git_commit: str | None = None
    git_dirty: bool | None = None
    model_residency_snapshot: dict[str, Any] = Field(default_factory=dict)
    redaction_status: RedactionStatus = "redacted"
    review_status: ReviewStatus = "not_reviewed"
    training_eligibility: TrainingEligibility = "review_only"
    claim_ceiling: str = "no_claim_promotion"
    contains_secret: bool = False
    contains_private_project_context: bool = True
    public_safe: bool = False
    capture_status: CaptureStatus = "captured"
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    """Return a stable sha256 hash for text or structured JSON-compatible data."""

    if isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = _canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def contains_secret(text: str | None) -> bool:
    return bool(text and _SECRETISH_RE.search(text))


def redact_text(text: str | None) -> str:
    """Redact obvious secrets and local absolute paths from review copies."""

    if not text:
        return ""
    redacted = _SECRETISH_RE.sub("[secret]", text)
    return _ABSOLUTE_PATH_RE.sub("[path]", redacted)


def redact_value(value: Any) -> Any:
    """Recursively redact strings in JSON-compatible metadata."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_value(item) for key, item in value.items()}
    return value


def _project_root(root: str | os.PathLike[str] | None = None) -> pathlib.Path:
    return pathlib.Path(root or os.getcwd()).expanduser().resolve()


def _rel(path: pathlib.Path, root: pathlib.Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def git_snapshot(root: str | os.PathLike[str] | None = None) -> tuple[str | None, bool | None]:
    repo_root = _project_root(root)
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--short"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        return commit, dirty
    except Exception:  # noqa: BLE001 -- provenance must not break callers
        return None, None


def model_residency_snapshot() -> dict[str, Any]:
    """Return a placeholder snapshot without shelling out to Ollama on request path."""

    return {"status": "not_collected"}


class ConversationStore:
    """Append-only local transcript, redacted record, and index writer."""

    def __init__(self, repo_root: str | os.PathLike[str] | None = None) -> None:
        self.repo_root = _project_root(repo_root)
        self.base = self.repo_root / ".ollarma" / "conversations"
        self.raw_dir = self.base / "raw"
        self.redacted_dir = self.base / "redacted"
        self.artifacts_dir = self.base / "artifacts"

    def append_turn(
        self,
        *,
        turn: ConversationTurn,
        raw_payload: dict[str, Any],
        redacted_payload: dict[str, Any],
    ) -> ConversationTurn:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.redacted_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        raw_path = self.raw_dir / f"{turn.conversation_id}.jsonl"
        redacted_path = self.redacted_dir / f"{turn.conversation_id}.jsonl"
        enriched = turn.model_copy(
            update={
                "raw_ref": _rel(raw_path, self.repo_root),
                "redacted_ref": _rel(redacted_path, self.repo_root),
            }
        )
        raw_record = {"turn": enriched.model_dump(mode="json"), "payload": raw_payload}
        redacted_record = {"turn": enriched.model_dump(mode="json"), "payload": redacted_payload}

        for path, record in ((raw_path, raw_record), (redacted_path, redacted_record)):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_canonical_json(record) + "\n")

        with (self.base / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(_canonical_json(enriched.model_dump(mode="json")) + "\n")

        index_row = {
            "schema_version": enriched.schema_version,
            "conversation_id": enriched.conversation_id,
            "turn_id": enriched.turn_id,
            "created_at": enriched.created_at,
            "surface": enriched.surface,
            "project": enriched.project,
            "model": enriched.model,
            "prompt_hash": enriched.prompt_hash,
            "response_hash": enriched.response_hash,
            "raw_ref": enriched.raw_ref,
            "redacted_ref": enriched.redacted_ref,
            "review_status": enriched.review_status,
            "training_eligibility": enriched.training_eligibility,
            "capture_status": enriched.capture_status,
        }
        with (self.base / "index.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(_canonical_json(index_row) + "\n")
        return enriched


def record_conversation_turn(
    *,
    repo_root: str | os.PathLike[str] | None = None,
    surface: ConversationSurface,
    role: ConversationRole = "model",
    prompt_text: str | None = None,
    response_text: str | None = None,
    conversation_id: str | None = None,
    turn_id: str | None = None,
    parent_turn_id: str | None = None,
    project: str | None = None,
    model: str | None = None,
    lane: str | None = None,
    reason_code: str | None = None,
    bridge_event_refs: tuple[str, ...] = (),
    route_receipt_ref: str | None = None,
    gateway_receipt_ref: str | None = None,
    agent_receipt_ref: str | None = None,
    kb_evidence_refs: tuple[str, ...] = (),
    artifact_refs: tuple[str, ...] = (),
    review_status: ReviewStatus = "not_reviewed",
    training_eligibility: TrainingEligibility = "review_only",
    contains_private_project_context: bool = True,
    public_safe: bool = False,
    metadata: dict[str, Any] | None = None,
) -> ConversationTurn:
    root = _project_root(repo_root)
    commit, dirty = git_snapshot(root)
    prompt = prompt_text or ""
    response = response_text or ""
    raw_metadata = metadata or {}
    redacted_metadata = redact_value(raw_metadata)
    raw_payload = {
        "prompt_text": prompt,
        "response_text": response,
        "metadata": raw_metadata,
    }
    redacted_payload = {
        "prompt_text": redact_text(prompt),
        "response_text": redact_text(response),
        "metadata": redacted_metadata,
    }
    secret = contains_secret(prompt) or contains_secret(response)
    turn = ConversationTurn(
        conversation_id=conversation_id or str(uuid.uuid4()),
        turn_id=turn_id or str(uuid.uuid4()),
        parent_turn_id=parent_turn_id,
        created_at=_utc_now(),
        surface=surface,
        role=role,
        project=project,
        model=model,
        lane=lane,
        reason_code=reason_code,
        prompt_hash=stable_hash(prompt),
        response_hash=stable_hash(response),
        artifact_refs=artifact_refs,
        bridge_event_refs=bridge_event_refs,
        route_receipt_ref=route_receipt_ref,
        gateway_receipt_ref=gateway_receipt_ref,
        agent_receipt_ref=agent_receipt_ref,
        kb_evidence_refs=kb_evidence_refs,
        git_commit=commit,
        git_dirty=dirty,
        model_residency_snapshot=model_residency_snapshot(),
        redaction_status="redacted",
        review_status=review_status,
        training_eligibility=training_eligibility,
        contains_secret=secret,
        contains_private_project_context=contains_private_project_context,
        public_safe=public_safe,
        metadata=metadata or {},
    )
    return ConversationStore(root).append_turn(
        turn=turn,
        raw_payload=raw_payload,
        redacted_payload=redacted_payload,
    )


def not_implemented_turn(
    *,
    surface: ConversationSurface,
    reason: str,
    repo_root: str | os.PathLike[str] | None = None,
) -> ConversationTurn:
    root = _project_root(repo_root)
    commit, dirty = git_snapshot(root)
    turn = ConversationTurn(
        conversation_id=str(uuid.uuid4()),
        turn_id=str(uuid.uuid4()),
        created_at=_utc_now(),
        surface=surface,
        role="event",
        git_commit=commit,
        git_dirty=dirty,
        model_residency_snapshot=model_residency_snapshot(),
        redaction_status="not_applicable",
        training_eligibility="do_not_train",
        capture_status="not_implemented",
        metadata={"reason": reason},
    )
    return ConversationStore(root).append_turn(
        turn=turn,
        raw_payload={"reason": reason},
        redacted_payload={"reason": reason},
    )


def extract_tool_call_artifacts(messages: list[dict]) -> tuple[ToolCallArtifact, ...]:
    """Summarize proposed tool calls and adjacent tool results without raw text."""

    artifacts: list[ToolCallArtifact] = []
    pending: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or ():
                function = call.get("function") or {}
                tool_name = str(function.get("name") or "")
                args = function.get("arguments") or {}
                pending.append({"tool_name": tool_name, "arguments": args})
        elif message.get("role") == "tool" and pending:
            proposal = pending.pop(0)
            result_text = str(message.get("content") or "")
            guardrail_status = "not_applicable"
            if "[BLOCKED by guardrail" in result_text:
                guardrail_status = "blocked"
            elif "[WARNING: flagged by guardrail" in result_text:
                guardrail_status = "flagged"
            artifacts.append(
                ToolCallArtifact(
                    tool_name=proposal["tool_name"],
                    arguments_hash=stable_hash(proposal["arguments"]),
                    result_hash=stable_hash(result_text),
                    guardrail_status=guardrail_status,
                    mutation_flag=_tool_can_mutate(proposal["tool_name"]),
                )
            )
    for proposal in pending:
        artifacts.append(
            ToolCallArtifact(
                tool_name=proposal["tool_name"],
                arguments_hash=stable_hash(proposal["arguments"]),
                result_hash=None,
                mutation_flag=_tool_can_mutate(proposal["tool_name"]),
            )
        )
    return tuple(artifacts)


def _tool_can_mutate(tool_name: str) -> bool:
    return tool_name in {
        "edit_file",
        "run_bash",
        "git_cmd",
        "workflow",
        "submit_workflow",
        "run_project_workflow",
    } or "workflow" in tool_name


def build_antigence_review_candidate(
    *,
    turn: ConversationTurn,
    redacted_prompt: str,
    redacted_response: str,
    labels: tuple[str, ...] = (),
    label_reviewer: str = "operator",
    split: Literal["review_only", "train_candidate", "eval_holdout_candidate"] = "review_only",
) -> dict[str, Any]:
    """Build an Antigence candidate only after redaction and labeling gates."""

    if turn.redaction_status != "redacted":
        raise ConversationProvenanceError("unredacted turns cannot be exported")
    if turn.training_eligibility == "do_not_train":
        raise ConversationProvenanceError("do_not_train turns cannot be exported")
    if not labels:
        raise ConversationProvenanceError("export requires at least one review label")
    if contains_secret(redacted_prompt) or contains_secret(redacted_response):
        raise ConversationProvenanceError("redacted export still appears to contain a secret")
    payload = {
        "source_repo": "ollarma",
        "conversation_id": turn.conversation_id,
        "turn_id": turn.turn_id,
        "surface": turn.surface,
        "project": turn.project,
        "model": turn.model,
        "prompt_hash": turn.prompt_hash,
        "response_hash": turn.response_hash,
        "redacted_prompt": redacted_prompt,
        "redacted_response": redacted_response,
        "artifact_refs": list(turn.artifact_refs),
        "receipt_refs": [
            ref
            for ref in (
                turn.route_receipt_ref,
                turn.gateway_receipt_ref,
                turn.agent_receipt_ref,
            )
            if ref
        ],
        "bridge_event_refs": list(turn.bridge_event_refs),
        "route_evidence_refs": list(turn.kb_evidence_refs),
        "operator_feedback_refs": [],
        "redaction_status": turn.redaction_status,
        "training_eligibility": turn.training_eligibility,
        "claim_ceiling": turn.claim_ceiling,
        "labels": [
            {"label": label, "reviewer": label_reviewer, "review_status": "labeled"}
            for label in labels
        ],
        "split": split,
    }
    payload["source_export_hash"] = stable_hash(payload)
    return payload
