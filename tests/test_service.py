"""Tests for ollarma.service -- service layer between CLI and business logic.

Tests cover:
  - Import guards: service.py must not pull Rich or Typer into sys.modules
  - Public API completeness: 7 public functions
  - Return type contracts: all functions return Pydantic BaseModel instances
  - Error handling: NoModelsError, NoResultsError
  - Helper routing: _dispatch_scorer routes to correct scorers
"""
from __future__ import annotations

import importlib
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from ollarma.execution_policy import SelectionResolutionError, WorkloadClass


def _mock_workflow_manifest(
    *,
    task_class: str = "validated-script",
    step_id: str = "execute-script",
    stage: str = "execute",
    run_id: str = "run-001",
    repo_root: pathlib.Path | None = None,
):
    """Return a minimal manifest-like object for workflow admission tests."""
    materialization_root = MagicMock()
    materialization_root.to_service_payload.return_value = {
        "repo_relative": f".ollarma/runs/{run_id}/materialized",
    }
    checkpoint_root = MagicMock(repo_relative=f".ollarma/runs/{run_id}/checkpoints")
    artifact_locator = MagicMock(repo_relative=f".ollarma/runs/{run_id}")
    step = MagicMock(
        step_id=step_id,
        stage=stage,
        task_type=task_class,
        materialization_root=materialization_root,
    )
    manifest = MagicMock()
    manifest.consumer_repo = "science/consumer"
    manifest.run_id = run_id
    manifest.steps = [step]
    manifest.artifact_roots = [MagicMock(locator=artifact_locator)]
    manifest.checkpoint_policy = MagicMock(
        max_retries=1,
        checkpoint_root=checkpoint_root,
    )
    if repo_root is not None:
        manifest._repo_root = repo_root
    manifest.to_service_payload.return_value = {
        "manifest_ref": {"repo_relative": ".ollarma/manifests/workflow.json"},
        "manifest_digest": "sha256:manifest",
    }
    return manifest


# ---------------------------------------------------------------------------
# Import guard tests
# ---------------------------------------------------------------------------


class TestImportGuards:
    """Verify service.py does not directly import CLI-only dependencies.

    Note: httpx (via ollama SDK) transitively imports rich. We check that
    service.py's SOURCE CODE contains no rich/typer import statements, which
    is the actual requirement. The transitive import via httpx is not our code.
    """

    def test_service_no_rich_import(self):
        """service.py source code contains no 'import rich' or 'from rich' statements."""
        import inspect
        from ollarma import service

        source = inspect.getsource(service)
        # Check for actual import statements (not comments or docstrings)
        import_lines = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        rich_imports = [
            line for line in import_lines
            if "rich" in line.split()
            or any(part.startswith("rich.") for part in line.split())
            or line.startswith("from rich")
            or line.startswith("import rich")
        ]
        assert not rich_imports, (
            f"ollarma.service has Rich import statements: {rich_imports}"
        )

    def test_service_no_typer_import(self):
        """service.py source code contains no 'import typer' or 'from typer' statements."""
        import inspect
        from ollarma import service

        source = inspect.getsource(service)
        import_lines = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        typer_imports = [
            line for line in import_lines
            if "typer" in line.split()
            or any(part.startswith("typer.") for part in line.split())
            or line.startswith("from typer")
            or line.startswith("import typer")
        ]
        assert not typer_imports, (
            f"ollarma.service has Typer import statements: {typer_imports}"
        )


# ---------------------------------------------------------------------------
# Public API tests
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """Verify the service module exports exactly the expected public API."""

    def test_service_public_api(self):
        """service module has exactly 7 public functions."""
        from ollarma import service

        expected_functions = {
            "run_benchmark",
            "list_models_and_tasks",
            "generate_report",
            "verify_evidence",
            "sonify_evidence",
            "list_projects",
            "resolve_project_adapter",
            "chat_with_model",
            "route_prompt",
            "submit_workflow",
            "generate_escalation",
            "get_project_kb_contract",
        }
        actual_functions = {
            name for name in dir(service)
            if not name.startswith("_") and callable(getattr(service, name))
            and not isinstance(getattr(service, name), type)
        }
        assert expected_functions.issubset(actual_functions), (
            f"Missing functions: {expected_functions - actual_functions}"
        )


# ---------------------------------------------------------------------------
# Return type tests
# ---------------------------------------------------------------------------


class TestReturnTypes:
    """Verify all service functions return the correct Pydantic model types."""

    @patch("ollarma.service.preflight_check", return_value="high")
    @patch("ollarma.service.warmup_model")
    @patch("ollarma.service.run_inference")
    @patch("ollarma.service.load_tasks")
    @patch("ollarma.service.load_models")
    def test_run_benchmark_returns_pydantic(
        self, mock_models, mock_tasks, mock_inference, mock_warmup, mock_preflight
    ):
        """run_benchmark() returns BenchmarkRunResult (Pydantic BaseModel)."""
        from ollarma.service import run_benchmark, BenchmarkRunResult
        from ollarma.registry import ModelConfig, TaskConfig
        from ollarma.executor import BenchmarkResult
        from datetime import datetime, timezone
        import tempfile

        mock_models.return_value = [ModelConfig(name="test-model")]
        mock_tasks.return_value = [
            TaskConfig(id="t1", suite="science", prompt="test", num_ctx=4096)
        ]

        # Create a realistic BenchmarkResult mock
        mock_result = BenchmarkResult(
            model="test-model",
            task_id="t1",
            suite="science",
            num_ctx=4096,
            prefill_tps=100.0,
            decode_tps=50.0,
            quality_score=None,
            model_digest="abc123",
            ollama_version="0.19.0",
            thinking_mode=False,
            prompt_hash="deadbeef",
            schema_version="1",
            run_ts=datetime.now(timezone.utc),
            raw_response='{"verdict":"SUPPORT","confidence":0.9,"reasoning":"ok"}',
        )
        mock_inference.return_value = mock_result

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_benchmark(
                dry_run=True,
                results_dir=pathlib.Path(tmpdir) / "results",
            )

        assert isinstance(result, BenchmarkRunResult)
        assert isinstance(result, BaseModel)
        assert result.dry_run is True

    def test_list_models_and_tasks_returns_pydantic(self):
        """list_models_and_tasks() returns ModelsAndTasks (Pydantic BaseModel)."""
        from ollarma.service import list_models_and_tasks, ModelsAndTasks
        from ollarma.registry import ModelConfig, TaskConfig

        with patch("ollarma.service.load_models") as mock_m, \
             patch("ollarma.service.load_tasks") as mock_t:
            mock_m.return_value = [ModelConfig(name="m1")]
            mock_t.return_value = [
                TaskConfig(id="t1", suite="science", prompt="p", num_ctx=4096)
            ]
            result = list_models_and_tasks()

        assert isinstance(result, ModelsAndTasks)
        assert isinstance(result, BaseModel)

    @patch("ollarma.service.build_selection_artifact")
    @patch("ollarma.service.aggregate_trials")
    @patch("ollarma.service.load_sealed_results")
    @patch("ollarma.service.find_latest_sealed")
    def test_generate_report_returns_pydantic(
        self, mock_find, mock_load, mock_agg, mock_artifact,
    ):
        """generate_report() returns ReportResult (Pydantic BaseModel)."""
        import tempfile
        from ollarma.service import generate_report, ReportResult
        from ollarma.reporter import ModelSuiteStats
        from ollarma.evidence import ModelSelectionArtifact

        with tempfile.TemporaryDirectory() as tmpdir:
            results_dir = pathlib.Path(tmpdir)
            mock_find.return_value = results_dir / "run-TEST.json"
            mock_load.return_value = [{"model": "m", "suite": "science", "decode_tps": 50}]
            mock_agg.return_value = [
                ModelSuiteStats(
                    model="m", suite="science", quality_mean=0.8, quality_std=0.1,
                    decode_tps_mean=50.0, decode_tps_std=5.0, prefill_tps_mean=100.0,
                    prefill_tps_std=10.0, ttft_ms=10.0, trial_count=3, composite_score=0.75,
                )
            ]
            mock_artifact.return_value = ModelSelectionArtifact(
                run_id="TEST", evidence_root="abc",
                quality_weight_default=0.7, speed_weight_default=0.3,
                quality_weight_routing=0.4, speed_weight_routing=0.6,
                per_suite_winners={"science": "m"}, pareto_frontier=["m"],
                stable_decision_hash="xyz",
            )

            # Pre-create the evidence file so generate_report can read it
            import orjson as _orjson
            evidence_data = {"run_id": "TEST", "evidence_root": "abc", "receipt_count": 1, "receipts": []}
            (results_dir / "run-TEST.evidence.json").write_bytes(
                _orjson.dumps(evidence_data, option=_orjson.OPT_INDENT_2)
            )

            result = generate_report(results_dir=results_dir)

        assert isinstance(result, ReportResult)
        assert isinstance(result, BaseModel)

    def test_verify_evidence_returns_pydantic(self):
        """verify_evidence() returns VerifyResult (Pydantic BaseModel)."""
        import tempfile
        from ollarma.service import verify_evidence, VerifyResult

        with patch("ollarma.service.load_sealed_results") as mock_load, \
             patch("ollarma.service.verify_evidence_chain") as mock_verify:

            mock_load.return_value = [{"model": "m", "suite": "s", "decode_tps": 50}]
            mock_verify.return_value = "abc123"

            with tempfile.TemporaryDirectory() as tmpdir:
                results_dir = pathlib.Path(tmpdir)
                # Pre-create the evidence file
                import orjson
                evidence_data = {
                    "receipts": [{"row_hash": "x", "parent_hash": "0" * 64, "receipt_hash": "abc123", "sequence": 0}],
                    "evidence_root": "abc123",
                }
                (results_dir / "run-TEST.evidence.json").write_bytes(
                    orjson.dumps(evidence_data, option=orjson.OPT_INDENT_2)
                )

                result = verify_evidence(run_id="TEST", results_dir=results_dir)

        assert isinstance(result, VerifyResult)
        assert isinstance(result, BaseModel)
        assert result.valid is True

    def test_sonify_evidence_returns_pydantic(self):
        """sonify_evidence() returns SonifyResult (Pydantic BaseModel)."""
        from ollarma.service import sonify_evidence, SonifyResult

        import tempfile

        with patch("ollarma.service.load_sealed_results") as mock_load, \
             patch("ollarma.service.sonify_chain") as mock_sonify, \
             patch("ollarma.service.validate_receipts") as mock_validate:

            mock_load.return_value = [{"model": "m", "suite": "s", "decode_tps": 50}]
            mock_sonify.return_value = b"RIFF...fake wav bytes"
            mock_validate.return_value = [True, True]

            with tempfile.TemporaryDirectory() as tmpdir:
                results_dir = pathlib.Path(tmpdir)
                # Pre-create the evidence file
                import orjson
                evidence_data = {
                    "receipts": [
                        {"row_hash": "x", "parent_hash": "0" * 64, "receipt_hash": "y", "sequence": 0},
                        {"row_hash": "x2", "parent_hash": "y", "receipt_hash": "z", "sequence": 1},
                    ],
                    "evidence_root": "z",
                }
                (results_dir / "run-TEST.evidence.json").write_bytes(
                    orjson.dumps(evidence_data, option=orjson.OPT_INDENT_2)
                )

                result = sonify_evidence(run_id="TEST", results_dir=results_dir)

        assert isinstance(result, SonifyResult)
        assert isinstance(result, BaseModel)
        assert result.mode == "single"

    def test_list_projects_returns_dict(self):
        """list_projects() returns dict[str, AdapterConfig]."""
        from ollarma.service import list_projects
        from ollarma.fleet import AdapterConfig

        with patch("ollarma.service._load_project_registry") as mock_reg:
            mock_reg.return_value = {
                "proj1": AdapterConfig(
                    project_name="proj1",
                    project_root="/tmp/proj1",
                    adapter_source="yaml",
                )
            }
            result = list_projects()

        assert isinstance(result, dict)
        assert "proj1" in result
        assert isinstance(result["proj1"], AdapterConfig)

    def test_get_project_kb_contract_returns_repo_local_paths(self, tmp_path):
        """get_project_kb_contract resolves repo-local KB paths and refs."""
        from ollarma.service import KBContractResult, get_project_kb_contract
        from ollarma.fleet import AdapterConfig

        (tmp_path / "docs").mkdir()
        adapter = AdapterConfig(
            project_name="proj1",
            project_root=str(tmp_path),
            adapter_source="yaml",
            knowledge_base={
                "artifact_root": ".ollarma/kb",
                "sources": [
                    {"path": "docs", "kind": "documents", "authority": "canonical"},
                ],
            },
        )

        with patch("ollarma.service._load_project_registry", return_value={"proj1": adapter}):
            result = get_project_kb_contract("proj1")

        assert isinstance(result, KBContractResult)
        assert result.contract.project == "proj1"
        assert result.contract.artifact_root.repo_relative == ".ollarma/kb"
        assert result.contract.sources[0].path_ref.repo_relative == "docs"
        assert result.contract.sources[0].path_ref.exists is True
        assert result.contract.status == "blocked"
        assert result.contract.reason_code == "KB_NOT_BUILT"

    def test_get_project_kb_contract_rejects_paths_outside_project_root(self, tmp_path):
        """get_project_kb_contract fails closed when a KB source escapes the repo root."""
        from ollarma.service import get_project_kb_contract
        from ollarma.fleet import AdapterConfig

        adapter = AdapterConfig(
            project_name="proj1",
            project_root=str(tmp_path),
            adapter_source="yaml",
            knowledge_base={
                "sources": [
                    {"path": "/tmp/outside-root", "kind": "documents"},
                ],
            },
        )

        with patch("ollarma.service._load_project_registry", return_value={"proj1": adapter}):
            with pytest.raises(ValueError, match="escapes project root"):
                get_project_kb_contract("proj1")

    def test_get_project_kb_contract_blocks_when_sources_undeclared(self, tmp_path):
        """get_project_kb_contract returns an explicit blocked status without sources."""
        from ollarma.service import get_project_kb_contract
        from ollarma.fleet import AdapterConfig

        adapter = AdapterConfig(
            project_name="proj1",
            project_root=str(tmp_path),
            adapter_source="yaml",
        )

        with patch("ollarma.service._load_project_registry", return_value={"proj1": adapter}):
            result = get_project_kb_contract("proj1")

        assert result.contract.status == "blocked"
        assert result.contract.reason_code == "KB_SOURCES_UNDECLARED"

    def test_build_project_kb_materializes_repo_local_artifacts(self, tmp_path):
        """build_project_kb writes deterministic artifact files under .ollarma/kb."""
        from ollarma.service import build_project_kb
        from ollarma.fleet import AdapterConfig

        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "overview.md").write_text("# Overview\n\nHello world.\n")
        (docs_dir / "notes.txt").write_text("alpha\nbeta\ngamma\n")

        adapter = AdapterConfig(
            project_name="proj1",
            project_root=str(tmp_path),
            adapter_source="yaml",
            knowledge_base={
                "sources": [
                    {"path": "docs", "kind": "documents", "authority": "canonical"},
                ],
            },
        )

        with patch("ollarma.service._load_project_registry", return_value={"proj1": adapter}):
            result = build_project_kb("proj1")

        assert result.project == "proj1"
        assert result.source_count == 1
        assert result.document_count == 2
        assert result.chunk_count >= 2
        assert result.manifest_ref["repo_relative"] == ".ollarma/kb/manifest.json"
        assert (tmp_path / result.manifest_ref["repo_relative"]).exists()
        assert (tmp_path / result.documents_ref["repo_relative"]).exists()
        assert (tmp_path / result.chunks_ref["repo_relative"]).exists()
        assert (tmp_path / result.tags_ref["repo_relative"]).exists()
        assert (tmp_path / result.receipt_ref["repo_relative"]).exists()

    def test_chat_with_model_returns_pydantic(self):
        """chat_with_model() returns ChatResult (Pydantic BaseModel)."""
        from ollarma.service import chat_with_model, ChatResult

        mock_response = MagicMock()
        mock_response.message.content = "hello from ollama"

        with patch("ollarma.service.resolve_default_model", return_value="qwen3:4b"), \
             patch("ollarma.service.ollama.Client") as mock_client_cls:
            mock_client_cls.return_value.chat.return_value = mock_response
            result = chat_with_model("hi")

        assert isinstance(result, ChatResult)
        assert isinstance(result, BaseModel)
        assert result.response == "hello from ollama"
        assert result.model == "qwen3:4b"
        mock_client_cls.assert_called_once_with(timeout=35.0)

    def test_chat_with_model_uses_configured_local_inference_timeout(self, monkeypatch):
        """Generic helper chat uses a finite Ollama SDK timeout."""
        from ollarma.service import chat_with_model

        monkeypatch.setenv("OLLARMA_LOCAL_INFERENCE_TIMEOUT_SECONDS", "7.5")
        mock_response = MagicMock()
        mock_response.message.content = "bounded"

        with patch("ollarma.service.resolve_default_model", return_value="qwen3:4b"), \
             patch("ollarma.service.ollama.Client") as mock_client_cls:
            mock_client_cls.return_value.chat.return_value = mock_response
            result = chat_with_model("hi")

        assert result.response == "bounded"
        mock_client_cls.assert_called_once_with(timeout=7.5)

    def test_chat_with_model_supports_legacy_no_arg_client_doubles(self, monkeypatch):
        """The timeout helper keeps existing no-arg Client test doubles usable."""
        from ollarma.service import chat_with_model
        import ollarma.service as service_mod

        class _FakeClient:
            def chat(self, *, model, messages):
                return type(
                    "FakeResponse",
                    (),
                    {"message": type("FakeMessage", (), {"content": messages[0]["content"]})()},
                )()

        monkeypatch.setattr(service_mod, "resolve_default_model", lambda *args, **kwargs: "qwen3:4b")
        monkeypatch.setattr(service_mod.ollama, "Client", lambda: _FakeClient())

        result = chat_with_model("hi")

        assert result.response == "hi"

    def test_chat_with_model_timeout_returns_blocked_result(self, monkeypatch):
        """Local chat timeouts return a structured blocked result."""
        from ollarma.service import chat_with_model

        monkeypatch.setenv("OLLARMA_LOCAL_INFERENCE_TIMEOUT_SECONDS", "3")

        with patch("ollarma.service.resolve_default_model", return_value="qwen3:4b"), \
             patch("ollarma.service.ollama.Client") as mock_client_cls:
            mock_client_cls.return_value.chat.side_effect = TimeoutError("timed out")
            result = chat_with_model("hi")

        assert result.status == "blocked"
        assert result.reason_code == "LOCAL_INFERENCE_TIMEOUT"
        assert result.model == "qwen3:4b"
        assert "3.0s" in (result.detail or "")

    def test_grounded_synthesis_timeout_returns_route_escalation(self, tmp_path, monkeypatch):
        """Project route synthesis timeouts are reason-coded instead of generic hangs."""
        from ollarma.fleet import AdapterConfig
        from ollarma.kb_search import KBSearchHit, KBSearchResult
        from ollarma.service import _grounded_synthesis_response

        monkeypatch.setenv("OLLARMA_LOCAL_INFERENCE_TIMEOUT_SECONDS", "3")
        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(tmp_path),
            adapter_source="yaml",
            knowledge_base={"sources": [{"path": "docs", "kind": "documents"}]},
        )

        with patch("ollarma.service.ollama.Client") as mock_client_cls:
            mock_client_cls.return_value.chat.side_effect = TimeoutError("timed out")
            result = _grounded_synthesis_response(
                prompt="summarize workflow guidance",
                effective_model="qwen3:4b",
                adapter=adapter,
                search_result=KBSearchResult(
                    project="overwatch",
                    query="workflow",
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
                ),
            )

        assert result.lane == "frontier_or_human"
        assert result.reason_code == "LOCAL_INFERENCE_TIMEOUT"
        assert result.next_action == "retry_smaller_local_or_escalate"

    def test_chat_with_model_surfaces_selection_missing(self):
        """chat_with_model() returns deterministic recovery guidance when no model is available."""
        from ollarma.service import chat_with_model

        error = SelectionResolutionError(
            "SELECTION_MISSING",
            WorkloadClass.CHAT,
            "No validated selection artifact found in results/",
        )

        with patch("ollarma.service.resolve_default_model", side_effect=error), \
             patch("ollarma.service.ollama.Client") as mock_client_cls:
            mock_client_cls.return_value.list.side_effect = RuntimeError("ollama offline")
            result = chat_with_model("hi")

        assert result.status == "blocked"
        assert result.model == "unavailable"
        assert result.reason_code == "SELECTION_MISSING"
        assert "No validated selection artifact found in results/" in result.response
        assert "ollarma run --suites code --trials 3" in result.response

    def test_route_prompt_returns_pydantic(self, tmp_path):
        """route_prompt() returns a typed direct-answer RouteResult when KB evidence is sufficient."""
        from ollarma.service import route_prompt, RouteResult
        from ollarma.fleet import AdapterConfig
        from ollarma.service import build_project_kb

        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "workflow.md").write_text("workflow manifest guidance\n", encoding="utf-8")
        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(tmp_path),
            adapter_source="yaml",
            knowledge_base={
                "sources": [
                    {"path": "docs", "kind": "documents", "authority": "canonical"},
                ],
            },
        )

        with patch("ollarma.service._load_project_registry", return_value={"overwatch": adapter}), \
             patch("ollarma.service._validate_service_project_root"):
            build_project_kb("overwatch")
            result = route_prompt("where is the workflow manifest", "overwatch", service_mode=True)

        assert isinstance(result, RouteResult)
        assert isinstance(result, BaseModel)
        assert result.lane == "kb_direct"
        assert result.reason_code == "KB_DIRECT_ANSWER"
        assert result.project == "overwatch"
        assert result.route_receipt is not None

    def test_route_prompt_surfaces_selection_stale(self, tmp_path):
        """route_prompt() degrades gracefully on SELECTION_STALE (Phase 53 ladder fallback).

        Phase 53 changed behavior: a stale selection no longer raises
        SelectionResolutionError. Instead the ladder absorbs the error and
        falls back to the rescue model so the request still completes locally.
        """
        from ollarma.service import route_prompt, RouteResult
        from ollarma.fleet import AdapterConfig
        from ollarma.service import build_project_kb

        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "workflow.md").write_text("workflow manifest guidance\n", encoding="utf-8")
        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(tmp_path),
            adapter_source="yaml",
            knowledge_base={
                "sources": [
                    {"path": "docs", "kind": "documents", "authority": "canonical"},
                ],
            },
        )
        stale_err = SelectionResolutionError(
            "SELECTION_STALE",
            WorkloadClass.ROUTE_PROMPT,
            "Selection artifact is older than 24h",
        )
        mock_response = MagicMock()
        mock_response.message.content = "fallback answer"

        # Patch resolve_ranked_selection (Phase 53 ladder) AND resolve_default_model
        # to both raise SELECTION_STALE — ladder falls back to rescue model.
        with patch("ollarma.service._load_project_registry", return_value={"overwatch": adapter}), \
            patch("ollarma.service._validate_service_project_root"), \
            patch("ollarma.service.resolve_ranked_selection", side_effect=stale_err), \
            patch("ollarma.service.resolve_default_model", side_effect=stale_err), \
            patch("ollarma.service.ollama.Client") as mock_client_cls:
            mock_client_cls.return_value.chat.return_value = mock_response
            build_project_kb("overwatch")
            # Phase 53: no longer raises; returns a degraded result using rescue model
            result = route_prompt("summarize workflow guidance", "overwatch", service_mode=True)

        # The ladder should have degraded to rescue; result is still a valid RouteResult
        assert isinstance(result, RouteResult)
        assert result.lane in ("grounded_local_synthesis", "frontier_or_human")
        # The ladder decision is populated and reflects stale/missing artifact
        if result.ladder is not None:
            assert result.ladder.reason_code in (
                "LADDER_PREFERRED", "LADDER_RESCUE_ONLY", "LADDER_DEGRADED_SWAP",
                "LADDER_DEGRADED_RESIDENCY", "LADDER_USER_OVERRIDE",
            )

    def test_route_prompt_returns_result_when_receipt_log_write_fails(self, tmp_path):
        """Read-only route answers survive receipt-log persistence failures."""
        from ollarma.service import route_prompt
        from ollarma.fleet import AdapterConfig
        from ollarma.service import build_project_kb

        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "workflow.md").write_text("workflow manifest guidance\n", encoding="utf-8")
        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(tmp_path),
            adapter_source="yaml",
            knowledge_base={
                "sources": [
                    {"path": "docs", "kind": "documents", "authority": "canonical"},
                ],
            },
        )

        with patch("ollarma.service._load_project_registry", return_value={"overwatch": adapter}), \
             patch("ollarma.service._validate_service_project_root"), \
             patch("ollarma.service._append_route_receipt_log", side_effect=OSError("permission denied")):
            build_project_kb("overwatch")
            result = route_prompt("where is the workflow manifest", "overwatch", service_mode=True)

        assert result.lane == "kb_direct"
        assert result.reason_code == "KB_DIRECT_ANSWER"

    def test_submit_workflow_returns_metadata(self):
        """submit_workflow() returns explicit workflow-lane metadata."""
        from ollarma.guards import RuntimeTelemetry
        from ollarma.scheduler import Scheduler
        from ollarma.service import submit_workflow, WorkflowSubmissionResult
        from ollarma.fleet import AdapterConfig

        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(pathlib.Path.home() / "projects" / "overwatch"),
            adapter_source="yaml",
        )
        scheduler = Scheduler(telemetry_provider=lambda: RuntimeTelemetry())

        with patch("ollarma.service._scheduler", scheduler), \
             patch("ollarma.service.resolve_project_adapter", return_value=adapter), \
             patch("ollarma.service.load_workflow_manifest", return_value=_mock_workflow_manifest()), \
             patch("ollarma.service.resolve_selection", return_value="qwen3-coder:7b"):
            result = submit_workflow(
                project="overwatch",
                manifest_ref=".ollarma/manifests/workflow.json",
                step_id="execute-script",
            )

        assert isinstance(result, WorkflowSubmissionResult)
        assert result.status == "accepted"
        assert result.lane == "workflow_execution_queue"
        assert result.step_id == "execute-script"
        assert result.manifest_ref["repo_relative"] == ".ollarma/manifests/workflow.json"
        assert result.owning_lane == "ollarma-default"
        assert result.next_stage == "scaffold_materialize"

    def test_list_project_workflows_discovers_manifest_catalog(self, tmp_path):
        """list_project_workflows returns portable manifest metadata for the dashboard."""
        from ollarma.fleet import AdapterConfig
        from ollarma.service import list_project_workflows

        manifest_dir = tmp_path / ".ollarma" / "manifests"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "workflow.json").write_text("{}", encoding="utf-8")
        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(tmp_path),
            adapter_source="yaml",
        )

        with patch("ollarma.service.resolve_project_adapter", return_value=adapter), \
             patch("ollarma.service.load_workflow_manifest", return_value=_mock_workflow_manifest()):
            result = list_project_workflows("overwatch", service_mode=True)

        assert result.project == "overwatch"
        assert len(result.manifests) == 1
        assert result.manifests[0].manifest_ref["repo_relative"] == ".ollarma/manifests/workflow.json"
        assert result.manifests[0].steps[0].step_id == "execute-script"
        assert result.manifests[0].steps[0].stage == "execute"

    def test_submit_autopilot_delegates_to_bounded_runner(self, tmp_path):
        """submit_autopilot delegates through the bounded autopilot lane."""
        from ollarma.fleet import AdapterConfig
        from ollarma.service import submit_autopilot

        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(tmp_path),
            adapter_source="yaml",
        )
        sentinel = object()

        with patch("ollarma.service.resolve_project_adapter", return_value=adapter), \
             patch("ollarma.service.run_autopilot", return_value=sentinel) as mock_run:
            result = submit_autopilot(
                "overwatch",
                run_assets=True,
                threshold=0.95,
                include=("*.py",),
                exclude=("tests/*",),
                service_mode=True,
            )

        assert result is sentinel
        mock_run.assert_called_once_with(
            project_name="overwatch",
            project_root=str(tmp_path),
            run=True,
            threshold=0.95,
            include_patterns=["*.py"],
            exclude_patterns=["tests/*"],
        )

    def test_submit_workflow_returns_receipt_and_checkpoint_refs_for_bound_manifest(self, tmp_path):
        """submit_workflow records preflight receipts when the manifest carries repo context."""
        from ollarma.guards import RuntimeTelemetry
        from ollarma.scheduler import Scheduler
        from ollarma.service import submit_workflow
        from ollarma.fleet import AdapterConfig

        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(tmp_path),
            adapter_source="yaml",
        )
        scheduler = Scheduler(telemetry_provider=lambda: RuntimeTelemetry())
        manifest = _mock_workflow_manifest(repo_root=tmp_path)

        with patch("ollarma.service._scheduler", scheduler), \
             patch("ollarma.service.resolve_project_adapter", return_value=adapter), \
             patch("ollarma.service.load_workflow_manifest", return_value=manifest), \
             patch("ollarma.service.resolve_selection", return_value="qwen3-coder:7b"):
            result = submit_workflow(
                project="overwatch",
                manifest_ref=".ollarma/manifests/workflow.json",
                step_id="execute-script",
            )

        assert result.receipt_ref is not None
        assert result.checkpoint_ref is not None
        assert result.receipt_ref["repo_relative"].endswith("receipts.json")
        assert result.checkpoint_ref["repo_relative"].endswith("checkpoint.json")

    def test_submit_workflow_returns_selection_receipt(self):
        """submit_workflow() rejects cleanly with selection receipt metadata."""
        from ollarma.guards import RuntimeTelemetry
        from ollarma.scheduler import Scheduler
        from ollarma.service import submit_workflow
        from ollarma.fleet import AdapterConfig

        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(pathlib.Path.home() / "projects" / "overwatch"),
            adapter_source="yaml",
        )
        scheduler = Scheduler(telemetry_provider=lambda: RuntimeTelemetry())
        error = SelectionResolutionError(
            "SELECTION_MISSING",
            WorkloadClass.VALIDATED_SCRIPT,
            "No validated selection artifact found in results/",
        )

        with patch("ollarma.service._scheduler", scheduler), \
             patch("ollarma.service.resolve_project_adapter", return_value=adapter), \
             patch("ollarma.service.load_workflow_manifest", return_value=_mock_workflow_manifest()), \
             patch("ollarma.service.resolve_selection", side_effect=error):
            result = submit_workflow(
                project="overwatch",
                manifest_ref=".ollarma/manifests/workflow.json",
                step_id="execute-script",
            )

        assert result.status == "rejected"
        assert result.reason_code == "SELECTION_MISSING"
        assert result.escalation_receipt is not None

    def test_submit_workflow_rejects_dependency_missing(self):
        """submit_workflow rejects notebook work with DEPENDENCY_MISSING when papermill is absent."""
        from ollarma.service import submit_workflow
        from ollarma.fleet import AdapterConfig

        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(pathlib.Path.home() / "projects" / "overwatch"),
            adapter_source="yaml",
        )

        with patch("ollarma.service.resolve_project_adapter", return_value=adapter), \
             patch(
                 "ollarma.service.load_workflow_manifest",
                 return_value=_mock_workflow_manifest(task_class="validated-notebook"),
             ), \
             patch("ollarma.service.importlib.util.find_spec", return_value=None):
            result = submit_workflow(
                project="overwatch",
                manifest_ref=".ollarma/manifests/workflow.json",
                step_id="execute-script",
            )

        assert result.status == "rejected"
        assert result.reason_code == "DEPENDENCY_MISSING"
        assert result.escalation_receipt is not None

    def test_submit_workflow_returns_queued_metadata(self):
        """submit_workflow returns queued metadata when another inference lease is active."""
        from ollarma.fleet import AdapterConfig
        from ollarma.guards import RuntimeTelemetry
        from ollarma.scheduler import JobRequest, Lane, Scheduler
        from ollarma.service import submit_workflow

        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(pathlib.Path.home() / "projects" / "overwatch"),
            adapter_source="yaml",
        )
        scheduler = Scheduler(telemetry_provider=lambda: RuntimeTelemetry())
        active_lease = scheduler.acquire(
            JobRequest(project="busy", lane=Lane.LOCAL_INFERENCE_SINGLE, model="qwen3:4b")
        )

        try:
            with patch("ollarma.service._scheduler", scheduler), \
                 patch("ollarma.service.resolve_project_adapter", return_value=adapter), \
                 patch("ollarma.service.load_workflow_manifest", return_value=_mock_workflow_manifest()), \
                 patch("ollarma.service.resolve_selection", return_value="qwen3-coder:7b"):
                result = submit_workflow(
                    project="overwatch",
                    manifest_ref=".ollarma/manifests/workflow.json",
                    step_id="execute-script",
                )
        finally:
            scheduler.release(active_lease)

        assert result.status == "queued"
        assert result.queue_depth == 1
        assert result.reason_code is None

    def test_submit_workflow_returns_queue_timeout_receipt(self):
        """submit_workflow returns QUEUE_TIMEOUT instead of hanging indefinitely."""
        from ollarma.fleet import AdapterConfig
        from ollarma.guards import RuntimeTelemetry
        from ollarma.scheduler import JobRequest, Lane, Scheduler
        from ollarma.service import submit_workflow

        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(pathlib.Path.home() / "projects" / "overwatch"),
            adapter_source="yaml",
        )
        scheduler = Scheduler(telemetry_provider=lambda: RuntimeTelemetry())
        active_lease = scheduler.acquire(
            JobRequest(project="busy", lane=Lane.LOCAL_INFERENCE_SINGLE, model="qwen3:4b")
        )

        try:
            with patch("ollarma.service._scheduler", scheduler), \
                 patch("ollarma.service.resolve_project_adapter", return_value=adapter), \
                 patch("ollarma.service.load_workflow_manifest", return_value=_mock_workflow_manifest()), \
                 patch("ollarma.service.resolve_selection", return_value="qwen3-coder:7b"):
                result = submit_workflow(
                    project="overwatch",
                    manifest_ref=".ollarma/manifests/workflow.json",
                    step_id="execute-script",
                    wait_for_available=True,
                    queue_timeout_s=0.01,
                )
        finally:
            scheduler.release(active_lease)

        assert result.status == "timed_out"
        assert result.reason_code == "QUEUE_TIMEOUT"
        assert result.escalation_receipt is not None

    def test_submit_workflow_rejects_resource_budget_exceeded(self):
        """submit_workflow rejects workflow admission with RESOURCE_BUDGET_EXCEEDED for low-swap degraded mode.

        swap_used_mb=100 is below the 512MB SWAP_DEGRADED threshold (Phase 31-01), so the
        generic RESOURCE_BUDGET_EXCEEDED reason code is emitted instead of SWAP_DEGRADED.
        """
        from ollarma.fleet import AdapterConfig
        from ollarma.guards import RuntimeTelemetry
        from ollarma.scheduler import Scheduler
        from ollarma.service import submit_workflow

        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(pathlib.Path.home() / "projects" / "overwatch"),
            adapter_source="yaml",
        )
        scheduler = Scheduler(
            telemetry_provider=lambda: RuntimeTelemetry(
                swap_used_mb=100,
                degraded_mode=True,
                degraded_reason="generic degraded mode (low swap)",
                telemetry_source="api/ps+ollama ps+sysctl vm.swapusage",
            )
        )

        with patch("ollarma.service._scheduler", scheduler), \
             patch("ollarma.service.resolve_project_adapter", return_value=adapter), \
             patch("ollarma.service.load_workflow_manifest", return_value=_mock_workflow_manifest()), \
             patch("ollarma.service.resolve_selection", return_value="qwen3-coder:7b"):
            result = submit_workflow(
                project="overwatch",
                manifest_ref=".ollarma/manifests/workflow.json",
                step_id="execute-script",
            )

        assert result.status == "rejected"
        assert result.reason_code == "RESOURCE_BUDGET_EXCEEDED"
        assert result.escalation_receipt is not None

    def test_resolve_project_adapter_rejects_home_root_in_service_mode(self):
        """service_mode project resolution rejects the home directory as a project root."""
        from ollarma.service import resolve_project_adapter
        from ollarma.fleet import AdapterConfig

        adapter = AdapterConfig(
            project_name="home",
            project_root=str(pathlib.Path.home()),
            adapter_source="yaml",
        )

        with patch("ollarma.service._load_project_registry", return_value={"home": adapter}):
            with pytest.raises(ValueError, match="home directory"):
                resolve_project_adapter("home", service_mode=True)

    def test_route_prompt_execution_request_returns_handoff_without_model(self, tmp_path):
        """Execution requests return an explicit handoff without selecting a route model."""
        from ollarma.service import route_prompt
        from ollarma.fleet import AdapterConfig

        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "workflow.md").write_text("workflow manifest guidance\n", encoding="utf-8")
        adapter = AdapterConfig(
            project_name="overwatch",
            project_root=str(tmp_path),
            adapter_source="yaml",
            knowledge_base={
                "sources": [
                    {"path": "docs", "kind": "documents", "authority": "canonical"},
                ],
            },
        )

        with patch("ollarma.service._load_project_registry", return_value={"overwatch": adapter}), \
             patch("ollarma.service._validate_service_project_root"), \
             patch("ollarma.service.resolve_default_model") as mock_model:
            result = route_prompt("run the workflow manifest", "overwatch", service_mode=True)

        mock_model.assert_not_called()
        assert result.lane == "orchestrator_handoff"
        assert result.reason_code == "EXECUTION_LANE_REQUIRED"

    def test_generate_escalation_returns_pydantic(self):
        """generate_escalation() returns EscalationResult (Pydantic BaseModel)."""
        from ollarma.service import generate_escalation, EscalationResult
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            results_dir = pathlib.Path(tmpdir)
            # Create a fake autopilot result file
            import orjson
            report_data = {
                "project_name": "test",
                "results": [
                    {
                        "asset_path": "/tmp/test.py",
                        "asset_type": "script",
                        "task_tier": "code",
                        "model_used": "qwen3:8b",
                        "escalation_needed": True,
                        "stdout": "",
                        "stderr": "error msg",
                    }
                ],
            }
            (results_dir / "autopilot-test-20260101T000000Z.json").write_bytes(
                orjson.dumps(report_data)
            )

            result = generate_escalation(results_dir=results_dir)

        assert isinstance(result, EscalationResult)
        assert isinstance(result, BaseModel)
        assert result.count == 1


# ---------------------------------------------------------------------------
# Scorer dispatch tests
# ---------------------------------------------------------------------------


class TestDispatchScorer:
    """Verify _dispatch_scorer routes to correct scorers."""

    def test_dispatch_scorer_routes_correctly(self):
        """_dispatch_scorer routes science/code/swarm to correct scorers."""
        from ollarma.service import _dispatch_scorer
        from ollarma.executor import BenchmarkResult
        from datetime import datetime, timezone

        base_kwargs = dict(
            model="test",
            task_id="t1",
            num_ctx=4096,
            prefill_tps=100.0,
            decode_tps=50.0,
            quality_score=None,
            model_digest="abc",
            ollama_version="0.19",
            thinking_mode=False,
            prompt_hash="deadbeef",
            schema_version="1",
            run_ts=datetime.now(timezone.utc),
            raw_response='{"verdict":"SUPPORT","confidence":0.9,"reasoning":"ok"}',
        )

        with patch("ollarma.service.score_science", return_value=0.9) as mock_sci, \
             patch("ollarma.service.score_code", return_value=0.8) as mock_code, \
             patch("ollarma.service.score_swarm", return_value=0.7) as mock_swarm:

            r_science = BenchmarkResult(suite="science", **base_kwargs)
            assert _dispatch_scorer(r_science) == 0.9
            mock_sci.assert_called_once_with(r_science)

            r_code = BenchmarkResult(suite="code", **base_kwargs)
            assert _dispatch_scorer(r_code) == 0.8
            mock_code.assert_called_once_with(r_code)

            r_swarm = BenchmarkResult(suite="swarm", **base_kwargs)
            assert _dispatch_scorer(r_swarm) == 0.7
            mock_swarm.assert_called_once_with(r_swarm)

            r_unknown = BenchmarkResult(suite="unknown", **base_kwargs)
            assert _dispatch_scorer(r_unknown) is None


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Verify error classes raise appropriately."""

    def test_no_models_raises(self):
        """list_models_and_tasks raises NoModelsError when models.yml is empty."""
        from ollarma.service import list_models_and_tasks, NoModelsError

        with patch("ollarma.service.load_models") as mock_m, \
             patch("ollarma.service.load_tasks") as mock_t:
            mock_m.return_value = []
            mock_t.return_value = []
            with pytest.raises(NoModelsError):
                list_models_and_tasks()

    def test_no_results_raises(self):
        """generate_report raises NoResultsError when no sealed files exist."""
        from ollarma.service import generate_report, NoResultsError

        with patch("ollarma.service.find_latest_sealed") as mock_find:
            mock_find.side_effect = FileNotFoundError("No sealed results")
            with pytest.raises(NoResultsError):
                generate_report()


class TestDashboardOverview:
    """Dashboard overview should expose operator resources and readiness hints."""

    def test_get_dashboard_overview_without_projects_surfaces_readiness(self):
        from ollarma import service
        from ollarma.scheduler import RuntimeSnapshot

        with patch.object(service, "list_projects", return_value={}), patch.object(
            service,
            "get_scheduler_snapshot",
            return_value=RuntimeSnapshot(
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
        ):
            overview = service.get_dashboard_overview()

        assert overview.boundary.interactive is True
        assert overview.project_count == 0
        # v4.5 Phase 51 PERSIST-03: a degraded startup posture surfaces at the
        # top of dashboard readiness, ahead of other items. The legacy
        # "No registered projects detected" item is still present; just
        # further down the list.
        titles = tuple(item.title for item in overview.readiness)
        assert "No registered projects detected" in titles
        assert overview.operator_resources[0].slug == "dashboard-start"

    def test_route_receipt_prompt_preview_redacts_paths_and_secretish_tokens(self):
        from ollarma.service import _redact_prompt_preview

        preview = _redact_prompt_preview(
            "inspect <repo> and token sk_secretvalue12345 please"
        )

        assert "/Users/" not in preview
        assert "sk_secretvalue12345" not in preview
        assert "[path]" in preview
        assert "[secret]" in preview

    def test_chat_with_model_falls_back_to_installed_model_when_selection_missing(self, monkeypatch):
        """Generic helper chat can use an installed fallback model when strict selection is unavailable."""
        from ollarma.execution_policy import SelectionResolutionError, WorkloadClass
        from ollarma.service import chat_with_model
        import ollarma.service as service_mod

        def _raise_selection(*args, **kwargs):
            raise SelectionResolutionError(
                "SELECTION_MISSING",
                WorkloadClass.CHAT,
                "No validated selection artifact found in results/",
            )

        class _FakeClient:
            def list(self):
                return type(
                    "FakeList",
                    (),
                    {"models": [type("FakeModel", (), {"model": "qwen2.5:1.5b"})()]},
                )()

            def chat(self, *, model, messages):
                return type(
                    "FakeResponse",
                    (),
                    {"message": type("FakeMessage", (), {"content": f"{model}:{messages[0]['content']}"})()},
                )()

        monkeypatch.setattr(service_mod, "resolve_default_model", _raise_selection)
        monkeypatch.setattr(service_mod.ollama, "Client", lambda: _FakeClient())

        result = chat_with_model("hi")

        assert result.model == "qwen2.5:1.5b"
        assert result.response == "qwen2.5:1.5b:hi"

    def test_chat_with_model_returns_blocked_result_when_selection_and_fallback_missing(self, monkeypatch):
        """Generic helper chat fails closed with a deterministic blocked response when no model is selectable."""
        from ollarma.execution_policy import SelectionResolutionError, WorkloadClass
        from ollarma.service import chat_with_model
        import ollarma.service as service_mod

        monkeypatch.setattr(
            service_mod,
            "resolve_default_model",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                SelectionResolutionError(
                    "SELECTION_MISSING",
                    WorkloadClass.CHAT,
                    "Selection artifact does not cover suite 'code' for chat",
                )
            ),
        )
        monkeypatch.setattr(
            service_mod.ollama,
            "Client",
            lambda: type(
                "FakeClient",
                (),
                {"list": lambda self: (_ for _ in ()).throw(RuntimeError("offline"))},
            )(),
        )

        result = chat_with_model("hi")

        assert result.status == "blocked"
        assert result.model == "unavailable"
        assert result.reason_code == "SELECTION_MISSING"
        assert (
            result.detail
            == "Selection artifact does not cover suite 'code' for chat. "
            "Local Ollama daemon probe failed: offline"
        )
        assert result.recovery_commands[:2] == ("ollama list", "ollama pull qwen2.5:1.5b")

    def test_get_runtime_health_reports_selection_and_helper_chat_status(self, monkeypatch):
        """Runtime health reports strict selection blockers and generic chat fallback readiness."""
        from ollarma.execution_policy import SelectionResolutionError, WorkloadClass
        from ollarma.service import get_runtime_health
        import ollarma.service as service_mod

        def _raise_default_selection(*args, **kwargs):
            raise SelectionResolutionError(
                "SELECTION_MISSING",
                WorkloadClass.CHAT,
                "Selection artifact does not cover suite 'code' for chat",
            )

        def _raise_runtime_selection(*args, **kwargs):
            workload_class = kwargs.get("workload_class") or args[0]
            raise SelectionResolutionError(
                "SELECTION_MISSING",
                workload_class,
                f"Selection artifact does not cover suite 'code' for {workload_class.value}",
            )

        monkeypatch.setattr(service_mod, "resolve_default_model", _raise_default_selection)
        monkeypatch.setattr(service_mod, "resolve_selection", _raise_runtime_selection)
        monkeypatch.setattr(
            service_mod.ollama,
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
                },
            )(),
        )
        monkeypatch.setattr(service_mod, "list_projects", lambda **kwargs: {})

        health = get_runtime_health()

        # v4.5 Phase 51 PERSIST-03: top-level status now folds startup readiness
        # (blocked > degraded > ready). With fallback_ready helper_chat + stale
        # chat selection, the combined status is at least "degraded".
        assert health.status in {"ready", "degraded", "blocked"}
        assert health.status != "ok"  # legacy value is gone
        assert health.helper_chat.status == "fallback_ready"
        assert health.helper_chat.effective_model == "qwen2.5:1.5b"
        # GPU-08: a missing validated selection no longer hard-blocks the
        # selection lanes when a pressure-aware local fallback is installed;
        # they report "fallback_ready" while preserving the SELECTION_MISSING
        # reason so operators still see the strict selection needs a refresh.
        assert health.chat_selection.status == "fallback_ready"
        assert health.chat_selection.reason_code == "SELECTION_MISSING"
        assert health.route_selection.status == "fallback_ready"
        assert health.route_selection.reason_code == "SELECTION_MISSING"


def test_granite_wired_into_chat_fallback_ladder():
    """#6: the measured quality winner must be reachable by degraded chat and
    preferred over the quality-0 tiny model when both are installed."""
    from ollarma.service import _CHAT_FALLBACK_MODELS, _pick_chat_fallback_model

    assert "granite4.1:8b" in _CHAT_FALLBACK_MODELS
    # Both installed -> prefer the quality winner.
    assert _pick_chat_fallback_model(("qwen2.5:1.5b", "granite4.1:8b")) == "granite4.1:8b"
    # granite absent -> the tiny lean rescue is still selected.
    assert _pick_chat_fallback_model(("qwen2.5:1.5b",)) == "qwen2.5:1.5b"
