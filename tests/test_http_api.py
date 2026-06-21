"""Tests for ollarma.http_api -- Starlette HTTP API exposing service tools.

Tests cover requirements HTTP-01 through HTTP-04:
  HTTP-01: Starlette app instance; GET /health returns 200
  HTTP-02: 5 business endpoints return correct status codes with mocked service
  HTTP-03: /openapi.json returns valid schema; /docs returns Swagger UI HTML
  HTTP-04: Origin validation rejects non-localhost; no dangerous tools; import guards
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """Fresh TestClient per test (no state bleed)."""
    from ollarma.http_api import app

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers for mocking service functions
# ---------------------------------------------------------------------------

def _mock_models_and_tasks():
    """Return a minimal ModelsAndTasks-like object with .model_dump()."""
    from ollarma.service import ModelsAndTasks
    from ollarma.registry import ModelConfig, TaskConfig

    return ModelsAndTasks(
        models=[ModelConfig(name="test-model", tier="primary")],
        tasks=[TaskConfig(id="t1", suite="science", prompt="test", num_ctx=4096)],
    )


def _mock_benchmark_result():
    """Return a minimal BenchmarkRunResult-like object with .model_dump()."""
    from ollarma.service import BenchmarkRunResult

    return BenchmarkRunResult(
        run_id="2026-01-01T00:00:00Z",
        rows_written=1,
        sealed_path="/tmp/test.json",
        evidence_path="/tmp/test.evidence.json",
        receipt_count=1,
        results=[],
        dry_run=True,
    )


def _mock_report_result():
    """Return a minimal ReportResult-like object with .model_dump()."""
    from ollarma.service import ReportResult

    return ReportResult(
        rows_loaded=1,
        stats=[],
        results_md="# Results",
        selection_md="# Selection",
        evidence_root="abc123",
        artifact_hash="def456",
    )


def _mock_verify_result():
    """Return a minimal VerifyResult-like object with .model_dump()."""
    from ollarma.service import VerifyResult

    return VerifyResult(
        valid=True,
        receipt_count=1,
        evidence_root="abc123",
    )


def _mock_kb_status():
    """Return a minimal KBStatus-like object with .model_dump()."""
    from ollarma.kb_search import KBStatus

    return KBStatus(
        project="overwatch",
        status="ready",
        freshness_hours=24,
        stale_behavior="escalate",
        built_at="2026-04-10T12:00:00Z",
        artifact_root=".ollarma/kb",
        search_db_path=".ollarma/kb/search.sqlite",
        document_count=12,
        chunk_count=30,
    )


def _mock_kb_search_result():
    """Return a minimal KBSearchResult-like object with .model_dump()."""
    from ollarma.kb_search import KBSearchHit, KBSearchResult

    return KBSearchResult(
        project="overwatch",
        query="manifest",
        status="ready",
        hit_count=1,
        hits=(
            KBSearchHit(
                chunk_id="chunk:1",
                document_id="doc:1",
                path="docs/workflow.md",
                chunk_index=0,
                authority="reference",
                source_kind="documents",
                score=1.0,
                text="Manifest-backed workflow guidance.",
                tags=("role:docs",),
            ),
        ),
    )


def _mock_dashboard_overview(*args, **kwargs):
    """Return a minimal DashboardOverview-like object with .model_dump()."""
    from ollarma.dashboard import (
        DashboardBoundaryInfo,
        DashboardCitation,
        DashboardCommandHint,
        DashboardOverview,
        DashboardReadinessItem,
        DashboardResource,
        DashboardRunSummary,
    )
    from ollarma.scheduler import RuntimeSnapshot

    return DashboardOverview(
        generated_at="2026-04-10T15:30:00Z",
        scheduler=RuntimeSnapshot(
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
        project_count=1,
        projects=("overwatch",),
        readiness=(
            DashboardReadinessItem(
                severity="info",
                title="No recent route receipts yet",
                detail="Try one project-routed help request.",
                action_label="Try project-routed help",
                action_command=None,
            ),
        ),
        operator_resources=(
            DashboardResource(
                slug="dashboard-start",
                title="Start With The Dashboard",
                category="operator",
                summary="Use the dashboard first.",
                why_it_matters="It is the review surface.",
                suggested_questions=("How do I use ollarma?",),
                citations=(
                    DashboardCitation(
                        label="dashboard doc",
                        repo_relative="docs/OLLARMA_DASHBOARD.md",
                    ),
                ),
                commands=(
                    DashboardCommandHint(
                        label="serve",
                        command="ollarma serve",
                        purpose="Start the dashboard.",
                    ),
                ),
            ),
        ),
        workflow_runs=(
            DashboardRunSummary(
                project="overwatch",
                run_id="run-001",
                run_kind="workflow",
                current_stage="execute",
                last_validated_stage="preflight",
                status="accepted",
                reason_code=None,
                step_id="execute-script",
                updated_at="2026-04-10T15:00:00Z",
                receipt_count=1,
                receipt_ref={"repo_relative": ".ollarma/runs/run-001/receipts.json"},
                checkpoint_ref={"repo_relative": ".ollarma/runs/run-001/checkpoint.json"},
            ),
        ),
        autopilot_runs=(),
        boundary=DashboardBoundaryInfo(
            dashboard_owner="ollarma",
            read_only=True,
            interactive=True,
            dashboard_path="/dashboard",
            overview_path="/dashboard/overview",
            run_detail_path_template="/dashboard/runs/{run_id}",
            review_gate="review when the ollarma dashboard is available",
            integration_contract="links_exports_only",
            portfolio_dashboard_dependency=False,
            helper_surfaces=("http.route",),
            deterministic_execution_surfaces=("cli.workflow",),
            safe_for=("running validated scripts under manifest control",),
            unsafe_for=("broad repo mutation through service-mode routing",),
            fallback_policy={"timeout": "escalate_to_frontier"},
        ),
    )


def _mock_runtime_health(*args, **kwargs):
    """Return a minimal RuntimeHealthResult for real dashboard overview assembly."""
    from ollarma.service import HelperModelResolution, RuntimeHealthResult, SelectionHealth

    return RuntimeHealthResult(
        status="ok",
        helper_chat=HelperModelResolution(
            effective_model="qwen2.5:1.5b",
            status="fallback_ready",
        ),
        chat_selection=SelectionHealth(
            workload_class="chat",
            status="ready",
            model="qwen2.5:1.5b",
        ),
        route_selection=SelectionHealth(
            workload_class="route_prompt",
            status="ready",
            model="qwen3-coder:7b",
        ),
        project_count=1,
    )


def _mock_dashboard_run_detail():
    """Return a minimal DashboardRunDetail-like object with .model_dump()."""
    from ollarma.dashboard import (
        DashboardCheckpointSummary,
        DashboardReceiptPreview,
        DashboardRunDetail,
    )

    return DashboardRunDetail(
        project="overwatch",
        run_id="run-001",
        run_kind="workflow",
        current_stage="execute",
        last_validated_stage="preflight",
        status="accepted",
        reason_code=None,
        step_id="execute-script",
        updated_at="2026-04-10T15:00:00Z",
        receipt_count=1,
        receipt_ref={"repo_relative": ".ollarma/runs/run-001/receipts.json"},
        checkpoint_ref={"repo_relative": ".ollarma/runs/run-001/checkpoint.json"},
        checkpoint=DashboardCheckpointSummary(
            current_stage="execute",
            last_validated_stage="preflight",
            retry_budget_remaining=1,
            resume_from_step="execute-script",
            last_receipt_hash="abc123",
        ),
        receipts=(
            DashboardReceiptPreview(
                stage="preflight",
                step_id="execute-script",
                lane="workflow_execution_queue",
                status="accepted",
                retry_count=0,
                reason_code=None,
                created_at="2026-04-10T15:00:00Z",
                checkpoint_ref={"repo_relative": ".ollarma/runs/run-001/checkpoint.json"},
            ),
        ),
    )


def _mock_dashboard_workflow_catalog():
    """Return a minimal DashboardWorkflowCatalog-like object with .model_dump()."""
    from ollarma.dashboard import (
        DashboardWorkflowCatalog,
        DashboardWorkflowManifest,
        DashboardWorkflowStep,
    )

    return DashboardWorkflowCatalog(
        project="overwatch",
        manifests=(
            DashboardWorkflowManifest(
                manifest_ref={"repo_relative": ".ollarma/manifests/workflow.json"},
                manifest_digest="sha256:manifest",
                run_id="run-001",
                consumer_repo="science/consumer",
                steps=(
                    DashboardWorkflowStep(
                        step_id="execute-script",
                        stage="execute",
                        task_type="validated-script",
                    ),
                ),
            ),
        ),
    )


def _mock_autopilot_report():
    """Return a minimal AutopilotReport-like object with .model_dump()."""
    from ollarma.autopilot import (
        AssetInventory,
        AutopilotReport,
        DiscoveredAsset,
        TierMapping,
    )

    return AutopilotReport(
        project_name="overwatch",
        project_root="/tmp/overwatch",
        inventory=AssetInventory(
            project_name="overwatch",
            project_root="/tmp/overwatch",
            assets=(
                DiscoveredAsset(
                    path="/tmp/overwatch/scripts/run.py",
                    asset_type="script",
                    has_entrypoint=True,
                ),
            ),
            counts={"script": 1},
        ),
        tier_map={
            "code": TierMapping(
                tier="code",
                model="qwen3-coder:7b",
                model_size_b=7.0,
                quality_mean=0.93,
                degraded_confidence=False,
            ),
        },
        results=(),
        total_assets=1,
        passed=0,
        failed=0,
        escalation_needed=0,
        tokens_consumed_local=0,
        run_executed=False,
    )


# ---------------------------------------------------------------------------
# HTTP-01: TestStartup
# ---------------------------------------------------------------------------


class TestStartup:
    """HTTP-01: Starlette app instance and health endpoint."""

    def test_app_is_starlette_instance(self):
        """from ollarma.http_api import app is a Starlette instance."""
        from starlette.applications import Starlette
        from ollarma.http_api import app

        assert isinstance(app, Starlette), f"app is {type(app)}, expected Starlette"

    def test_health_endpoint(self, client):
        """GET /health returns 200 with readiness details.

        v4.5 Phase 51 PERSIST-03: top-level status now folds startup readiness,
        so it is one of {ready, degraded, blocked} — never the legacy "ok".
        """
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in {"ready", "degraded", "blocked"}
        assert "helper_chat" in data
        assert "chat_selection" in data
        assert "route_selection" in data
        # v4.5 Phase 51 PERSIST-02: startup_readiness block is embedded.
        assert "startup_readiness" in data


# ---------------------------------------------------------------------------
# HTTP-02: TestEndpoints
# ---------------------------------------------------------------------------


class TestEndpoints:
    """HTTP-02: 5 business endpoints return correct status codes."""

    def test_list_models(self, client, monkeypatch):
        """GET /models returns 200 with 'models' and 'tasks' keys."""
        import ollarma.http_api as http_mod

        monkeypatch.setattr(
            http_mod.service, "list_models_and_tasks", _mock_models_and_tasks
        )
        response = client.get("/models")
        assert response.status_code == 200
        data = response.json()
        assert "models" in data
        assert "tasks" in data

    def test_list_models_no_models(self, client, monkeypatch):
        """GET /models returns 404 when NoModelsError raised."""
        import ollarma.http_api as http_mod
        from ollarma.service import NoModelsError

        def raise_no_models():
            raise NoModelsError("No models found")

        monkeypatch.setattr(
            http_mod.service, "list_models_and_tasks", raise_no_models
        )
        response = client.get("/models")
        assert response.status_code == 404
        assert "error" in response.json()

    def test_run_benchmark(self, client, monkeypatch):
        """POST /run with {"dry_run": true} returns 200."""
        import ollarma.http_api as http_mod

        monkeypatch.setattr(
            http_mod.service,
            "run_benchmark",
            lambda **kwargs: _mock_benchmark_result(),
        )
        response = client.post("/run", json={"dry_run": True})
        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == "2026-01-01T00:00:00Z"

    def test_run_benchmark_default_dry_run(self, client, monkeypatch):
        """POST /run with {} defaults dry_run=True."""
        import ollarma.http_api as http_mod

        captured = {}

        def capture_kwargs(**kwargs):
            captured.update(kwargs)
            return _mock_benchmark_result()

        monkeypatch.setattr(http_mod.service, "run_benchmark", capture_kwargs)
        response = client.post("/run", json={})
        assert response.status_code == 200
        assert captured.get("dry_run") is True

    def test_get_report_latest(self, client, monkeypatch):
        """GET /report returns 200 (latest report)."""
        import ollarma.http_api as http_mod

        monkeypatch.setattr(
            http_mod.service,
            "generate_report",
            lambda **kwargs: _mock_report_result(),
        )
        response = client.get("/report")
        assert response.status_code == 200
        data = response.json()
        assert "results_md" in data

    def test_get_report_by_id(self, client, monkeypatch):
        """GET /report/2026-01-01T00:00:00Z returns 200."""
        import ollarma.http_api as http_mod

        captured = {}

        def capture_report(**kwargs):
            captured.update(kwargs)
            return _mock_report_result()

        monkeypatch.setattr(http_mod.service, "generate_report", capture_report)
        response = client.get("/report/2026-01-01T00:00:00Z")
        assert response.status_code == 200
        assert captured.get("run_id") == "2026-01-01T00:00:00Z"

    def test_verify_evidence(self, client, monkeypatch):
        """GET /verify/2026-01-01T00:00:00Z returns 200."""
        import ollarma.http_api as http_mod

        monkeypatch.setattr(
            http_mod.service,
            "verify_evidence",
            lambda **kwargs: _mock_verify_result(),
        )
        response = client.get("/verify/2026-01-01T00:00:00Z")
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True

    def test_verify_invalid(self, client, monkeypatch):
        """GET /verify/bad-id returns 404 when FileNotFoundError raised."""
        import ollarma.http_api as http_mod

        def raise_not_found(**kwargs):
            raise FileNotFoundError("No such run")

        monkeypatch.setattr(http_mod.service, "verify_evidence", raise_not_found)
        response = client.get("/verify/bad-id")
        assert response.status_code == 404
        assert "error" in response.json()

    def test_list_projects(self, client, monkeypatch):
        """GET /projects returns 200."""
        import ollarma.http_api as http_mod
        from ollarma.fleet import AdapterConfig

        mock_adapter = AdapterConfig(
            project_name="test-project",
            project_root="/tmp/test",
        )
        monkeypatch.setattr(
            http_mod.service,
            "list_projects",
            lambda **kwargs: {"test-project": mock_adapter},
        )
        response = client.get("/projects")
        assert response.status_code == 200
        data = response.json()
        assert "test-project" in data

    def test_dashboard_workflows(self, client, monkeypatch):
        """GET /dashboard/workflows/{project} returns discovered manifest metadata."""
        import ollarma.http_api as http_mod

        monkeypatch.setattr(
            http_mod.service,
            "list_project_workflows",
            lambda *args, **kwargs: _mock_dashboard_workflow_catalog(),
        )
        response = client.get("/dashboard/workflows/overwatch")
        assert response.status_code == 200
        data = response.json()
        assert data["project"] == "overwatch"
        assert data["manifests"][0]["steps"][0]["step_id"] == "execute-script"

    def test_dashboard_workflows_not_found(self, client, monkeypatch):
        """GET /dashboard/workflows/{project} returns 404 when project resolution fails."""
        import ollarma.http_api as http_mod

        def raise_missing(*args, **kwargs):
            raise ValueError("Project 'missing' not found in fleet registry")

        monkeypatch.setattr(http_mod.service, "list_project_workflows", raise_missing)
        response = client.get("/dashboard/workflows/missing")
        assert response.status_code == 404
        assert "error" in response.json()

    def test_autopilot(self, client, monkeypatch):
        """POST /autopilot returns a bounded autopilot report."""
        import ollarma.http_api as http_mod

        monkeypatch.setattr(
            http_mod.service,
            "submit_autopilot",
            lambda **kwargs: _mock_autopilot_report(),
        )
        response = client.post("/autopilot", json={"project": "overwatch"})
        assert response.status_code == 200
        data = response.json()
        assert data["project_name"] == "overwatch"
        assert data["run_executed"] is False

    def test_autopilot_requires_project(self, client):
        """POST /autopilot validates required project input."""
        response = client.post("/autopilot", json={})
        assert response.status_code == 400
        assert response.json()["error"] == "project is required"

    def test_chat(self, client, monkeypatch):
        """POST /chat returns 200 with response and model."""
        import ollarma.http_api as http_mod
        from ollarma.service import ChatResult

        monkeypatch.setattr(
            http_mod.service,
            "chat_with_model",
            lambda **kwargs: ChatResult(response="hello", model="qwen3:4b"),
        )
        response = client.post("/chat", json={"message": "hi"})
        assert response.status_code == 200
        data = response.json()
        assert data["response"] == "hello"
        assert data["model"] == "qwen3:4b"

    def test_chat_requires_message(self, client):
        """POST /chat without message returns 400."""
        response = client.post("/chat", json={})
        assert response.status_code == 400
        assert "error" in response.json()

    def test_chat_rejects_reserved_model_as_policy_denial(self, client):
        """POST /chat maps reserved-model policy denial to 403, not upstream 502."""
        from ollarma.reserved_models import RESERVED_MODEL_REASON_CODE

        response = client.post(
            "/chat",
            json={"message": "hi", "model": "qwen3:1.7b"},
        )

        assert response.status_code == 403
        data = response.json()
        assert data["reason_code"] == RESERVED_MODEL_REASON_CODE
        assert RESERVED_MODEL_REASON_CODE in data["error"]

    def test_chat_falls_back_to_installed_model_when_selection_missing(self, client, monkeypatch):
        """POST /chat uses an installed local model when no validated selection artifact exists."""
        import ollarma.http_api as http_mod
        from ollarma.execution_policy import SelectionResolutionError, WorkloadClass

        def _raise_selection(*args, **kwargs):
            raise SelectionResolutionError(
                "SELECTION_MISSING",
                WorkloadClass.CHAT,
                "No validated selection artifact found in results/",
            )

        monkeypatch.setattr(http_mod.service, "resolve_default_model", _raise_selection)
        monkeypatch.setattr(
            http_mod.service.ollama,
            "Client",
            lambda: type(
                "FakeClient",
                (),
                {
                    "list": lambda self: type(
                        "FakeList",
                        (),
                        {"models": [type("FakeModel", (), {"model": "qwen2.5:1.5b"})()]},
                    )(),
                    "chat": lambda self, *, model, messages: type(
                        "FakeResponse",
                        (),
                        {"message": type("FakeMessage", (), {"content": "ready"})()},
                    )(),
                },
            )(),
        )
        response = client.post("/chat", json={"message": "hi"})

        assert response.status_code == 200
        assert response.json()["model"] == "qwen2.5:1.5b"
        assert response.json()["response"] == "ready"

    def test_chat_returns_blocked_result_when_selection_and_fallback_missing(self, client, monkeypatch):
        """POST /chat remains interactive with deterministic recovery guidance when no model is selectable."""
        import ollarma.http_api as http_mod
        from ollarma.execution_policy import SelectionResolutionError, WorkloadClass

        def _raise_selection(*args, **kwargs):
            raise SelectionResolutionError(
                "SELECTION_MISSING",
                WorkloadClass.CHAT,
                "Selection artifact does not cover suite 'code' for chat",
            )

        monkeypatch.setattr(http_mod.service, "resolve_default_model", _raise_selection)
        monkeypatch.setattr(
            http_mod.service.ollama,
            "Client",
            lambda: type(
                "FakeClient",
                (),
                {"list": lambda self: (_ for _ in ()).throw(RuntimeError("offline"))},
            )(),
        )

        response = client.post("/chat", json={"message": "hi"})
        data = response.json()

        assert response.status_code == 200
        assert data["status"] == "blocked"
        assert data["model"] == "unavailable"
        assert data["reason_code"] == "SELECTION_MISSING"
        assert data["detail"] == (
            "Selection artifact does not cover suite 'code' for chat. "
            "Local Ollama daemon probe failed: offline"
        )
        assert data["recovery_commands"] == [
            "ollama list",
            "ollama pull qwen2.5:1.5b",
            "ollarma run --suites code --trials 3",
            "ollarma report --run-id <fresh_run_id>",
            "ollarma verify <fresh_run_id>",
        ]

    def test_route_prompt(self, client, monkeypatch):
        """POST /route returns 200 with routed project response."""
        import ollarma.http_api as http_mod
        from ollarma.service import RouteResult

        monkeypatch.setattr(
            http_mod.service,
            "route_prompt",
            lambda **kwargs: RouteResult(
                final_response="done",
                tool_calls_count=0,
                model=None,
                project="overwatch",
                lane="kb_direct",
                reason_code="KB_DIRECT_ANSWER",
                query_class="file_lookup",
                kb_status="ready",
            ),
        )
        response = client.post(
            "/route",
            json={"prompt": "summarize drift", "project": "overwatch"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["final_response"] == "done"
        assert data["project"] == "overwatch"
        assert data["lane"] == "kb_direct"
        assert data["reason_code"] == "KB_DIRECT_ANSWER"

    def test_route_prompt_requires_project(self, client):
        """POST /route without project returns 400."""
        response = client.post("/route", json={"prompt": "hi"})
        assert response.status_code == 400
        assert "error" in response.json()

    def test_route_prompt_rejects_reserved_model_as_policy_denial(self, client, monkeypatch):
        """POST /route maps reserved-model policy denial to 403, not not-found/upstream errors."""
        import ollarma.http_api as http_mod
        from ollarma.reserved_models import RESERVED_MODEL_REASON_CODE

        def _raise_reserved(*args, **kwargs):
            raise ValueError(f"{RESERVED_MODEL_REASON_CODE}: reserved model")

        monkeypatch.setattr(http_mod.service, "route_prompt", _raise_reserved)

        response = client.post(
            "/route",
            json={"prompt": "summarize drift", "project": "overwatch", "model": "qwen3:1.7b"},
        )

        assert response.status_code == 403
        data = response.json()
        assert data["reason_code"] == RESERVED_MODEL_REASON_CODE

    def test_kb_status(self, client, monkeypatch):
        """GET /kb/status/{project} returns read-only KB status."""
        import ollarma.http_api as http_mod

        monkeypatch.setattr(http_mod.service, "get_project_kb_status", lambda *args, **kwargs: _mock_kb_status())
        response = client.get("/kb/status/overwatch")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["search_db_path"] == ".ollarma/kb/search.sqlite"

    def test_kb_search(self, client, monkeypatch):
        """POST /kb/search returns bounded KB hits."""
        import ollarma.http_api as http_mod

        monkeypatch.setattr(http_mod.service, "search_project_kb", lambda *args, **kwargs: _mock_kb_search_result())
        response = client.post(
            "/kb/search",
            json={"project": "overwatch", "query": "manifest", "limit": 3},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["hit_count"] == 1
        assert data["hits"][0]["path"] == "docs/workflow.md"

    def test_kb_search_requires_project_and_query(self, client):
        """POST /kb/search without project/query returns 400."""
        response = client.post("/kb/search", json={"project": "overwatch"})
        assert response.status_code == 400
        assert "error" in response.json()

    def test_kb_search_rejects_invalid_limit(self, client):
        """POST /kb/search validates limit before delegating."""
        response = client.post(
            "/kb/search",
            json={"project": "overwatch", "query": "manifest", "limit": 0},
        )
        assert response.status_code == 400
        assert "positive integer" in response.json()["error"]

    def test_workflow(self, client, monkeypatch):
        """POST /workflow returns explicit workflow admission metadata."""
        import ollarma.http_api as http_mod
        from ollarma.service import WorkflowSubmissionResult
        from ollarma.scheduler import RuntimeSnapshot

        captured: dict[str, object] = {}
        manifest_ref = {
            "repo_relative": ".ollarma/manifests/workflow.json",
            "digest": "sha256:deadbeef",
        }

        def _submit_workflow(**kwargs):
            captured.update(kwargs)
            return WorkflowSubmissionResult(
                project="overwatch",
                manifest_ref=manifest_ref,
                manifest_digest="sha256:manifest",
                run_id="run-001",
                step_id="execute-script",
                task_class="validated-script",
                lane="workflow_execution_queue",
                status="accepted",
                model="qwen3-coder:7b",
                queue_depth=0,
                reason_code=None,
                scheduler=RuntimeSnapshot(
                    active_job_id=None,
                    active_lane=None,
                    active_model=None,
                    active_project=None,
                    queue_depth=0,
                    queue_depth_by_lane={
                        "read_only_non_inference": 0,
                        "local_inference_single": 0,
                        "workflow_execution_queue": 0,
                    },
                    active_read_only=0,
                ),
                escalation_receipt=None,
            )

        monkeypatch.setattr(
            http_mod.service,
            "submit_workflow",
            _submit_workflow,
        )
        response = client.post(
            "/workflow",
            json={
                "project": "overwatch",
                "manifest_ref": manifest_ref,
                "step_id": "execute-script",
            },
        )
        assert response.status_code == 200
        assert response.json()["lane"] == "workflow_execution_queue"
        assert captured["manifest_ref"] == manifest_ref

    def test_workflow_requires_fields(self, client):
        """POST /workflow without required fields returns 400."""
        response = client.post("/workflow", json={"project": "overwatch"})
        assert response.status_code == 400
        assert "error" in response.json()

    def test_dashboard_html(self, client, monkeypatch):
        """GET /dashboard returns server-rendered HTML for the operator surface."""
        import ollarma.http_api as http_mod

        monkeypatch.setattr(http_mod.service, "get_dashboard_overview", _mock_dashboard_overview)
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "ollarma operator dashboard" in response.text
        assert "Chat With ollarma" in response.text
        assert "chat-mode" in response.text
        assert "Important Resources, Commands, And Citations" in response.text
        assert "/dashboard/runs/run-001" in response.text

    def test_dashboard_overview_json(self, client, monkeypatch):
        """GET /dashboard/overview returns typed dashboard JSON."""
        import ollarma.http_api as http_mod

        monkeypatch.setattr(http_mod.service, "get_dashboard_overview", _mock_dashboard_overview)
        response = client.get("/dashboard/overview")
        assert response.status_code == 200
        data = response.json()
        assert data["project_count"] == 1
        assert data["workflow_runs"][0]["run_id"] == "run-001"

    def test_dashboard_overview_degrades_missing_project_root_http(
        self,
        client,
        monkeypatch,
        tmp_path,
    ):
        """GET /dashboard/overview degrades missing project roots instead of 500ing."""
        import ollarma.http_api as http_mod
        from ollarma.fleet import AdapterConfig
        from ollarma.scheduler import RuntimeSnapshot

        missing_root = tmp_path / "missing-project"
        monkeypatch.setattr(
            http_mod.service,
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
            http_mod.service,
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
        monkeypatch.setattr(http_mod.service, "get_runtime_health", _mock_runtime_health)
        monkeypatch.setattr(http_mod.service, "_probe_installed_model_names", lambda: ((), None))

        response = client.get("/dashboard/overview")

        assert response.status_code == 200
        data = response.json()
        assert data["project_count"] == 1
        assert data["kb_status"][0]["project"] == "missing-project"
        assert data["kb_status"][0]["status"] == "missing"
        assert data["kb_status"][0]["reason_code"] == "PROJECT_ROOT_MISSING"

    def test_dashboard_run_detail_json(self, client, monkeypatch):
        """GET /dashboard/runs/{run_id} returns run drill-down JSON."""
        import ollarma.http_api as http_mod

        monkeypatch.setattr(
            http_mod.service,
            "get_dashboard_run_detail",
            lambda *args, **kwargs: _mock_dashboard_run_detail(),
        )
        response = client.get("/dashboard/runs/run-001")
        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == "run-001"
        assert data["checkpoint"]["current_stage"] == "execute"

    def test_dashboard_run_detail_not_found(self, client, monkeypatch):
        """GET /dashboard/runs/{run_id} returns 404 when no run exists."""
        import ollarma.http_api as http_mod

        def _missing(*args, **kwargs):
            raise FileNotFoundError("dashboard run not found")

        monkeypatch.setattr(http_mod.service, "get_dashboard_run_detail", _missing)
        response = client.get("/dashboard/runs/missing")
        assert response.status_code == 404
        assert "error" in response.json()

    def test_workflow_queued_returns_202(self, client, monkeypatch):
        """POST /workflow returns 202 when admission is queued behind active work."""
        import ollarma.http_api as http_mod
        from ollarma.service import WorkflowSubmissionResult
        from ollarma.scheduler import RuntimeSnapshot

        monkeypatch.setattr(
            http_mod.service,
            "submit_workflow",
            lambda **kwargs: WorkflowSubmissionResult(
                project="overwatch",
                manifest_ref={"repo_relative": ".ollarma/manifests/workflow.json"},
                manifest_digest="sha256:manifest",
                run_id="run-001",
                step_id="execute-script",
                task_class="validated-script",
                lane="workflow_execution_queue",
                status="queued",
                model="qwen3-coder:7b",
                queue_depth=1,
                reason_code=None,
                scheduler=RuntimeSnapshot(
                    active_job_id="job-1",
                    active_lane="local_inference_single",
                    active_model="qwen3:4b",
                    active_project="busy",
                    queue_depth=0,
                    queue_depth_by_lane={
                        "read_only_non_inference": 0,
                        "local_inference_single": 0,
                        "workflow_execution_queue": 0,
                    },
                    active_read_only=0,
                ),
                escalation_receipt=None,
            ),
        )
        response = client.post(
            "/workflow",
            json={
                "project": "overwatch",
                "manifest_ref": ".ollarma/manifests/workflow.json",
                "step_id": "execute-script",
            },
        )
        assert response.status_code == 202
        assert response.json()["status"] == "queued"

    def test_workflow_dependency_missing_returns_409(self, client, monkeypatch):
        """POST /workflow returns a deterministic DEPENDENCY_MISSING payload."""
        import ollarma.http_api as http_mod
        from ollarma.service import WorkflowSubmissionResult
        from ollarma.scheduler import RuntimeSnapshot

        monkeypatch.setattr(
            http_mod.service,
            "submit_workflow",
            lambda **kwargs: WorkflowSubmissionResult(
                project="overwatch",
                manifest_ref={"repo_relative": ".ollarma/manifests/workflow.json"},
                manifest_digest="sha256:manifest",
                run_id="run-001",
                step_id="execute-script",
                task_class="validated-notebook",
                lane="workflow_execution_queue",
                status="rejected",
                model=None,
                queue_depth=0,
                reason_code="DEPENDENCY_MISSING",
                scheduler=RuntimeSnapshot(
                    active_job_id=None,
                    active_lane=None,
                    active_model=None,
                    active_project=None,
                    queue_depth=0,
                    queue_depth_by_lane={
                        "read_only_non_inference": 0,
                        "local_inference_single": 0,
                        "workflow_execution_queue": 0,
                    },
                    active_read_only=0,
                ),
                escalation_receipt={"reason_code": "DEPENDENCY_MISSING"},
            ),
        )
        response = client.post(
            "/workflow",
            json={
                "project": "overwatch",
                "manifest_ref": ".ollarma/manifests/workflow.json",
                "step_id": "execute-script",
            },
        )
        assert response.status_code == 409
        assert response.json()["reason_code"] == "DEPENDENCY_MISSING"


# ---------------------------------------------------------------------------
# HTTP-03: TestOpenAPI
# ---------------------------------------------------------------------------


class TestOpenAPI:
    """HTTP-03: OpenAPI schema and Swagger UI docs."""

    def test_openapi_json(self, client):
        """GET /openapi.json returns 200 with 'openapi' and 'paths' keys."""
        from ollarma import __version__

        response = client.get("/openapi.json")
        assert response.status_code == 200
        data = response.json()
        assert "openapi" in data
        assert "paths" in data
        assert data["info"]["version"] == __version__

    def test_openapi_paths_not_empty(self, client):
        """GET /openapi.json paths dict has entries for core and chat/routing endpoints."""
        response = client.get("/openapi.json")
        data = response.json()
        paths = data["paths"]
        expected_paths = [
            "/models",
            "/run",
            "/report",
            "/verify/{run_id}",
            "/projects",
            "/chat",
            "/route",
            "/workflow",
        ]
        for p in expected_paths:
            assert p in paths, f"Missing path {p} in OpenAPI schema. Got: {list(paths.keys())}"

    def test_docs_html(self, client):
        """GET /docs returns 200 with content-type text/html containing 'swagger-ui'."""
        response = client.get("/docs")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert "swagger-ui" in response.text.lower()


# ---------------------------------------------------------------------------
# HTTP-04: TestSecurity
# ---------------------------------------------------------------------------


class TestSecurity:
    """HTTP-04: Origin validation, import guards, no dangerous tools, semaphore."""

    def test_origin_rejected(self, client):
        """GET /models with Origin: http://evil.com returns 403."""
        response = client.get("/health", headers={"Origin": "http://evil.com"})
        assert response.status_code == 403
        assert "error" in response.json()

    def test_origin_localhost_allowed(self, client):
        """GET /health with Origin: http://127.0.0.1 returns 200."""
        response = client.get("/health", headers={"Origin": "http://127.0.0.1"})
        assert response.status_code == 200

    def test_origin_localhost_with_port(self, client):
        """GET /health with Origin: http://localhost:3000 returns 200."""
        response = client.get("/health", headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 200

    def test_no_origin_allowed(self, client):
        """GET /health with no Origin header returns 200."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_import_guards_no_rich(self):
        """http_api.py source has no rich imports."""
        from ollarma import http_api

        source = inspect.getsource(http_api)
        import_lines = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        rich_imports = [
            line for line in import_lines
            if line.startswith("from rich") or line.startswith("import rich")
        ]
        assert not rich_imports, f"http_api.py has Rich imports: {rich_imports}"

    def test_import_guards_no_typer(self):
        """http_api.py source has no typer imports."""
        from ollarma import http_api

        source = inspect.getsource(http_api)
        import_lines = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        typer_imports = [
            line for line in import_lines
            if line.startswith("from typer") or line.startswith("import typer")
        ]
        assert not typer_imports, f"http_api.py has Typer imports: {typer_imports}"

    def test_no_dangerous_tools_in_routes(self):
        """Route paths do not include /bash, /edit, /tools."""
        from ollarma.http_api import app

        route_paths = [r.path for r in app.routes]
        dangerous = {"/bash", "/edit", "/tools", "/run_bash", "/edit_file"}
        overlap = set(route_paths) & dangerous
        assert not overlap, f"Dangerous routes exposed: {overlap}"

    def test_inference_semaphore_exists(self):
        """Module-level _inference_sem is asyncio.Semaphore(1)."""
        from ollarma import http_api

        assert hasattr(http_api, "_inference_sem"), "Missing _inference_sem"
        sem = http_api._inference_sem
        assert isinstance(sem, asyncio.Semaphore), (
            f"_inference_sem is {type(sem)}, expected asyncio.Semaphore"
        )
        assert sem._value == 1, f"Semaphore value is {sem._value}, expected 1"


# ---------------------------------------------------------------------------
# NS-01 / NS-02: Namespace prefix threading through HTTP transport
# ---------------------------------------------------------------------------


class TestNamespaceTransport:
    """NS-01 + NS-02: namespace_prefix accepted and validated via HTTP transport."""

    def _mock_route_result(self):
        from ollarma.service import RouteResult
        return RouteResult(
            final_response="routed",
            tool_calls_count=0,
            model=None,
            project="overwatch",
            lane="kb_direct",
            reason_code="KB_DIRECT_ANSWER",
            query_class="file_lookup",
            kb_status="ready",
        )

    def test_route_accepts_namespace_prefix_in_body(self, client, monkeypatch):
        """POST /route with namespace_prefix in body passes it to service and returns 200."""
        import ollarma.http_api as http_mod
        from ollarma.namespace_registry import NAMESPACE_REGISTRY

        captured = {}

        def _fake_route(**kwargs):
            captured.update(kwargs)
            return self._mock_route_result()

        monkeypatch.setattr(http_mod.service, "route_prompt", _fake_route)
        monkeypatch.setattr(NAMESPACE_REGISTRY, "is_registered", lambda prefix: True)

        response = client.post(
            "/route",
            json={"prompt": "hello", "project": "overwatch", "namespace_prefix": "ollarma-demo:"},
        )
        assert response.status_code == 200
        assert captured.get("namespace_prefix") == "ollarma-demo:"

    def test_route_unknown_namespace_returns_400(self, client, monkeypatch):
        """POST /route with an unregistered namespace_prefix returns 400 with reason_code UNKNOWN_NAMESPACE."""
        import ollarma.http_api as http_mod

        def _raise_unknown(**kwargs):
            raise ValueError("UNKNOWN_NAMESPACE: 'bogus:' is not a registered namespace prefix")

        monkeypatch.setattr(http_mod.service, "route_prompt", _raise_unknown)

        response = client.post(
            "/route",
            json={"prompt": "hello", "project": "overwatch", "namespace_prefix": "bogus:"},
        )
        assert response.status_code == 400
        data = response.json()
        assert data.get("reason_code") == "UNKNOWN_NAMESPACE"
        assert "UNKNOWN_NAMESPACE" in data.get("error", "")

    def test_route_absent_namespace_prefix_is_backward_compatible(self, client, monkeypatch):
        """POST /route without namespace_prefix returns 200 (unscoped, no NS-02 failure)."""
        import ollarma.http_api as http_mod

        captured = {}

        def _fake_route(**kwargs):
            captured.update(kwargs)
            return self._mock_route_result()

        monkeypatch.setattr(http_mod.service, "route_prompt", _fake_route)

        response = client.post(
            "/route",
            json={"prompt": "hello", "project": "overwatch"},
        )
        assert response.status_code == 200
        # namespace_prefix should be empty string (or absent) — never non-empty
        assert captured.get("namespace_prefix", "") == ""

    def test_route_accepts_x_namespace_prefix_header(self, client, monkeypatch):
        """POST /route with X-Namespace-Prefix header (no body key) uses the header value."""
        import ollarma.http_api as http_mod
        from ollarma.namespace_registry import NAMESPACE_REGISTRY

        captured = {}

        def _fake_route(**kwargs):
            captured.update(kwargs)
            return self._mock_route_result()

        monkeypatch.setattr(http_mod.service, "route_prompt", _fake_route)
        monkeypatch.setattr(NAMESPACE_REGISTRY, "is_registered", lambda prefix: True)

        response = client.post(
            "/route",
            json={"prompt": "hello", "project": "overwatch"},
            headers={"X-Namespace-Prefix": "ollarma-demo:"},
        )
        assert response.status_code == 200
        assert captured.get("namespace_prefix") == "ollarma-demo:"
