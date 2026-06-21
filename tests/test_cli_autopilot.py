"""Tests for autopilot and escalate CLI commands in bench.py.

Tests CLI command registration, argument parsing, output rendering,
and fleet-wide autopilot execution. Uses typer.testing.CliRunner
for CLI invocation without live Ollama or real project directories.
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import patch, MagicMock

import orjson
import pytest
from typer.testing import CliRunner

from ollarma.cli import app
from ollarma.autopilot import (
    AutopilotReport,
    AssetInventory,
    AssetResult,
    DiscoveredAsset,
    TierMapping,
)
from ollarma.fleet import AdapterConfig

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    name: str = "test-project",
    root: str = "/tmp/test-project",
) -> AdapterConfig:
    """Build a minimal AdapterConfig for testing."""
    return AdapterConfig(
        project_name=name,
        project_root=root,
        project_type="python",
        tools=["gh", "git_cmd"],
        antibodies=["prompt_injection"],
        adapter_source="test",
    )


def _make_inventory(
    project_name: str = "test-project",
    project_root: str = "/tmp/test-project",
) -> AssetInventory:
    """Build a minimal AssetInventory for testing."""
    return AssetInventory(
        project_name=project_name,
        project_root=project_root,
        assets=(
            DiscoveredAsset(path="/tmp/test-project/script.py", asset_type="script", has_entrypoint=True),
            DiscoveredAsset(path="/tmp/test-project/notebook.ipynb", asset_type="notebook", has_entrypoint=False, code_cell_count=5),
        ),
        counts={"script": 1, "notebook": 1},
    )


def _make_report(
    project_name: str = "test-project",
    project_root: str = "/tmp/test-project",
    run_executed: bool = False,
    results: tuple[AssetResult, ...] = (),
) -> AutopilotReport:
    """Build a minimal AutopilotReport for testing."""
    passed = sum(1 for r in results if r.exit_code == 0)
    failed = sum(1 for r in results if r.exit_code != 0 and not r.escalation_needed)
    escalation_needed = sum(1 for r in results if r.escalation_needed)
    tokens = sum(r.tokens_consumed for r in results)
    return AutopilotReport(
        project_name=project_name,
        project_root=project_root,
        inventory=_make_inventory(project_name, project_root),
        tier_map={
            "code": TierMapping(tier="code", model="qwen3:8b", model_size_b=8.0, quality_mean=0.85, degraded_confidence=False),
            "science": TierMapping(tier="science", model="qwen3:8b", model_size_b=8.0, quality_mean=0.92, degraded_confidence=False),
        },
        results=results,
        total_assets=2,
        passed=passed,
        failed=failed,
        escalation_needed=escalation_needed,
        tokens_consumed_local=tokens,
        run_executed=run_executed,
    )


def _make_asset_result(
    asset_path: str = "/tmp/test-project/script.py",
    exit_code: int = 0,
    escalation_needed: bool = False,
    tokens: int = 100,
) -> AssetResult:
    """Build a minimal AssetResult for testing."""
    return AssetResult(
        asset_path=asset_path,
        asset_type="script",
        task_tier="code",
        model_used="qwen3:8b",
        exit_code=exit_code,
        stdout="OK" if exit_code == 0 else "",
        stderr="" if exit_code == 0 else "RuntimeError: failed",
        duration_s=1.5,
        tokens_consumed=tokens,
        escalated=False,
        escalation_needed=escalation_needed,
        degraded_confidence=False,
    )


# ---------------------------------------------------------------------------
# autopilot command -- registration and help
# ---------------------------------------------------------------------------


class TestAutopilotCommandRegistration:
    """Tests for autopilot command registration."""

    def test_command_registered(self) -> None:
        """autopilot is a registered CLI command."""
        result = runner.invoke(app, ["--help"])
        assert "autopilot" in result.output

    def test_has_run_option(self) -> None:
        """autopilot command has --run option."""
        result = runner.invoke(app, ["autopilot", "--help"])
        assert "--run" in result.output

    def test_has_fleet_option(self) -> None:
        """autopilot command has --fleet option."""
        result = runner.invoke(app, ["autopilot", "--help"])
        assert "--fleet" in result.output

    def test_has_threshold_option(self) -> None:
        """autopilot command has --threshold option."""
        result = runner.invoke(app, ["autopilot", "--help"])
        assert "--threshold" in result.output

    def test_has_include_option(self) -> None:
        """autopilot command has --include option."""
        result = runner.invoke(app, ["autopilot", "--help"])
        assert "--include" in result.output

    def test_has_exclude_option(self) -> None:
        """autopilot command has --exclude option."""
        result = runner.invoke(app, ["autopilot", "--help"])
        assert "--exclude" in result.output


# ---------------------------------------------------------------------------
# autopilot command -- single project discovery
# ---------------------------------------------------------------------------


class TestAutopilotCLI:
    """Tests for autopilot CLI command behavior."""

    @patch("ollarma.cli.run_autopilot")
    @patch("ollarma.service.resolve_project_adapter")
    @patch("ollarma.service.list_projects")
    def test_autopilot_cli_discovery_only(
        self, mock_list_projects, mock_resolve_adapter, mock_run, tmp_path
    ) -> None:
        """autopilot someproject shows Asset Inventory table."""
        adapter = _make_adapter()
        mock_list_projects.return_value = {"test-project": adapter}
        mock_resolve_adapter.return_value = adapter
        report = _make_report()
        mock_run.return_value = report

        result = runner.invoke(app, ["autopilot", "test-project"])
        assert result.exit_code == 0
        assert "Asset Inventory" in result.output
        mock_run.assert_called_once()
        # run=False for discovery-only
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("run") is False or (
            len(call_kwargs.args) >= 3 and call_kwargs.args[2] is False
        )

    @patch("ollarma.service.resolve_project_adapter")
    @patch("ollarma.service.list_projects")
    def test_autopilot_cli_project_not_found(
        self, mock_list_projects, mock_resolve_adapter
    ) -> None:
        """autopilot with unknown project exits 1."""
        mock_list_projects.return_value = {}
        mock_resolve_adapter.side_effect = ValueError("not found")

        result = runner.invoke(app, ["autopilot", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    @patch("ollarma.cli.run_autopilot")
    @patch("ollarma.service.resolve_project_adapter")
    @patch("ollarma.service.list_projects")
    def test_autopilot_cli_with_run(
        self, mock_list_projects, mock_resolve_adapter, mock_run, tmp_path
    ) -> None:
        """autopilot with --run shows Execution Results table."""
        adapter = _make_adapter()
        mock_list_projects.return_value = {"test-project": adapter}
        mock_resolve_adapter.return_value = adapter
        results = (
            _make_asset_result(exit_code=0),
            _make_asset_result(asset_path="/tmp/test-project/notebook.ipynb", exit_code=1),
        )
        report = _make_report(run_executed=True, results=results)
        mock_run.return_value = report

        result = runner.invoke(app, ["autopilot", "test-project", "--run"])
        assert result.exit_code == 0
        assert "Execution Results" in result.output
        # Verify --run=True was passed
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("run") is True

    @patch("ollarma.cli.run_autopilot")
    @patch("ollarma.service.list_projects")
    def test_autopilot_cli_fleet_mode(
        self, mock_list_projects, mock_run, tmp_path
    ) -> None:
        """autopilot --fleet runs across all projects and shows Fleet Summary."""
        adapter_a = _make_adapter("proj-a", "/tmp/proj-a")
        adapter_b = _make_adapter("proj-b", "/tmp/proj-b")
        mock_list_projects.return_value = {"proj-a": adapter_a, "proj-b": adapter_b}
        mock_run.side_effect = [
            _make_report("proj-a", "/tmp/proj-a"),
            _make_report("proj-b", "/tmp/proj-b"),
        ]

        result = runner.invoke(app, ["autopilot", "ignored", "--fleet"])
        assert result.exit_code == 0
        assert "Fleet Summary" in result.output
        assert mock_run.call_count == 2

    @patch("ollarma.cli.run_autopilot")
    @patch("ollarma.service.resolve_project_adapter")
    @patch("ollarma.service.list_projects")
    def test_autopilot_cli_threshold_flag(
        self, mock_list_projects, mock_resolve_adapter, mock_run
    ) -> None:
        """--threshold 0.5 passes through to run_autopilot."""
        adapter = _make_adapter()
        mock_list_projects.return_value = {"test-project": adapter}
        mock_resolve_adapter.return_value = adapter
        mock_run.return_value = _make_report()

        result = runner.invoke(app, ["autopilot", "test-project", "--threshold", "0.5"])
        assert result.exit_code == 0
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("threshold") == 0.5

    @patch("ollarma.cli.run_autopilot")
    @patch("ollarma.service.resolve_project_adapter")
    @patch("ollarma.service.list_projects")
    def test_autopilot_cli_include_exclude(
        self, mock_list_projects, mock_resolve_adapter, mock_run
    ) -> None:
        """--include and --exclude pass through to run_autopilot."""
        adapter = _make_adapter()
        mock_list_projects.return_value = {"test-project": adapter}
        mock_resolve_adapter.return_value = adapter
        mock_run.return_value = _make_report()

        result = runner.invoke(
            app,
            ["autopilot", "test-project", "--include", "*.py", "--exclude", "test_*"],
        )
        assert result.exit_code == 0
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("include_patterns") == ["*.py"]
        assert call_kwargs.kwargs.get("exclude_patterns") == ["test_*"]

    @patch("ollarma.cli.run_autopilot")
    @patch("ollarma.service.list_projects")
    def test_autopilot_fleet_empty_registry_exits_1(
        self, mock_list_projects, mock_run
    ) -> None:
        """autopilot --fleet with empty registry exits 1."""
        mock_list_projects.return_value = {}

        result = runner.invoke(app, ["autopilot", "ignored", "--fleet"])
        assert result.exit_code == 1
        assert "no projects" in result.output.lower()


# ---------------------------------------------------------------------------
# escalate command
# ---------------------------------------------------------------------------


class TestEscalateCLI:
    """Tests for escalate CLI command."""

    def test_command_registered(self) -> None:
        """escalate is a registered CLI command."""
        result = runner.invoke(app, ["--help"])
        assert "escalate" in result.output

    def test_has_run_id_option(self) -> None:
        """escalate command has --run-id option."""
        result = runner.invoke(app, ["escalate", "--help"])
        assert "--run-id" in result.output

    def test_escalate_cli_no_results(self, tmp_path) -> None:
        """escalate with no autopilot results exits 1."""
        # Run in a tmp directory with no results/ folder
        (tmp_path / "results").mkdir()
        import os
        orig_dir = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["escalate"])
            assert result.exit_code == 1
            assert "no autopilot results" in result.output.lower()
        finally:
            os.chdir(orig_dir)

    def test_escalate_cli_no_escalations(self, tmp_path) -> None:
        """escalate with no escalation-needed assets prints green message."""
        # Create a fake autopilot result with no escalations
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        report = _make_report(
            run_executed=True,
            results=(_make_asset_result(exit_code=0),),
        )
        report_path = results_dir / "autopilot-test-20260408T000000Z.json"
        report_path.write_bytes(orjson.dumps(report.model_dump(mode="json")))

        with patch("ollarma.cli.pathlib.Path") as mock_path_cls:
            # We need to be careful with Path mocking -- use chdir approach instead
            pass

        # Use monkeypatch on results dir via chdir
        import os
        orig_dir = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["escalate"])
            assert result.exit_code == 0
            assert "no assets need escalation" in result.output.lower()
        finally:
            os.chdir(orig_dir)

    def test_escalate_cli_with_escalations(self, tmp_path) -> None:
        """escalate with escalation-needed assets shows Escalation Checklist."""
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        escalated_result = _make_asset_result(
            asset_path="/tmp/test-project/failing.py",
            exit_code=1,
            escalation_needed=True,
        )
        report = _make_report(
            run_executed=True,
            results=(escalated_result,),
        )
        report_path = results_dir / "autopilot-test-20260408T000000Z.json"
        report_path.write_bytes(orjson.dumps(report.model_dump(mode="json")))

        import os
        orig_dir = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["escalate"])
            assert result.exit_code == 0
            assert "1 assets need escalation" in result.output
        finally:
            os.chdir(orig_dir)

    def test_escalate_cli_dual_output(self, tmp_path) -> None:
        """escalate writes both .json and .md files."""
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        escalated_result = _make_asset_result(
            asset_path="/tmp/test-project/failing.py",
            exit_code=1,
            escalation_needed=True,
        )
        report = _make_report(
            run_executed=True,
            results=(escalated_result,),
        )
        report_path = results_dir / "autopilot-test-20260408T000000Z.json"
        report_path.write_bytes(orjson.dumps(report.model_dump(mode="json")))

        import os
        orig_dir = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["escalate"])
            assert result.exit_code == 0
            # Verify both file types are referenced in output
            assert ".json" in result.output
            assert ".md" in result.output
            # Verify files were actually created
            json_files = list(results_dir.glob("escalation-*.json"))
            md_files = list(results_dir.glob("escalation-*.md"))
            assert len(json_files) == 1, f"Expected 1 JSON escalation file, got {json_files}"
            assert len(md_files) == 1, f"Expected 1 MD escalation file, got {md_files}"

            # Verify JSON content
            esc_data = orjson.loads(json_files[0].read_bytes())
            assert len(esc_data) == 1
            assert esc_data[0]["asset_path"] == "/tmp/test-project/failing.py"

            # Verify MD content
            md_content = md_files[0].read_text()
            assert "Escalation Checklist" in md_content
            assert "failing.py" in md_content
        finally:
            os.chdir(orig_dir)
