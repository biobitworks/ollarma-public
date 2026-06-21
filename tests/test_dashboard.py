from __future__ import annotations

import pathlib

import orjson

from ollarma.dashboard import DashboardOverview
from ollarma.dashboard_html import render_dashboard
from ollarma.fleet import AdapterConfig
from ollarma.run_ledger import (
    CheckpointState,
    RunReceipt,
    append_run_receipt,
    write_checkpoint_state,
)
from ollarma.scheduler import RuntimeSnapshot


def _mock_runtime_health(*args, **kwargs):
    from ollarma.service import HelperModelResolution, RuntimeHealthResult, SelectionHealth

    return RuntimeHealthResult(
        status="ok",
        helper_chat=HelperModelResolution(
            effective_model="qwen3:1.7b",
            status="fallback_ready",
            reason_code="SELECTION_MISSING",
            detail="Selection artifact does not cover suite 'code' for chat",
            recovery_commands=(
                "ollarma run --suites code --trials 3",
                "ollarma report --run-id <fresh_run_id>",
                "ollarma verify <fresh_run_id>",
            ),
        ),
        chat_selection=SelectionHealth(
            workload_class="chat",
            status="blocked",
            reason_code="SELECTION_MISSING",
            detail="Selection artifact does not cover suite 'code' for chat",
            recovery_commands=(
                "ollarma run --suites code --trials 3",
                "ollarma report --run-id <fresh_run_id>",
                "ollarma verify <fresh_run_id>",
            ),
        ),
        route_selection=SelectionHealth(
            workload_class="route_prompt",
            status="ready",
            model="qwen3-coder:7b",
        ),
        project_count=1,
    )


def _repo_root(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    repo_root = tmp_path / name
    repo_root.mkdir()
    return repo_root


def _write_run(
    repo_root: pathlib.Path,
    *,
    run_kind: str,
    run_id: str,
    stage: str,
    status: str,
) -> None:
    kind_dir = "runs" if run_kind == "workflow" else "autopilot"
    run_root = repo_root / ".ollarma" / kind_dir / run_id
    receipt = append_run_receipt(
        repo_root=repo_root,
        receipts_path=run_root / "receipts.json",
        receipt=RunReceipt(
            run_id=run_id,
            stage=stage,
            step_id="execute-script",
            task_or_command="workflow-step:execute-script",
            lane="workflow_execution_queue",
            status=status,
            inputs=({"repo_relative": f".ollarma/{kind_dir}/{run_id}/input.json"},),
            outputs=(),
            duration_s=0.1,
            retry_count=0,
            reason_code=None,
            checkpoint_ref={"repo_relative": f".ollarma/{kind_dir}/{run_id}/checkpoint.json"},
        ),
    )
    write_checkpoint_state(
        repo_root=repo_root,
        checkpoint_path=run_root / "checkpoint.json",
        state=CheckpointState(
            run_id=run_id,
            current_stage=stage,
            last_validated_stage=stage,
            last_receipt_hash=receipt.receipt_hash,
            retry_budget_remaining=0,
            resume_from_step="execute-script",
        ),
    )


def _write_route_receipt(repo_root: pathlib.Path, *, project: str, prompt_preview: str = "where is workflow") -> None:
    receipts_path = repo_root / ".ollarma" / "kb" / "route_receipts.jsonl"
    receipts_path.parent.mkdir(parents=True, exist_ok=True)
    receipts_path.write_bytes(
        orjson.dumps(
            {
                "project": project,
                "lane": "kb_direct",
                "reason_code": "KB_DIRECT_ANSWER",
                "query_class": "file_lookup",
                "kb_status": "ready",
                "evidence_count": 1,
                "next_action": "none",
                "selected_model": None,
                "prompt_preview": prompt_preview,
                "created_at": "2026-04-10T12:00:00Z",
            }
        )
        + b"\n"
    )


def test_get_dashboard_overview_aggregates_scheduler_and_runs(tmp_path: pathlib.Path, monkeypatch) -> None:
    from ollarma import service

    workflow_root = _repo_root(tmp_path, "workflow-project")
    autopilot_root = _repo_root(tmp_path, "autopilot-project")
    (workflow_root / "docs").mkdir()
    (workflow_root / "docs" / "workflow.md").write_text("workflow manifest guidance\n", encoding="utf-8")
    (autopilot_root / "docs").mkdir()
    (autopilot_root / "docs" / "notes.md").write_text("autopilot notes\n", encoding="utf-8")
    _write_run(workflow_root, run_kind="workflow", run_id="run-001", stage="execute", status="accepted")
    _write_run(autopilot_root, run_kind="autopilot", run_id="auto-001", stage="summarize", status="completed")
    _write_route_receipt(workflow_root, project="workflow-project")

    monkeypatch.setattr(
        service,
        "list_projects",
        lambda **kwargs: {
            "workflow-project": AdapterConfig(
                project_name="workflow-project",
                project_root=str(workflow_root),
                knowledge_base={"sources": [{"path": "docs", "kind": "documents"}]},
            ),
            "autopilot-project": AdapterConfig(
                project_name="autopilot-project",
                project_root=str(autopilot_root),
                knowledge_base={"sources": [{"path": "docs", "kind": "documents"}]},
            ),
        },
    )
    monkeypatch.setattr(
        service,
        "get_scheduler_snapshot",
        lambda: RuntimeSnapshot(
            active_job_id="job-123",
            active_lane="workflow_execution_queue",
            active_model="qwen3-coder:7b",
            active_project="workflow-project",
            queue_depth=1,
            queue_depth_by_lane={"workflow_execution_queue": 1},
            active_read_only=0,
            degraded_mode=False,
            resource_reason=None,
            swap_used_mb=256.0,
            loaded_models=("qwen3-coder:7b",),
            telemetry_source="test",
        ),
    )
    monkeypatch.setattr(service, "get_runtime_health", _mock_runtime_health)
    monkeypatch.setattr(
        service,
        "_probe_installed_model_names",
        lambda: (("qwen3:1.7b", "gemma3:4b-it-qat", "smollm2:latest", "nomic-embed-text:latest", "bge-m3:latest", "llava:7b"), None),
    )

    overview = service.get_dashboard_overview()

    assert isinstance(overview, DashboardOverview)
    assert overview.project_count == 2
    assert overview.projects == ("autopilot-project", "workflow-project")
    assert overview.workflow_runs[0].run_id == "run-001"
    assert overview.autopilot_runs[0].run_id == "auto-001"
    assert overview.kb_status[0].project == "autopilot-project"
    assert overview.kb_status[1].project == "workflow-project"
    assert any(item.title == "Generic dashboard chat is using a fallback model" for item in overview.readiness)
    assert any("KB not ready" in item.title for item in overview.readiness)
    assert overview.operator_resources[0].slug == "dashboard-start"
    assert overview.recent_route_receipts[0].reason_code == "KB_DIRECT_ANSWER"
    assert overview.boundary.dashboard_owner == "ollarma"
    assert overview.boundary.interactive is True
    assert overview.boundary.integration_contract == "links_exports_only"
    assert any(item.name == "gemma3:4b-it-qat" for item in overview.model_options)
    assert any(item.installed for item in overview.model_options if item.name == "gemma3:4b-it-qat")
    assert any(item.name == "smollm2" and item.installed for item in overview.model_options)
    assert not any("embed" in item.name for item in overview.model_options)
    assert not any(item.name.startswith("bge") or item.name.startswith("llava") for item in overview.model_options)


def test_get_dashboard_overview_degrades_missing_project_root(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    from ollarma import service

    missing_root = tmp_path / "missing-project"
    monkeypatch.setattr(
        service,
        "list_projects",
        lambda **kwargs: {
            "missing-project": AdapterConfig(
                project_name="missing-project",
                project_root=str(missing_root),
                knowledge_base={"sources": [{"path": "docs", "kind": "documents"}]},
            ),
        },
    )
    monkeypatch.setattr(
        service,
        "get_scheduler_snapshot",
        lambda: RuntimeSnapshot(
            active_job_id=None,
            active_lane=None,
            active_model=None,
            active_project=None,
            queue_depth=0,
            queue_depth_by_lane={},
            active_read_only=0,
            degraded_mode=False,
            resource_reason=None,
            swap_used_mb=0.0,
            loaded_models=(),
            telemetry_source="test",
        ),
    )
    monkeypatch.setattr(service, "get_runtime_health", _mock_runtime_health)
    monkeypatch.setattr(service, "_probe_installed_model_names", lambda: ((), None))

    overview = service.get_dashboard_overview()

    assert overview.project_count == 1
    assert overview.kb_status[0].project == "missing-project"
    assert overview.kb_status[0].status == "missing"
    assert overview.kb_status[0].reason_code == "PROJECT_ROOT_MISSING"


def test_get_dashboard_run_detail_finds_workflow_run(tmp_path: pathlib.Path, monkeypatch) -> None:
    from ollarma import service

    repo_root = _repo_root(tmp_path, "workflow-project")
    _write_run(repo_root, run_kind="workflow", run_id="run-001", stage="validate", status="passed")

    monkeypatch.setattr(
        service,
        "list_projects",
        lambda **kwargs: {
            "workflow-project": AdapterConfig(project_name="workflow-project", project_root=str(repo_root)),
        },
    )

    detail = service.get_dashboard_run_detail("run-001")

    assert detail.project == "workflow-project"
    assert detail.run_kind == "workflow"
    assert detail.current_stage == "validate"
    assert detail.receipt_ref is not None
    assert detail.checkpoint is not None


def test_render_dashboard_contains_run_links(tmp_path: pathlib.Path, monkeypatch) -> None:
    from ollarma import service

    repo_root = _repo_root(tmp_path, "workflow-project")
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "workflow.md").write_text("workflow manifest guidance\n", encoding="utf-8")
    _write_run(repo_root, run_kind="workflow", run_id="run-001", stage="execute", status="accepted")
    _write_route_receipt(repo_root, project="workflow-project")

    monkeypatch.setattr(
        service,
        "list_projects",
        lambda **kwargs: {
            "workflow-project": AdapterConfig(
                project_name="workflow-project",
                project_root=str(repo_root),
                knowledge_base={"sources": [{"path": "docs", "kind": "documents"}]},
            ),
        },
    )
    monkeypatch.setattr(
        service,
        "get_scheduler_snapshot",
        lambda: RuntimeSnapshot(
            active_job_id=None,
            active_lane=None,
            active_model=None,
            active_project=None,
            queue_depth=0,
            queue_depth_by_lane={"workflow_execution_queue": 0},
            active_read_only=0,
            degraded_mode=False,
            resource_reason=None,
            swap_used_mb=0.0,
            loaded_models=(),
            telemetry_source="test",
        ),
    )
    monkeypatch.setattr(service, "get_runtime_health", _mock_runtime_health)
    monkeypatch.setattr(
        service,
        "_probe_installed_model_names",
        lambda: (("qwen3:1.7b", "gemma3:4b-it-qat", "smollm2:latest", "nomic-embed-text:latest", "bge-m3:latest", "llava:7b"), None),
    )

    html = render_dashboard(service.get_dashboard_overview())

    assert "ollarma operator dashboard" in html
    assert "Chat With ollarma" in html
    assert "Run With Guardrails" in html
    assert "chat-mode" in html
    assert "project-routed help" in html
    assert "dashboard-execution-form" in html
    assert "workflow-manifest" in html
    assert "Run bounded execution" in html
    assert "Automatic (recommended)" in html
    assert "gemma3:4b-it-qat" in html
    assert "smollm2:latest" not in html
    assert "nomic-embed-text:latest" not in html
    assert "bge-m3:latest" not in html
    assert "llava:7b" not in html
    assert "what you are missing" in html
    assert "Generic dashboard chat is using a fallback model" in html
    assert "Important Resources, Commands, And Citations" in html
    assert "KB Status" in html
    assert "Recent Route Receipts" in html
    assert "/dashboard/runs/run-001" in html
    assert "docs/OLLARMA_DASHBOARD.md" in html
    assert "/Users/" not in html
    assert "adapters_dir" not in html
    assert "links exports only" in html
