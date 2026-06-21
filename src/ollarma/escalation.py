"""escalation.py -- Structured receipts for deterministic local escalation."""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReasonCode(str, Enum):
    """Machine-readable escalation reasons from the Phase 18 contract."""

    TOOL_INTENT_REJECTED = "TOOL_INTENT_REJECTED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    GUARDRAIL_FLAG_RETRYABLE = "GUARDRAIL_FLAG_RETRYABLE"
    RESOURCE_BUDGET_EXCEEDED = "RESOURCE_BUDGET_EXCEEDED"
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    SELECTION_MISSING = "SELECTION_MISSING"
    SELECTION_STALE = "SELECTION_STALE"
    UNKNOWN_NAMESPACE = "UNKNOWN_NAMESPACE"
    SWAP_DEGRADED = "SWAP_DEGRADED"
    # v5.1 lane-runtime extensions (Phase 66 plan 02). Additive only --
    # do NOT reorder or remove existing values (breaks v5.0 receipt-chain compat).
    LEASE_HELD_BY_OTHER = "LEASE_HELD_BY_OTHER"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    LANE_FAILED = "LANE_FAILED"
    BLOCKED = "BLOCKED"
    # Phase 67 additions (checkpoint/resume + token-loss). Additive only.
    # SWAP_DEGRADED is reused from above (already in enum) -- no duplicate.
    TOKEN_BUDGET_EXCEEDED = "TOKEN_BUDGET_EXCEEDED"
    RESUME_NO_CANDIDATE = "RESUME_NO_CANDIDATE"
    # Phase 68 additions (small-model routing ladder). Additive only.
    ROUTING_DEGRADED = "ROUTING_DEGRADED"
    ROUTING_RESCUE_ONLY = "ROUTING_RESCUE_ONLY"
    ROUTING_BLOCKED_ESCALATE = "ROUTING_BLOCKED_ESCALATE"


class EscalationReceipt(BaseModel):
    """Structured receipt emitted when local execution cannot proceed."""

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    project: str
    lane: str
    task_class: str
    run_id: str | None = None
    stage: str | None = None
    local_model: str | None = None
    reason_code: ReasonCode
    reason_detail: str
    guardrail: dict[str, Any] | None = None
    resource_snapshot: dict[str, Any] = Field(default_factory=dict)
    checkpoint_ref: dict[str, str] | None = None
    next_action: str
    created_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def build_escalation_receipt(
    *,
    project: str,
    lane: str,
    task_class: str,
    run_id: str | None = None,
    stage: str | None = None,
    reason_code: ReasonCode,
    reason_detail: str,
    guardrail: dict[str, Any] | None = None,
    resource_snapshot: dict[str, Any] | None = None,
    checkpoint_ref: dict[str, str] | None = None,
    next_action: str = "frontier_or_human",
    local_model: str | None = None,
) -> EscalationReceipt:
    """Build a normalized escalation receipt."""
    return EscalationReceipt(
        project=project,
        lane=lane,
        task_class=task_class,
        run_id=run_id,
        stage=stage,
        local_model=local_model,
        reason_code=reason_code,
        reason_detail=reason_detail,
        guardrail=guardrail,
        resource_snapshot=resource_snapshot or {},
        checkpoint_ref=checkpoint_ref,
        next_action=next_action,
    )
