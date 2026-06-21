from __future__ import annotations

from ollarma.run_ledger import CheckpointState
from ollarma.stage_router import RouteDecision, WorkflowStage, determine_next_stage


def _checkpoint(
    *,
    current_stage: str = "execute",
    last_validated_stage: str | None = "preflight",
    retry_budget_remaining: int = 1,
) -> CheckpointState:
    return CheckpointState(
        run_id="run-001",
        current_stage=current_stage,
        last_validated_stage=last_validated_stage,
        last_receipt_hash="tail",
        retry_budget_remaining=retry_budget_remaining,
        resume_from_step="execute-script",
    )


def test_determine_next_stage_pass_path() -> None:
    decision = determine_next_stage(
        requested_stage=WorkflowStage.EXECUTE,
        checkpoint_state=_checkpoint(),
    )

    assert isinstance(decision, RouteDecision)
    assert decision.next_stage == WorkflowStage.EXECUTE
    assert decision.owning_lane == "ollarma-default"
    assert decision.handoff_required is False


def test_determine_next_stage_retryable_fail() -> None:
    decision = determine_next_stage(
        requested_stage=WorkflowStage.EXECUTE,
        checkpoint_state=_checkpoint(retry_budget_remaining=1),
        reason_code="EXIT_1",
    )

    assert decision.retry_allowed is True
    assert decision.handoff_required is False


def test_determine_next_stage_deterministic_fail() -> None:
    decision = determine_next_stage(
        requested_stage=WorkflowStage.EXECUTE,
        checkpoint_state=_checkpoint(retry_budget_remaining=0),
        reason_code="DEPENDENCY_MISSING",
    )

    assert decision.next_stage == WorkflowStage.INTERPRET_ESCALATE
    assert decision.handoff_required is True


def test_determine_next_stage_contradiction_or_anomaly_routes_frontier() -> None:
    contradiction = determine_next_stage(
        requested_stage=WorkflowStage.VALIDATE,
        checkpoint_state=_checkpoint(current_stage="validate", last_validated_stage="execute"),
        contradiction_detected=True,
    )
    anomaly = determine_next_stage(
        requested_stage=WorkflowStage.VALIDATE,
        checkpoint_state=_checkpoint(current_stage="validate", last_validated_stage="execute"),
        anomaly_detected=True,
    )

    assert contradiction.owning_lane == "frontier-only"
    assert anomaly.owning_lane == "frontier-only"
    assert contradiction.handoff_required is True
    assert anomaly.handoff_required is True


def test_determine_next_stage_sidecar_parallel_validate_and_summarize() -> None:
    validate = determine_next_stage(
        requested_stage=WorkflowStage.VALIDATE,
        checkpoint_state=_checkpoint(current_stage="validate", last_validated_stage="execute"),
        sidecar_candidate=True,
    )
    summarize = determine_next_stage(
        requested_stage=WorkflowStage.SUMMARIZE,
        checkpoint_state=_checkpoint(current_stage="summarize", last_validated_stage="validate"),
        sidecar_candidate=True,
    )

    assert validate.owning_lane == "sidecar-parallel"
    assert summarize.owning_lane == "sidecar-parallel"


def test_determine_next_stage_human_review_required_for_canonical_mutation() -> None:
    mutation = determine_next_stage(
        requested_stage=WorkflowStage.SUMMARIZE,
        checkpoint_state=_checkpoint(current_stage="summarize", last_validated_stage="validate"),
        sidecar_candidate=True,
        mutates_canonical_output=True,
    )
    governance = determine_next_stage(
        requested_stage=WorkflowStage.INTERPRET_ESCALATE,
        checkpoint_state=_checkpoint(current_stage="interpret/escalate", last_validated_stage="summarize"),
        sidecar_candidate=True,
        governance_significant=True,
    )

    assert mutation.owning_lane == "human-review-required"
    assert governance.owning_lane == "human-review-required"
    assert mutation.handoff_required is True
    assert governance.handoff_required is True
