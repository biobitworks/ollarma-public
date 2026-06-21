"""Tests for fleet CLI commands in bench.py.

Tests projects command, --project flag on chat/execute, and fleet integration.
Uses typer.testing.CliRunner for CLI invocation without live Ollama.
"""
from __future__ import annotations

import pathlib
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from ollarma.cli import app
from ollarma.fleet import AdapterConfig
from ollarma.agent import AgentResult


runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    name: str = "test-project",
    root: str = "/tmp/test-project",
    tools: list[str] | None = None,
    antibodies: list[str] | None = None,
) -> AdapterConfig:
    """Build a minimal AdapterConfig for testing."""
    return AdapterConfig(
        project_name=name,
        project_root=root,
        project_type="python",
        tools=tools or ["gh", "git_cmd"],
        antibodies=antibodies or ["prompt_injection"],
        adapter_source="test",
    )


def _make_agent_result(response: str = "done") -> AgentResult:
    """Build a minimal AgentResult for mocking."""
    return AgentResult(
        final_response=response,
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": response}],
        tool_calls_count=0,
        model="test-model",
    )


# ---------------------------------------------------------------------------
# projects command
# ---------------------------------------------------------------------------


class TestProjectsCommand:
    """Tests for the 'projects' CLI command."""

    def test_command_registered(self) -> None:
        """'projects' is a registered CLI command."""
        result = runner.invoke(app, ["--help"])
        assert "projects" in result.output

    @patch("ollarma.service._load_project_registry")
    def test_empty_registry_exits_1(self, mock_load) -> None:
        """projects with empty registry prints warning and exits 1."""
        mock_load.return_value = {}
        result = runner.invoke(app, ["projects"])
        assert result.exit_code == 1
        assert "no adapters" in result.output.lower()

    @patch("ollarma.service._load_project_registry")
    def test_with_adapters_shows_project_count(self, mock_load) -> None:
        """projects with adapters shows project count and exits 0."""
        mock_load.return_value = {
            "cellico-bio": _make_adapter("cellico-bio", "/tmp/cellico"),
            "overwatch": _make_adapter("overwatch", "/tmp/overwatch"),
        }
        result = runner.invoke(app, ["projects"])
        assert result.exit_code == 0
        # Rich table may truncate in narrow CliRunner terminal,
        # but the summary line always renders fully
        assert "2 projects registered" in result.output
        assert "Fleet Projects" in result.output

    @patch("ollarma.service._load_project_registry")
    def test_shows_adapter_source(self, mock_load) -> None:
        """projects output includes adapter source column."""
        mock_load.return_value = {
            "test-proj": _make_adapter("test-proj", "/tmp/proj"),
        }
        result = runner.invoke(app, ["projects"])
        assert result.exit_code == 0
        assert "test" in result.output  # adapter_source="test"

    @patch("ollarma.service._load_project_registry")
    def test_shows_project_table(self, mock_load) -> None:
        """projects output renders table with Fleet Projects title."""
        mock_load.return_value = {
            "proj-a": _make_adapter(
                "proj-a", "/tmp/a",
                tools=["gh", "git_cmd", "db_query"],
                antibodies=["citation", "logic"],
            ),
        }
        result = runner.invoke(app, ["projects"])
        assert result.exit_code == 0
        assert "Fleet Projects" in result.output
        assert "1 projects registered" in result.output


class TestKbBuildCommand:
    """Tests for the planned kb-build CLI surface."""

    def test_command_registered(self) -> None:
        """kb-build is a registered CLI command."""
        result = runner.invoke(app, ["--help"])
        assert "kb-build" in result.output

    @patch("ollarma.service.build_project_kb")
    def test_kb_build_success_delegates_to_service(self, mock_build) -> None:
        """kb-build calls the service layer and exits cleanly on success."""
        mock_build.return_value = MagicMock(
            project="kb-demo",
            artifact_root=".ollarma/kb",
            status="built",
            receipt_count=1,
            model_dump=MagicMock(return_value={
                "project": "kb-demo",
                "artifact_root": ".ollarma/kb",
                "status": "built",
            }),
        )

        result = runner.invoke(
            app,
            [
                "kb-build",
                "--project", "kb-demo",
                "--adapters-dir", "/tmp/adapters",
            ],
        )

        assert result.exit_code == 0
        assert mock_build.called
        call_args = mock_build.call_args
        if call_args.kwargs:
            assert call_args.kwargs.get("project") == "kb-demo"
            assert call_args.kwargs.get("adapters_dir") == "/tmp/adapters"
        else:
            assert len(call_args.args) >= 1
            assert call_args.args[0] == "kb-demo"
            assert len(call_args.args) >= 2
            assert call_args.args[1] == "/tmp/adapters"

    @patch("ollarma.service.build_project_kb")
    def test_kb_build_failure_exits_1(self, mock_build) -> None:
        """kb-build reports service-layer failures and exits non-zero."""
        mock_build.side_effect = ValueError("KB build failed: missing KB sources")

        result = runner.invoke(
            app,
            [
                "kb-build",
                "--project", "kb-demo",
            ],
        )

        assert result.exit_code == 1
        assert "missing kb sources" in result.output.lower()


class TestKbReadCommands:
    """Tests for the read-only KB CLI surfaces."""

    def test_status_and_search_commands_registered(self) -> None:
        """kb-status and kb-search are registered CLI commands."""
        result = runner.invoke(app, ["--help"])
        assert "kb-status" in result.output
        assert "kb-search" in result.output

    @patch("ollarma.service.get_project_kb_status")
    def test_kb_status_success(self, mock_status) -> None:
        """kb-status renders the typed status payload."""
        from ollarma.kb_search import KBStatus

        mock_status.return_value = KBStatus(
            project="kb-demo",
            status="ready",
            freshness_hours=24,
            stale_behavior="escalate",
            built_at="2026-04-10T12:00:00Z",
            artifact_root=".ollarma/kb",
            search_db_path=".ollarma/kb/search.sqlite",
            document_count=4,
            chunk_count=9,
        )

        result = runner.invoke(app, ["kb-status", "--project", "kb-demo"])

        assert result.exit_code == 0
        assert "kb-demo" in result.output
        assert "ready" in result.output.lower()
        assert "search.sqlite" in result.output

    @patch("ollarma.service.get_project_kb_status")
    def test_kb_status_failure_exits_1(self, mock_status) -> None:
        """kb-status reports read failures and exits non-zero."""
        mock_status.side_effect = ValueError("KB status failed")

        result = runner.invoke(app, ["kb-status", "--project", "kb-demo"])

        assert result.exit_code == 1
        assert "kb status failed" in result.output.lower()

    @patch("ollarma.service.search_project_kb")
    def test_kb_search_success(self, mock_search) -> None:
        """kb-search renders bounded hits from the shared service layer."""
        from ollarma.kb_search import KBSearchHit, KBSearchResult

        mock_search.return_value = KBSearchResult(
            project="kb-demo",
            query="adapter schema",
            status="ready",
            hit_count=1,
            hits=(
                KBSearchHit(
                    chunk_id="chunk:1",
                    document_id="doc:1",
                    path="docs/guide.md",
                    chunk_index=0,
                    authority="reference",
                    source_kind="documents",
                    score=1.0,
                    text="Adapter schema guidance for deterministic builds.",
                    tags=("role:docs",),
                ),
            ),
        )

        result = runner.invoke(
            app,
            ["kb-search", "--project", "kb-demo", "--query", "adapter schema"],
        )

        assert result.exit_code == 0
        assert "docs/guide.md#0" in result.output
        assert "hits=1" in result.output

    @patch("ollarma.service.search_project_kb")
    def test_kb_search_blocked_exits_1(self, mock_search) -> None:
        """kb-search exits non-zero when the KB is stale-blocked or unavailable."""
        from ollarma.kb_search import KBSearchResult

        mock_search.return_value = KBSearchResult(
            project="kb-demo",
            query="adapter schema",
            status="blocked",
            reason_code="KB_NOT_BUILT",
            hit_count=0,
            hits=(),
        )

        result = runner.invoke(
            app,
            ["kb-search", "--project", "kb-demo", "--query", "adapter schema"],
        )

        assert result.exit_code == 1
        assert "kb search blocked" in result.output.lower()
        assert "KB_NOT_BUILT" in result.output


class TestWorkflowCommand:
    """Tests for the explicit workflow CLI surface."""

    def test_command_registered(self) -> None:
        """workflow is a registered CLI command."""
        result = runner.invoke(app, ["--help"])
        assert "workflow" in result.output

    @patch("ollarma.service.submit_workflow")
    def test_workflow_delegates_to_service(self, mock_submit) -> None:
        """workflow command delegates to service.submit_workflow."""
        from ollarma.service import WorkflowSubmissionResult
        from ollarma.scheduler import RuntimeSnapshot

        mock_submit.return_value = WorkflowSubmissionResult(
            project="overwatch",
            manifest_ref={"repo_relative": ".ollarma/manifests/workflow.json"},
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

        result = runner.invoke(
            app,
            [
                "workflow",
                "--project", "overwatch",
                "--manifest-ref", ".ollarma/manifests/workflow.json",
                "--step-id", "execute-script",
            ],
        )
        assert result.exit_code == 0
        assert mock_submit.called
        assert "Workflow accepted" in result.output

    @patch("ollarma.service.submit_workflow")
    def test_workflow_queued_exits_0(self, mock_submit) -> None:
        """Queued workflow admission is reported as success, not rejection."""
        from ollarma.service import WorkflowSubmissionResult
        from ollarma.scheduler import RuntimeSnapshot

        mock_submit.return_value = WorkflowSubmissionResult(
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
        )

        result = runner.invoke(
            app,
            [
                "workflow",
                "--project", "overwatch",
                "--manifest-ref", ".ollarma/manifests/workflow.json",
                "--step-id", "execute-script",
            ],
        )
        assert result.exit_code == 0
        assert "Workflow queued" in result.output

    @patch("ollarma.service.submit_workflow")
    def test_workflow_dependency_missing_exits_1(self, mock_submit) -> None:
        """Rejected workflow admission surfaces DEPENDENCY_MISSING in CLI output."""
        from ollarma.service import WorkflowSubmissionResult
        from ollarma.scheduler import RuntimeSnapshot

        mock_submit.return_value = WorkflowSubmissionResult(
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
        )

        result = runner.invoke(
            app,
            [
                "workflow",
                "--project", "overwatch",
                "--manifest-ref", ".ollarma/manifests/workflow.json",
                "--step-id", "execute-script",
            ],
        )
        assert result.exit_code == 1
        assert "DEPENDENCY_MISSING" in result.output


# ---------------------------------------------------------------------------
# chat --project flag
# ---------------------------------------------------------------------------


class TestChatProject:
    """Tests for --project flag on chat command."""

    def test_has_project_option(self) -> None:
        """chat command has --project option in help."""
        result = runner.invoke(app, ["chat", "--help"])
        assert "--project" in result.output

    @patch("ollarma.cli.resolve_default_model", return_value="qwen3-coder:7b")
    @patch("ollarma.cli.fleet_agent_loop")
    @patch("ollarma.service.resolve_project_adapter")
    def test_with_project_uses_fleet_agent_loop(
        self, mock_resolve_adapter, mock_fleet_loop, _mock_resolve_model
    ) -> None:
        """chat with --project calls fleet_agent_loop instead of agent_loop."""
        adapter = _make_adapter("cellico-bio", "/tmp/cellico")
        mock_resolve_adapter.return_value = adapter
        mock_fleet_loop.return_value = _make_agent_result("fleet response")

        # Simulate user typing "hello" then "exit"
        result = runner.invoke(app, ["chat", "--project", "cellico-bio"], input="hello\nexit\n")
        assert mock_fleet_loop.called
        # Verify fleet_agent_loop was called with the adapter
        call_kwargs = mock_fleet_loop.call_args
        assert call_kwargs.kwargs.get("adapter") is adapter or \
               (len(call_kwargs.args) >= 3 and call_kwargs.args[2] is adapter)

    @patch("ollarma.cli.resolve_default_model", return_value="qwen3-coder:7b")
    @patch("ollarma.cli.agent_loop")
    def test_without_project_uses_agent_loop(self, mock_agent_loop, _mock_resolve_model) -> None:
        """chat without --project calls original agent_loop."""
        mock_agent_loop.return_value = _make_agent_result("standard response")

        result = runner.invoke(app, ["chat"], input="hello\nexit\n")
        assert mock_agent_loop.called

    @patch("ollarma.cli.resolve_default_model", return_value="qwen3-coder:7b")
    @patch("ollarma.service.resolve_project_adapter")
    def test_unknown_project_exits_1(self, mock_resolve_adapter, _mock_resolve_model) -> None:
        """chat with unknown --project prints error and exits 1."""
        mock_resolve_adapter.side_effect = ValueError("not found")

        result = runner.invoke(app, ["chat", "--project", "nonexistent"], input="exit\n")
        assert result.exit_code == 1
        assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# execute --project flag
# ---------------------------------------------------------------------------


class TestExecuteProject:
    """Tests for --project flag on execute command."""

    def test_has_project_option(self) -> None:
        """execute command has --project option in help."""
        result = runner.invoke(app, ["execute", "--help"])
        assert "--project" in result.output

    @patch("ollarma.cli.resolve_default_model", return_value="qwen3-coder:7b")
    @patch("ollarma.cli.fleet_agent_loop")
    @patch("ollarma.service.resolve_project_adapter")
    def test_with_project_uses_fleet_agent_loop(
        self, mock_resolve_adapter, mock_fleet_loop, _mock_resolve_model, tmp_path
    ) -> None:
        """execute with --project calls fleet_agent_loop."""
        adapter = _make_adapter("overwatch", "/tmp/overwatch")
        mock_resolve_adapter.return_value = adapter
        mock_fleet_loop.return_value = _make_agent_result("fleet execute done")

        plan_file = tmp_path / "test-plan.md"
        plan_file.write_text(
            "---\nphase: test\nplan: 1\ntype: execute\nwave: 1\n---\n"
            "<tasks>\n<task type=\"auto\">\n"
            "  <name>Task 1: test</name>\n"
            "  <files>test.py</files>\n"
            "  <action>Do nothing</action>\n"
            "</task>\n</tasks>\n"
        )

        result = runner.invoke(
            app, ["execute", str(plan_file), "--project", "overwatch"]
        )
        assert mock_fleet_loop.called
        call_kwargs = mock_fleet_loop.call_args
        assert call_kwargs.kwargs.get("adapter") is adapter or \
               (len(call_kwargs.args) >= 3 and call_kwargs.args[2] is adapter)

    @patch("ollarma.cli.resolve_default_model", return_value="qwen3-coder:7b")
    @patch("ollarma.cli.agent_loop")
    def test_without_project_uses_agent_loop(self, mock_agent_loop, _mock_resolve_model, tmp_path) -> None:
        """execute without --project calls original agent_loop."""
        mock_agent_loop.return_value = _make_agent_result("standard execute done")

        plan_file = tmp_path / "test-plan.md"
        plan_file.write_text(
            "---\nphase: test\nplan: 1\ntype: execute\nwave: 1\n---\n"
            "<tasks>\n<task type=\"auto\">\n"
            "  <name>Task 1: test</name>\n"
            "  <files>test.py</files>\n"
            "  <action>Do nothing</action>\n"
            "</task>\n</tasks>\n"
        )

        result = runner.invoke(app, ["execute", str(plan_file)])
        assert mock_agent_loop.called


# ---------------------------------------------------------------------------
# pyproject.toml fleet deps
# ---------------------------------------------------------------------------


class TestPyprojectFleetDeps:
    """Tests for fleet optional dependencies in pyproject.toml."""

    def test_fleet_section_exists(self) -> None:
        """pyproject.toml has fleet optional dependency group."""
        import tomllib

        pyproject_path = pathlib.Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        optional_deps = data.get("project", {}).get("optional-dependencies", {})
        assert "fleet" in optional_deps

    def test_fleet_contains_antigence(self) -> None:
        """fleet deps include antigence."""
        import tomllib

        pyproject_path = pathlib.Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        fleet_deps = data["project"]["optional-dependencies"]["fleet"]
        assert any("antigence" in dep for dep in fleet_deps)

    def test_fleet_contains_python_arango(self) -> None:
        """fleet deps include python-arango."""
        import tomllib

        pyproject_path = pathlib.Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        fleet_deps = data["project"]["optional-dependencies"]["fleet"]
        assert any("python-arango" in dep for dep in fleet_deps)
