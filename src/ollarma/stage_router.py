"""stage_router.py -- Deterministic workflow stage and ownership routing."""
from __future__ import annotations

from enum import Enum
from typing import Iterable

from pydantic import BaseModel, ConfigDict

from ollarma.execution_policy import is_retryable_workflow_reason, requires_human_review
from ollarma.run_ledger import CheckpointState, ResumePoint, RunReceipt


class WorkflowStage(str, Enum):
    """Canonical Phase 19 workflow stage machine."""

    PREFLIGHT = "preflight"
    SCAFFOLD_MATERIALIZE = "scaffold_materialize"
    EXECUTE = "execute"
    VALIDATE = "validate"
    SUMMARIZE = "summarize"
    INTERPRET_ESCALATE = "interpret_escalate"


class RouteDecision(BaseModel):
    """Deterministic next-stage and ownership decision."""

    model_config = ConfigDict(frozen=True)

    next_stage: WorkflowStage
    owning_lane: str
    resume_from: str | None = None
    retry_allowed: bool = False
    reason_code: str | None = None
    handoff_required: bool = False


_NEXT_STAGE = {
    WorkflowStage.PREFLIGHT: WorkflowStage.SCAFFOLD_MATERIALIZE,
    WorkflowStage.SCAFFOLD_MATERIALIZE: WorkflowStage.EXECUTE,
    WorkflowStage.EXECUTE: WorkflowStage.VALIDATE,
    WorkflowStage.VALIDATE: WorkflowStage.SUMMARIZE,
    WorkflowStage.SUMMARIZE: WorkflowStage.INTERPRET_ESCALATE,
    WorkflowStage.INTERPRET_ESCALATE: WorkflowStage.INTERPRET_ESCALATE,
}

_LEDGER_TO_STAGE = {
    "preflight": WorkflowStage.PREFLIGHT,
    "scaffold/materialize": WorkflowStage.SCAFFOLD_MATERIALIZE,
    "execute": WorkflowStage.EXECUTE,
    "validate": WorkflowStage.VALIDATE,
    "summarize": WorkflowStage.SUMMARIZE,
    "interpret/escalate": WorkflowStage.INTERPRET_ESCALATE,
}


def _normalize_requested_stage(
    requested_stage: WorkflowStage | str | None,
    checkpoint_state: CheckpointState | None,
    receipt_history: Iterable[RunReceipt],
) -> WorkflowStage:
    if isinstance(requested_stage, WorkflowStage):
        return requested_stage
    if isinstance(requested_stage, str):
        normalized = requested_stage.replace("/", "_")
        return WorkflowStage(normalized)
    if checkpoint_state and checkpoint_state.last_validated_stage:
        prior = _LEDGER_TO_STAGE.get(checkpoint_state.last_validated_stage)
        if prior is not None:
            return _NEXT_STAGE[prior]
    if checkpoint_state and checkpoint_state.current_stage:
        return _LEDGER_TO_STAGE.get(checkpoint_state.current_stage, WorkflowStage.PREFLIGHT)
    for receipt in reversed(tuple(receipt_history)):
        stage = _LEDGER_TO_STAGE.get(receipt.stage)
        if stage is not None:
            return _NEXT_STAGE[stage]
    return WorkflowStage.PREFLIGHT


def determine_next_stage(
    *,
    requested_stage: WorkflowStage | str | None = None,
    checkpoint_state: CheckpointState | None = None,
    receipt_history: Iterable[RunReceipt] = (),
    resume_point: ResumePoint | None = None,
    reason_code: str | None = None,
    contradiction_detected: bool = False,
    anomaly_detected: bool = False,
    sidecar_candidate: bool = False,
    mutates_canonical_output: bool = False,
    governance_significant: bool = False,
) -> RouteDecision:
    """Decide the next stage and owning lane without hidden control flow."""
    target_stage = _normalize_requested_stage(requested_stage, checkpoint_state, receipt_history)
    resume_from = resume_point.resume_from_step if resume_point is not None else checkpoint_state.resume_from_step if checkpoint_state else None
    retry_allowed = bool(
        checkpoint_state is not None
        and checkpoint_state.retry_budget_remaining > 0
        and is_retryable_workflow_reason(reason_code)
    )

    if contradiction_detected or anomaly_detected:
        return RouteDecision(
            next_stage=WorkflowStage.INTERPRET_ESCALATE,
            owning_lane="frontier-only",
            resume_from=resume_from,
            retry_allowed=False,
            reason_code=reason_code or "CONTRADICTION_OR_ANOMALY",
            handoff_required=True,
        )

    if requires_human_review(
        governance_significant=governance_significant,
        mutates_canonical_output=mutates_canonical_output,
    ):
        return RouteDecision(
            next_stage=WorkflowStage.INTERPRET_ESCALATE,
            owning_lane="human-review-required",
            resume_from=resume_from,
            retry_allowed=False,
            reason_code=reason_code,
            handoff_required=True,
        )

    if target_stage in {WorkflowStage.VALIDATE, WorkflowStage.SUMMARIZE} and sidecar_candidate:
        owning_lane = "sidecar-parallel"
    else:
        owning_lane = "ollarma-default"

    if reason_code and not retry_allowed:
        return RouteDecision(
            next_stage=WorkflowStage.INTERPRET_ESCALATE,
            owning_lane=owning_lane,
            resume_from=resume_from,
            retry_allowed=False,
            reason_code=reason_code,
            handoff_required=True,
        )

    return RouteDecision(
        next_stage=target_stage,
        owning_lane=owning_lane,
        resume_from=resume_from,
        retry_allowed=retry_allowed,
        reason_code=reason_code,
        handoff_required=False,
    )
