"""Tests for ollarma.mcp_server -- MCP server exposing service tools.

Tests cover requirements MCP-01 through MCP-05 plus Phase 16 MCP routing remediation:
  MCP-01: FastMCP instance created with json_response=True; __main__ entry point
  MCP-02: expected tools registered with correct annotations
  MCP-03: No dangerous tools (run_bash, edit_file, etc.) in tool registry
  MCP-04: No Rich/Typer imports in source; logging configured to stderr
  MCP-05: Inference semaphore exists; async function signatures
"""
from __future__ import annotations

import asyncio
import inspect

import pytest


# ---------------------------------------------------------------------------
# MCP-01: FastMCP instance + stdio transport
# ---------------------------------------------------------------------------


class TestMCPInstance:
    """MCP-01: FastMCP server instance creation and transport entry point."""

    def test_mcp_instance_created(self):
        """FastMCP('ollarma') instance exists with json_response=True."""
        from ollarma.mcp_server import mcp

        assert mcp.name == "ollarma"
        assert mcp.settings.json_response is True

    def test_dunder_main_entry(self):
        """mcp_server.py has `if __name__ == '__main__'` block calling mcp.run(transport='stdio')."""
        from ollarma import mcp_server

        source = inspect.getsource(mcp_server)
        assert 'if __name__ == "__main__"' in source or "if __name__ == '__main__'" in source
        assert 'mcp.run(transport="stdio")' in source or "mcp.run(transport='stdio')" in source


# ---------------------------------------------------------------------------
# MCP-02: Tool registration + annotations
# ---------------------------------------------------------------------------


EXPECTED_TOOLS = sorted([
    "list_models",
    "run_benchmark",
    "get_report",
    "verify_evidence",
    "list_projects",
    "chat_with_model",
    "embed_status",
    "embed_text",
    "route_prompt",
    "kb_status",
    "kb_search",
    "submit_workflow",
    "read_sibling_file",
    "find_on_mac",
    "reindex_macfind",
    "get_pipeline_status",
    "pipeline_warmup",
    "pipeline_pin",
    "pipeline_evict",
    "pipeline_drain_swap",
    "run_agent",
    "scribe_progress",
    "read_resume",
    # v4.3 Phase 43 recovery tools
    "scan_recovery_state",
    "read_recovery_report",
])


class TestToolRegistration:
    """MCP-02: expected tools registered with correct names and annotations."""

    def test_all_tools_registered(self):
        """mcp._tool_manager has the expected tool names."""
        from ollarma.mcp_server import mcp

        tools = list(mcp._tool_manager._tools.keys())
        assert sorted(tools) == EXPECTED_TOOLS, (
            f"Expected {EXPECTED_TOOLS}, got {sorted(tools)}"
        )

    def test_tool_annotations_read_only(self):
        """Tool annotations match the actual side-effect profile."""
        from ollarma.mcp_server import mcp

        expected_read_only = {
            "list_models",
            "list_projects",
            "chat_with_model",
            "embed_status",
            "embed_text",
            "route_prompt",
            "kb_status",
            "kb_search",
            "read_sibling_file",
            "find_on_mac",
            "get_pipeline_status",
            "pipeline_warmup",
            "pipeline_pin",
            "pipeline_evict",
            "read_resume",
            "read_recovery_report",  # v4.3
        }
        expected_writing = {"run_benchmark", "get_report", "verify_evidence", "submit_workflow", "reindex_macfind", "pipeline_drain_swap", "run_agent", "scribe_progress", "scan_recovery_state"}  # v4.3 adds scan_recovery_state

        for name, tool in mcp._tool_manager._tools.items():
            assert tool.annotations is not None, f"Tool {name} has no annotations"
            assert tool.annotations.destructiveHint is False, (
                f"Tool {name} destructiveHint is not False"
            )
            if name in expected_read_only:
                assert tool.annotations.readOnlyHint is True, (
                    f"Tool {name} readOnlyHint is not True"
                )
            elif name in expected_writing:
                assert tool.annotations.readOnlyHint is False, (
                    f"Tool {name} readOnlyHint is not False"
                )
            else:
                pytest.fail(f"Unhandled tool annotation expectation for {name}")


# ---------------------------------------------------------------------------
# MCP-03: No dangerous tools
# ---------------------------------------------------------------------------


DANGEROUS_TOOLS = {"run_bash", "edit_file", "grep_search", "read_file", "write_file"}


class TestNoDangerousTools:
    """MCP-03: Dangerous tools not exposed via MCP."""

    def test_no_dangerous_tools(self):
        """Tool names do not include run_bash, edit_file, grep_search, or any tool from tools.py."""
        from ollarma.mcp_server import mcp

        registered = set(mcp._tool_manager._tools.keys())
        overlap = registered & DANGEROUS_TOOLS
        assert not overlap, f"Dangerous tools exposed via MCP: {overlap}"


# ---------------------------------------------------------------------------
# MCP-04: Import guards + stderr logging
# ---------------------------------------------------------------------------


class TestImportGuards:
    """MCP-04: mcp_server.py has no Rich/Typer imports; logging goes to stderr."""

    def test_import_guards_no_rich(self):
        """mcp_server.py source code contains no 'import rich' or 'from rich' statements."""
        from ollarma import mcp_server

        source = inspect.getsource(mcp_server)
        import_lines = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        rich_imports = [
            line for line in import_lines
            if line.startswith("from rich") or line.startswith("import rich")
        ]
        assert not rich_imports, (
            f"mcp_server.py has Rich import statements: {rich_imports}"
        )

    def test_import_guards_no_typer(self):
        """mcp_server.py source code contains no 'import typer' or 'from typer' statements."""
        from ollarma import mcp_server

        source = inspect.getsource(mcp_server)
        import_lines = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        typer_imports = [
            line for line in import_lines
            if line.startswith("from typer") or line.startswith("import typer")
        ]
        assert not typer_imports, (
            f"mcp_server.py has Typer import statements: {typer_imports}"
        )

    def test_logging_to_stderr(self):
        """Logging is configured with stream=sys.stderr before application imports."""
        from ollarma import mcp_server

        source = inspect.getsource(mcp_server)
        lines = source.splitlines()

        # Find the logging.basicConfig call and the stderr reference.
        # The basicConfig call may span multiple lines, so track them separately.
        basicconfig_line = None
        stderr_line = None
        first_ollarma_import_line = None

        for i, line in enumerate(lines):
            stripped = line.strip()
            if "logging.basicConfig" in stripped and basicconfig_line is None:
                basicconfig_line = i
            if "stderr" in stripped and basicconfig_line is not None and stderr_line is None:
                stderr_line = i
            if stripped.startswith("from ollarma") and first_ollarma_import_line is None:
                first_ollarma_import_line = i

        assert basicconfig_line is not None, "No logging.basicConfig found in mcp_server.py"
        assert stderr_line is not None, "No stderr reference found near logging.basicConfig"
        if first_ollarma_import_line is not None:
            assert basicconfig_line < first_ollarma_import_line, (
                "logging.basicConfig(stream=sys.stderr) must come BEFORE ollarma imports"
            )


# ---------------------------------------------------------------------------
# MCP-05: Inference semaphore + async signatures
# ---------------------------------------------------------------------------


class TestInferenceConcurrency:
    """MCP-05: Semaphore guards inference; tool functions are async."""

    def test_inference_semaphore_exists(self):
        """Module-level _inference_sem is asyncio.Semaphore(1)."""
        from ollarma import mcp_server

        assert hasattr(mcp_server, "_inference_sem"), "Missing _inference_sem"
        sem = mcp_server._inference_sem
        assert isinstance(sem, asyncio.Semaphore), (
            f"_inference_sem is {type(sem)}, expected asyncio.Semaphore"
        )
        assert sem._value == 1, f"Semaphore value is {sem._value}, expected 1"

    def test_chat_with_model_is_async(self):
        """chat_with_model tool function is a coroutine function."""
        from ollarma.mcp_server import mcp

        tool = mcp._tool_manager._tools["chat_with_model"]
        assert inspect.iscoroutinefunction(tool.fn), (
            "chat_with_model is not an async function"
        )

    def test_route_prompt_is_async(self):
        """route_prompt tool function is a coroutine function."""
        from ollarma.mcp_server import mcp

        tool = mcp._tool_manager._tools["route_prompt"]
        assert inspect.iscoroutinefunction(tool.fn), (
            "route_prompt is not an async function"
        )

    def test_run_benchmark_is_async(self):
        """run_benchmark tool function is a coroutine function."""
        from ollarma.mcp_server import mcp

        tool = mcp._tool_manager._tools["run_benchmark"]
        assert inspect.iscoroutinefunction(tool.fn), (
            "run_benchmark is not an async function"
        )

    def test_kb_search_is_async(self):
        """kb_search tool function is a coroutine function."""
        from ollarma.mcp_server import mcp

        tool = mcp._tool_manager._tools["kb_search"]
        assert inspect.iscoroutinefunction(tool.fn), (
            "kb_search is not an async function"
        )


class TestRoutePromptTool:
    """Phase 16: MCP route_prompt delegates to the shared service layer."""

    def test_route_prompt_delegates_to_service(self, monkeypatch):
        """route_prompt returns the shared service result as JSON-serializable dict."""
        from ollarma import mcp_server
        from ollarma.service import RouteResult

        async def run_test():
            monkeypatch.setattr(
                mcp_server.service,
                "route_prompt",
                lambda **kwargs: RouteResult(
                    final_response="routed",
                    tool_calls_count=0,
                    model=None,
                    project="overwatch",
                    lane="kb_direct",
                    reason_code="KB_DIRECT_ANSWER",
                    query_class="file_lookup",
                    kb_status="ready",
                ),
            )
            result = await mcp_server.route_prompt(
                None,
                project="overwatch",
                prompt="Inspect autorun readiness.",
            )
            assert result["final_response"] == "routed"
            assert result["tool_calls_count"] == 0
            assert result["model"] is None
            assert result["project"] == "overwatch"
            assert result["lane"] == "kb_direct"
            assert result["reason_code"] == "KB_DIRECT_ANSWER"

        asyncio.run(run_test())

    def test_kb_status_delegates_to_service(self, monkeypatch):
        """kb_status returns the shared KB status payload."""
        from ollarma import mcp_server
        from ollarma.kb_search import KBStatus

        async def run_test():
            monkeypatch.setattr(
                mcp_server.service,
                "get_project_kb_status",
                lambda *args, **kwargs: KBStatus(
                    project="overwatch",
                    status="ready",
                    freshness_hours=24,
                    stale_behavior="escalate",
                    built_at="2026-04-10T12:00:00Z",
                    artifact_root=".ollarma/kb",
                    search_db_path=".ollarma/kb/search.sqlite",
                    document_count=12,
                    chunk_count=30,
                ),
            )
            result = await mcp_server.kb_status(None, project="overwatch")
            assert result["status"] == "ready"
            assert result["document_count"] == 12

        asyncio.run(run_test())

    def test_kb_search_delegates_to_service(self, monkeypatch):
        """kb_search returns bounded read-only hits unchanged."""
        from ollarma import mcp_server
        from ollarma.kb_search import KBSearchHit, KBSearchResult

        async def run_test():
            monkeypatch.setattr(
                mcp_server.service,
                "search_project_kb",
                lambda *args, **kwargs: KBSearchResult(
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
                ),
            )
            result = await mcp_server.kb_search(
                None,
                project="overwatch",
                query="manifest",
                limit=3,
            )
            assert result["hit_count"] == 1
            assert result["hits"][0]["path"] == "docs/workflow.md"

        asyncio.run(run_test())

    def test_submit_workflow_delegates_to_service(self, monkeypatch):
        """submit_workflow returns the shared workflow admission result."""
        from ollarma import mcp_server
        from ollarma.service import WorkflowSubmissionResult
        from ollarma.scheduler import RuntimeSnapshot

        async def run_test():
            monkeypatch.setattr(
                mcp_server.service,
                "submit_workflow",
                lambda **kwargs: WorkflowSubmissionResult(
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
                ),
            )
            result = await mcp_server.submit_workflow(
                None,
                project="overwatch",
                manifest_ref=".ollarma/manifests/workflow.json",
                step_id="execute-script",
            )
            assert result["lane"] == "workflow_execution_queue"
            assert result["status"] == "accepted"

        asyncio.run(run_test())

    def test_submit_workflow_dependency_missing_receipt(self, monkeypatch):
        """submit_workflow returns DEPENDENCY_MISSING payloads unchanged."""
        from ollarma import mcp_server
        from ollarma.service import WorkflowSubmissionResult
        from ollarma.scheduler import RuntimeSnapshot

        async def run_test():
            monkeypatch.setattr(
                mcp_server.service,
                "submit_workflow",
                lambda **kwargs: WorkflowSubmissionResult(
                    project="overwatch",
                    manifest_ref={"repo_relative": ".ollarma/manifests/workflow.json"},
                    manifest_digest="sha256:manifest",
                    run_id="run-001",
                    step_id="execute-script",
                    task_class="validated-pipeline-step",
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
            result = await mcp_server.submit_workflow(
                None,
                project="overwatch",
                manifest_ref=".ollarma/manifests/workflow.json",
                step_id="execute-script",
            )
            assert result["reason_code"] == "DEPENDENCY_MISSING"
            assert result["status"] == "rejected"

        asyncio.run(run_test())


# ---------------------------------------------------------------------------
# NS-01: Namespace prefix parameter on MCP transport
# ---------------------------------------------------------------------------


class TestNamespaceMCPTransport:
    """NS-01: MCP route_prompt and submit_workflow accept optional namespace_prefix parameter."""

    def test_route_prompt_accepts_namespace_prefix_param(self):
        """route_prompt MCP tool signature includes optional namespace_prefix parameter."""
        from ollarma.mcp_server import mcp
        import inspect

        tool = mcp._tool_manager._tools["route_prompt"]
        sig = inspect.signature(tool.fn)
        assert "namespace_prefix" in sig.parameters, (
            "route_prompt MCP tool is missing namespace_prefix parameter"
        )
        param = sig.parameters["namespace_prefix"]
        # Must be optional (default None)
        assert param.default is None, (
            f"namespace_prefix default should be None, got {param.default!r}"
        )

    def test_submit_workflow_accepts_namespace_prefix_param(self):
        """submit_workflow MCP tool signature includes optional namespace_prefix parameter."""
        from ollarma.mcp_server import mcp
        import inspect

        tool = mcp._tool_manager._tools["submit_workflow"]
        sig = inspect.signature(tool.fn)
        assert "namespace_prefix" in sig.parameters, (
            "submit_workflow MCP tool is missing namespace_prefix parameter"
        )
        param = sig.parameters["namespace_prefix"]
        assert param.default is None, (
            f"namespace_prefix default should be None, got {param.default!r}"
        )

    def test_route_prompt_threads_namespace_prefix_to_service(self, monkeypatch):
        """route_prompt passes namespace_prefix kwarg through to service.route_prompt."""
        from ollarma import mcp_server
        from ollarma.service import RouteResult

        captured = {}

        async def run_test():
            def _fake_route(**kwargs):
                captured.update(kwargs)
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

            monkeypatch.setattr(mcp_server.service, "route_prompt", _fake_route)

            result = await mcp_server.route_prompt(
                None,
                project="overwatch",
                prompt="hello",
                namespace_prefix="ollarma-demo:",
            )
            assert result["final_response"] == "routed"

        asyncio.run(run_test())
        assert captured.get("namespace_prefix") == "ollarma-demo:"
