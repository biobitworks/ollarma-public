from __future__ import annotations

import pathlib

import pytest

from ollarma.run_ledger import (
    CheckpointState,
    RunReceipt,
    append_run_receipt,
    apply_receipt_to_checkpoint,
    list_autopilot_runs,
    list_workflow_runs,
    load_autopilot_run_detail,
    load_workflow_run_detail,
    resolve_resume_point,
    resume_workflow_run,
    write_checkpoint_state,
)


def _repo_root(tmp_path: pathlib.Path) -> pathlib.Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return repo_root


def _receipt(
    *,
    run_id: str = "run-001",
    stage: str = "preflight",
    step_id: str = "execute-script",
    status: str = "accepted",
    retry_count: int = 0,
    reason_code: str | None = None,
    checkpoint_ref: dict[str, str] | None = None,
    inputs: tuple[dict[str, object], ...] = ({"repo_relative": ".ollarma/manifests/workflow.json"},),
) -> RunReceipt:
    return RunReceipt(
        run_id=run_id,
        stage=stage,
        step_id=step_id,
        task_or_command=f"workflow-step:{step_id}",
        lane="workflow_execution_queue",
        status=status,
        inputs=inputs,
        outputs=(),
        duration_s=0.1,
        retry_count=retry_count,
        reason_code=reason_code,
        checkpoint_ref=checkpoint_ref,
    )


def test_append_run_receipt(tmp_path: pathlib.Path) -> None:
    repo_root = _repo_root(tmp_path)
    receipts_path = repo_root / ".ollarma" / "runs" / "run-001" / "receipts.json"

    first = append_run_receipt(
        repo_root=repo_root,
        receipts_path=receipts_path,
        receipt=_receipt(),
    )
    second = append_run_receipt(
        repo_root=repo_root,
        receipts_path=receipts_path,
        receipt=_receipt(stage="execute", status="completed"),
    )

    assert first.parent_hash == "0" * 64
    assert second.parent_hash == first.receipt_hash
    assert len(receipts_path.read_text(encoding="utf-8").splitlines()) > 2


def test_resolve_resume_point_after_restart(tmp_path: pathlib.Path) -> None:
    repo_root = _repo_root(tmp_path)
    receipts_path = repo_root / ".ollarma" / "runs" / "run-001" / "receipts.json"
    checkpoint_path = repo_root / ".ollarma" / "runs" / "run-001" / "checkpoint.json"
    checkpoint_ref = {"repo_relative": ".ollarma/runs/run-001/checkpoint.json"}

    receipt = append_run_receipt(
        repo_root=repo_root,
        receipts_path=receipts_path,
        receipt=_receipt(checkpoint_ref=checkpoint_ref),
    )
    checkpoint = CheckpointState(
        run_id="run-001",
        current_stage="preflight",
        last_validated_stage="preflight",
        last_receipt_hash=receipt.receipt_hash,
        retry_budget_remaining=1,
        resume_from_step="execute-script",
    )
    write_checkpoint_state(
        repo_root=repo_root,
        checkpoint_path=checkpoint_path,
        state=checkpoint,
    )

    resume = resolve_resume_point(
        repo_root=repo_root,
        receipts_path=receipts_path,
        checkpoint_path=checkpoint_path,
    )

    assert resume.next_stage == "scaffold/materialize"
    assert resume.resume_from_step == "execute-script"
    assert resume.checkpoint_ref == checkpoint_ref
    assert resume_workflow_run(
        "run-001",
        repo_root=repo_root,
        receipts_path=receipts_path,
        checkpoint_path=checkpoint_path,
    ).next_stage == "scaffold/materialize"


def test_retry_budget_updates_for_retryable_failure_only() -> None:
    checkpoint = CheckpointState(
        run_id="run-001",
        current_stage="execute",
        last_validated_stage="preflight",
        last_receipt_hash="abc",
        retry_budget_remaining=1,
        resume_from_step="execute-script",
    )
    retryable = _receipt(stage="execute", status="retryable_failure", retry_count=0).model_copy(
        update={"receipt_hash": "hash-1"}
    )
    deterministic = _receipt(stage="execute", status="deterministic_failure", retry_count=0).model_copy(
        update={"receipt_hash": "hash-2"}
    )

    retry_state = apply_receipt_to_checkpoint(checkpoint, retryable)
    deterministic_state = apply_receipt_to_checkpoint(checkpoint, deterministic)

    assert retry_state.retry_budget_remaining == 0
    assert deterministic_state.retry_budget_remaining == 1


def test_secret_bearing_inputs_are_redacted(tmp_path: pathlib.Path) -> None:
    repo_root = _repo_root(tmp_path)
    receipts_path = repo_root / ".ollarma" / "runs" / "run-001" / "receipts.json"

    receipt = append_run_receipt(
        repo_root=repo_root,
        receipts_path=receipts_path,
        receipt=_receipt(inputs=({"repo_relative": ".ollarma/runs/run-001/.env"},)),
    )

    assert receipt.inputs[0].get("kind") == "redacted-path"
    assert "repo_relative" not in receipt.inputs[0]


def test_checkpoint_mismatch_fails_closed(tmp_path: pathlib.Path) -> None:
    repo_root = _repo_root(tmp_path)
    receipts_path = repo_root / ".ollarma" / "runs" / "run-001" / "receipts.json"
    checkpoint_path = repo_root / ".ollarma" / "runs" / "run-001" / "checkpoint.json"

    append_run_receipt(
        repo_root=repo_root,
        receipts_path=receipts_path,
        receipt=_receipt(),
    )
    write_checkpoint_state(
        repo_root=repo_root,
        checkpoint_path=checkpoint_path,
        state=CheckpointState(
            run_id="run-001",
            current_stage="preflight",
            last_validated_stage="preflight",
            last_receipt_hash="wrong-tail",
            retry_budget_remaining=1,
            resume_from_step="execute-script",
        ),
    )

    with pytest.raises(ValueError, match="last_receipt_hash"):
        resolve_resume_point(
            repo_root=repo_root,
            receipts_path=receipts_path,
            checkpoint_path=checkpoint_path,
        )


def test_execution_subtree_writes_are_enforced(tmp_path: pathlib.Path) -> None:
    repo_root = _repo_root(tmp_path)

    with pytest.raises(ValueError, match="execution subtree"):
        append_run_receipt(
            repo_root=repo_root,
            receipts_path=repo_root / "receipts.json",
            receipt=_receipt(),
        )

    with pytest.raises(ValueError, match="execution subtree"):
        write_checkpoint_state(
            repo_root=repo_root,
            checkpoint_path=repo_root / "checkpoint.json",
            state=CheckpointState(
                run_id="run-001",
                current_stage="preflight",
                last_validated_stage=None,
                last_receipt_hash="tail",
                retry_budget_remaining=1,
                resume_from_step="execute-script",
            ),
        )


def test_list_workflow_runs(tmp_path: pathlib.Path) -> None:
    repo_root = _repo_root(tmp_path)
    receipts_path = repo_root / ".ollarma" / "runs" / "run-001" / "receipts.json"
    checkpoint_path = repo_root / ".ollarma" / "runs" / "run-001" / "checkpoint.json"

    receipt = append_run_receipt(
        repo_root=repo_root,
        receipts_path=receipts_path,
        receipt=_receipt(stage="execute", status="completed"),
    )
    write_checkpoint_state(
        repo_root=repo_root,
        checkpoint_path=checkpoint_path,
        state=CheckpointState(
            run_id="run-001",
            current_stage="execute",
            last_validated_stage="execute",
            last_receipt_hash=receipt.receipt_hash,
            retry_budget_remaining=1,
            resume_from_step="execute-script",
        ),
    )

    runs = list_workflow_runs(repo_root=repo_root)

    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-001"
    assert runs[0]["run_kind"] == "workflow"
    assert runs[0]["status"] == "completed"


def test_load_workflow_run_detail(tmp_path: pathlib.Path) -> None:
    repo_root = _repo_root(tmp_path)
    receipts_path = repo_root / ".ollarma" / "runs" / "run-001" / "receipts.json"
    checkpoint_path = repo_root / ".ollarma" / "runs" / "run-001" / "checkpoint.json"
    receipt = append_run_receipt(
        repo_root=repo_root,
        receipts_path=receipts_path,
        receipt=_receipt(stage="validate", status="passed"),
    )
    write_checkpoint_state(
        repo_root=repo_root,
        checkpoint_path=checkpoint_path,
        state=CheckpointState(
            run_id="run-001",
            current_stage="validate",
            last_validated_stage="validate",
            last_receipt_hash=receipt.receipt_hash,
            retry_budget_remaining=0,
            resume_from_step="execute-script",
        ),
    )

    detail = load_workflow_run_detail(repo_root=repo_root, run_id="run-001")

    assert detail["run_id"] == "run-001"
    assert detail["checkpoint"]["current_stage"] == "validate"
    assert detail["receipt_ref"]["repo_relative"].endswith("receipts.json")


def test_list_autopilot_runs_and_load_detail(tmp_path: pathlib.Path) -> None:
    repo_root = _repo_root(tmp_path)
    receipts_path = repo_root / ".ollarma" / "autopilot" / "auto-001" / "receipts.json"
    checkpoint_path = repo_root / ".ollarma" / "autopilot" / "auto-001" / "checkpoint.json"
    receipt = append_run_receipt(
        repo_root=repo_root,
        receipts_path=receipts_path,
        receipt=_receipt(
            run_id="auto-001",
            stage="summarize",
            status="completed",
            step_id="execute-script",
            inputs=({"repo_relative": ".ollarma/autopilot/auto-001/input.json"},),
        ),
    )
    write_checkpoint_state(
        repo_root=repo_root,
        checkpoint_path=checkpoint_path,
        state=CheckpointState(
            run_id="auto-001",
            current_stage="summarize",
            last_validated_stage="summarize",
            last_receipt_hash=receipt.receipt_hash,
            retry_budget_remaining=0,
            resume_from_step="execute-script",
        ),
    )

    summaries = list_autopilot_runs(repo_root=repo_root)
    detail = load_autopilot_run_detail(repo_root=repo_root, run_id="auto-001")

    assert summaries[0]["run_kind"] == "autopilot"
    assert detail["run_kind"] == "autopilot"
    assert detail["checkpoint_ref"]["repo_relative"].endswith("checkpoint.json")
