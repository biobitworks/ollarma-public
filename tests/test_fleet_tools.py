"""Tests for harness/fleet_tools.py -- Fleet tool implementations.

Covers gh_tool, git_cmd_tool, skill_invoke_tool, db_query_tool,
mcp_query_tool, and build_tool_registry.
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# gh_tool tests
# ---------------------------------------------------------------------------


class TestGhTool:
    """Tests for gh_tool subprocess wrapper."""

    def test_gh_pr_list_appends_json_fields(self):
        """gh_tool appends --json with default pr list fields when not present."""
        from ollarma.fleet_tools import gh_tool

        with patch("ollarma.fleet_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout='[{"number":1}]', stderr="", returncode=0
            )
            result = gh_tool("pr list --limit 5", cwd="/tmp")
            args_called = mock_run.call_args[0][0]
            assert "--json" in args_called
            assert "number,title,state,author" in args_called

    def test_gh_does_not_double_json(self):
        """gh_tool does not add --json when already in args."""
        from ollarma.fleet_tools import gh_tool

        with patch("ollarma.fleet_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout='{"data":"ok"}', stderr="", returncode=0
            )
            result = gh_tool("pr list --json number,url", cwd="/tmp")
            args_called = mock_run.call_args[0][0]
            # Count occurrences of --json
            json_count = args_called.count("--json")
            assert json_count == 1

    def test_gh_returns_error_on_file_not_found(self):
        """gh_tool returns error string when gh CLI is missing."""
        from ollarma.fleet_tools import gh_tool

        with patch(
            "ollarma.fleet_tools.subprocess.run",
            side_effect=FileNotFoundError("gh not found"),
        ):
            result = gh_tool("pr list", cwd="/tmp")
            assert result.startswith("Error:")
            assert "gh not found" in result or "not found" in result.lower()

    def test_gh_returns_stdout(self):
        """gh_tool returns stdout from successful command."""
        from ollarma.fleet_tools import gh_tool

        with patch("ollarma.fleet_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="pr data here", stderr="", returncode=0
            )
            result = gh_tool("pr list", cwd="/tmp")
            assert "pr data here" in result

    def test_gh_issue_list_json_fields(self):
        """gh_tool appends issue-specific --json fields for issue list."""
        from ollarma.fleet_tools import gh_tool

        with patch("ollarma.fleet_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="[]", stderr="", returncode=0
            )
            gh_tool("issue list", cwd="/tmp")
            args_called = mock_run.call_args[0][0]
            assert "--json" in args_called
            assert "number,title,state" in args_called

    def test_gh_caps_output_at_max_size(self):
        """gh_tool truncates output exceeding MAX_OUTPUT_SIZE."""
        from ollarma.fleet_tools import gh_tool
        from ollarma.tools import MAX_OUTPUT_SIZE

        with patch("ollarma.fleet_tools.subprocess.run") as mock_run:
            big_output = "x" * (MAX_OUTPUT_SIZE + 5000)
            mock_run.return_value = MagicMock(
                stdout=big_output, stderr="", returncode=0
            )
            result = gh_tool("pr list", cwd="/tmp")
            assert len(result) <= MAX_OUTPUT_SIZE + 100  # allow for truncation msg


# ---------------------------------------------------------------------------
# git_cmd_tool tests
# ---------------------------------------------------------------------------


class TestGitCmdTool:
    """Tests for git_cmd_tool subprocess wrapper with destructive blocklist."""

    def test_git_status_returns_stdout(self):
        """git_cmd_tool('status') executes and returns stdout."""
        from ollarma.fleet_tools import git_cmd_tool

        with patch("ollarma.fleet_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="On branch main", stderr="", returncode=0
            )
            result = git_cmd_tool("status", cwd="/tmp")
            assert "On branch main" in result
            args_called = mock_run.call_args[0][0]
            assert args_called[0] == "git"
            assert args_called[1] == "status"

    def test_git_push_force_blocked(self):
        """git_cmd_tool blocks 'push --force' as destructive."""
        from ollarma.fleet_tools import git_cmd_tool

        result = git_cmd_tool("push --force", cwd="/tmp")
        assert "Error:" in result or "blocked" in result.lower()

    def test_git_reset_hard_blocked(self):
        """git_cmd_tool blocks 'reset --hard' as destructive."""
        from ollarma.fleet_tools import git_cmd_tool

        result = git_cmd_tool("reset --hard", cwd="/tmp")
        assert "Error:" in result or "blocked" in result.lower()

    def test_git_clean_f_blocked(self):
        """git_cmd_tool blocks 'clean -f' as destructive."""
        from ollarma.fleet_tools import git_cmd_tool

        result = git_cmd_tool("clean -f", cwd="/tmp")
        assert "Error:" in result or "blocked" in result.lower()

    def test_git_branch_d_blocked(self):
        """git_cmd_tool blocks 'branch -D' as destructive."""
        from ollarma.fleet_tools import git_cmd_tool

        result = git_cmd_tool("branch -D feature", cwd="/tmp")
        assert "Error:" in result or "blocked" in result.lower()

    def test_git_allows_log(self):
        """git_cmd_tool allows 'log' subcommand."""
        from ollarma.fleet_tools import git_cmd_tool

        with patch("ollarma.fleet_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="commit abc123", stderr="", returncode=0
            )
            result = git_cmd_tool("log --oneline -5", cwd="/tmp")
            assert "commit abc123" in result

    def test_git_allows_diff(self):
        """git_cmd_tool allows 'diff' subcommand."""
        from ollarma.fleet_tools import git_cmd_tool

        with patch("ollarma.fleet_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="+added line", stderr="", returncode=0
            )
            result = git_cmd_tool("diff", cwd="/tmp")
            assert "+added line" in result

    def test_git_allows_show(self):
        """git_cmd_tool allows 'show' subcommand."""
        from ollarma.fleet_tools import git_cmd_tool

        with patch("ollarma.fleet_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="show output", stderr="", returncode=0
            )
            result = git_cmd_tool("show HEAD", cwd="/tmp")
            assert "show output" in result

    def test_git_allows_blame(self):
        """git_cmd_tool allows 'blame' subcommand."""
        from ollarma.fleet_tools import git_cmd_tool

        with patch("ollarma.fleet_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="blame output", stderr="", returncode=0
            )
            result = git_cmd_tool("blame file.py", cwd="/tmp")
            assert "blame output" in result


# ---------------------------------------------------------------------------
# skill_invoke_tool tests
# ---------------------------------------------------------------------------


class TestSkillInvokeTool:
    """Tests for skill_invoke_tool file reader."""

    def test_skill_invoke_reads_skill_md(self, tmp_path):
        """skill_invoke_tool reads SKILL.md from skills dir."""
        from ollarma.fleet_tools import skill_invoke_tool

        skill_dir = tmp_path / "gsigmad-run-experiment"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Run Experiment\nDo the thing.")
        result = skill_invoke_tool("gsigmad-run-experiment", skills_dir=str(tmp_path))
        assert "# Run Experiment" in result
        assert "Do the thing." in result

    def test_skill_invoke_nonexistent_returns_error(self, tmp_path):
        """skill_invoke_tool returns error for missing skill."""
        from ollarma.fleet_tools import skill_invoke_tool

        result = skill_invoke_tool("nonexistent-skill", skills_dir=str(tmp_path))
        assert result.startswith("Error:")
        assert "nonexistent-skill" in result

    def test_skill_invoke_empty_name_returns_error(self, tmp_path):
        """skill_invoke_tool returns error for empty name."""
        from ollarma.fleet_tools import skill_invoke_tool

        result = skill_invoke_tool("", skills_dir=str(tmp_path))
        assert result.startswith("Error:")

    def test_skill_invoke_invalid_name_returns_error(self, tmp_path):
        """skill_invoke_tool rejects names with path traversal chars."""
        from ollarma.fleet_tools import skill_invoke_tool

        result = skill_invoke_tool("../etc/passwd", skills_dir=str(tmp_path))
        assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# build_tool_registry tests
# ---------------------------------------------------------------------------


class TestBuildToolRegistry:
    """Tests for the build_tool_registry factory function."""

    def test_no_adapter_returns_base_tools(self):
        """build_tool_registry with no adapter returns base 4 tools."""
        from ollarma.fleet_tools import build_tool_registry

        registry, definitions = build_tool_registry("/tmp/project")
        assert "read_file" in registry
        assert "edit_file" in registry
        assert "run_bash" in registry
        assert "grep_search" in registry
        assert len(registry) == 4
        assert len(definitions) == 4

    def test_adapter_with_databases_includes_db_query(self):
        """build_tool_registry with databases in adapter includes db_query."""
        from ollarma.fleet import AdapterConfig
        from ollarma.fleet_tools import build_tool_registry

        adapter = AdapterConfig(
            project_name="test",
            project_root="/tmp/test",
            databases=[{"type": "arangodb", "host": "localhost", "port": 8531, "db": "test"}],
        )
        registry, definitions = build_tool_registry("/tmp/test", adapter=adapter)
        assert "db_query" in registry
        assert "gh" in registry
        assert "git_cmd" in registry
        assert "skill_invoke" in registry

    def test_adapter_without_databases_omits_db_query(self):
        """build_tool_registry without databases omits db_query."""
        from ollarma.fleet import AdapterConfig
        from ollarma.fleet_tools import build_tool_registry

        adapter = AdapterConfig(
            project_name="test",
            project_root="/tmp/test",
        )
        registry, definitions = build_tool_registry("/tmp/test", adapter=adapter)
        assert "db_query" not in registry
        # But still has gh, git_cmd, skill_invoke
        assert "gh" in registry
        assert "git_cmd" in registry
        assert "skill_invoke" in registry

    def test_adapter_with_databases_and_mcps_returns_all_tools(self):
        """build_tool_registry with databases+mcps returns all fleet tools."""
        from ollarma.fleet import AdapterConfig
        from ollarma.fleet_tools import build_tool_registry

        adapter = AdapterConfig(
            project_name="test",
            project_root="/tmp/test",
            databases=[{"type": "arangodb", "host": "localhost", "port": 8531, "db": "test"}],
            mcps=[{"name": "antigence", "command": "antigence-mcp"}],
        )
        registry, definitions = build_tool_registry("/tmp/test", adapter=adapter)
        # All fleet tools present
        assert "gh" in registry
        assert "git_cmd" in registry
        assert "skill_invoke" in registry
        assert "db_query" in registry
        assert "mcp_query" in registry
        # Base tools still present
        assert "read_file" in registry
        assert "edit_file" in registry
        # Total: 4 base + 5 fleet = 9
        assert len(registry) == 9

    def test_adapter_no_databases_no_mcps_returns_base_plus_three(self):
        """build_tool_registry with adapter but no db/mcp returns 7 tools (4 base + 3 always)."""
        from ollarma.fleet import AdapterConfig
        from ollarma.fleet_tools import build_tool_registry

        adapter = AdapterConfig(
            project_name="test",
            project_root="/tmp/test",
        )
        registry, definitions = build_tool_registry("/tmp/test", adapter=adapter)
        assert len(registry) == 7  # 4 base + gh + git_cmd + skill_invoke
        assert "mcp_query" not in registry
        assert "db_query" not in registry

    def test_service_mode_registry_excludes_mutating_and_external_tools(self):
        """service_mode registry keeps only the bounded read-only tool surface."""
        from ollarma.fleet import AdapterConfig
        from ollarma.fleet_tools import build_tool_registry

        adapter = AdapterConfig(
            project_name="test",
            project_root="/tmp/test",
            databases=[{"type": "arangodb", "host": "localhost", "port": 8531, "db": "test"}],
            mcps=[{"name": "antigence", "command": "antigence-mcp"}],
        )
        registry, definitions = build_tool_registry(
            "/tmp/test",
            adapter=adapter,
            service_mode=True,
        )

        assert set(registry) == {"read_file", "grep_search", "skill_invoke"}
        definition_names = {definition["function"]["name"] for definition in definitions}
        assert definition_names == {"read_file", "grep_search", "skill_invoke"}

    @patch("ollarma.fleet_tools.mcp_query_tool", return_value="ok")
    def test_mcp_registry_forwards_tool_name_and_arguments(self, mock_mcp_query):
        """adapter registry lambda forwards explicit MCP tool arguments."""
        from ollarma.fleet import AdapterConfig
        from ollarma.fleet_tools import build_tool_registry

        adapter = AdapterConfig(
            project_name="test",
            project_root="/tmp/test",
            mcps=[{"name": "antigence", "command": "antigence-mcp"}],
        )
        registry, _definitions = build_tool_registry("/tmp/test", adapter=adapter)

        result = registry["mcp_query"](
            query="ignored",
            tool_name="antigence_scan",
            arguments={"code": "eval(data)"},
        )

        assert result == "ok"
        mock_mcp_query.assert_called_once_with(
            "ignored",
            "antigence-mcp",
            tool_name="antigence_scan",
            arguments={"code": "eval(data)"},
        )


# ---------------------------------------------------------------------------
# db_query_tool tests
# ---------------------------------------------------------------------------


class TestDbQueryTool:
    """Tests for db_query_tool ArangoDB wrapper."""

    def test_db_query_executes_aql_returns_json(self, monkeypatch):
        """db_query_tool executes AQL and returns JSON string of results."""
        from ollarma.fleet_tools import db_query_tool

        monkeypatch.setenv("ARANGO_USER", "testuser")
        monkeypatch.setenv("ARANGO_PASSWORD", "testpass")

        mock_cursor = MagicMock()
        mock_cursor.__iter__ = MagicMock(return_value=iter([{"_key": "1", "name": "doc1"}]))
        mock_db = MagicMock()
        mock_db.aql.execute.return_value = mock_cursor
        mock_client_instance = MagicMock()
        mock_client_instance.db.return_value = mock_db

        with patch("ollarma.fleet_tools._import_arango_client") as mock_import:
            mock_import.return_value = MagicMock(return_value=mock_client_instance)
            result = db_query_tool(
                "FOR d IN docs RETURN d",
                {"host": "localhost", "port": 8531, "db": "testdb"},
            )
        assert '"name"' in result or "doc1" in result

    def test_db_query_caps_at_100_results(self, monkeypatch):
        """db_query_tool caps results at 100 items."""
        from ollarma.fleet_tools import db_query_tool

        monkeypatch.setenv("ARANGO_USER", "root")
        monkeypatch.setenv("ARANGO_PASSWORD", "")

        # Create 150 results
        big_results = [{"_key": str(i)} for i in range(150)]
        mock_cursor = MagicMock()
        mock_cursor.__iter__ = MagicMock(return_value=iter(big_results))
        mock_db = MagicMock()
        mock_db.aql.execute.return_value = mock_cursor
        mock_client_instance = MagicMock()
        mock_client_instance.db.return_value = mock_db

        with patch("ollarma.fleet_tools._import_arango_client") as mock_import:
            mock_import.return_value = MagicMock(return_value=mock_client_instance)
            result = db_query_tool(
                "FOR d IN docs RETURN d",
                {"host": "localhost", "port": 8531, "db": "testdb"},
            )
        parsed = json.loads(result)
        assert len(parsed) <= 100

    def test_db_query_reads_env_vars(self, monkeypatch):
        """db_query_tool reads ARANGO_USER and ARANGO_PASSWORD from env."""
        from ollarma.fleet_tools import db_query_tool

        monkeypatch.setenv("ARANGO_USER", "myuser")
        monkeypatch.setenv("ARANGO_PASSWORD", "mypass")

        mock_cursor = MagicMock()
        mock_cursor.__iter__ = MagicMock(return_value=iter([]))
        mock_db = MagicMock()
        mock_db.aql.execute.return_value = mock_cursor
        mock_client_instance = MagicMock()
        mock_client_instance.db.return_value = mock_db
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        with patch("ollarma.fleet_tools._import_arango_client") as mock_import:
            mock_import.return_value = mock_client_cls
            db_query_tool(
                "RETURN 1",
                {"host": "localhost", "port": 8531, "db": "testdb"},
            )
        # Verify db() was called with the env var credentials
        mock_client_instance.db.assert_called_once_with(
            "testdb", username="myuser", password="mypass"
        )

    def test_db_query_returns_error_on_connection_failure(self, monkeypatch):
        """db_query_tool returns error string when connection fails."""
        from ollarma.fleet_tools import db_query_tool

        monkeypatch.setenv("ARANGO_USER", "root")
        monkeypatch.setenv("ARANGO_PASSWORD", "")

        with patch("ollarma.fleet_tools._import_arango_client") as mock_import:
            mock_import.return_value = MagicMock(
                side_effect=ConnectionError("Connection refused")
            )
            result = db_query_tool(
                "RETURN 1",
                {"host": "localhost", "port": 8531, "db": "testdb"},
            )
        assert result.startswith("Error:")

    def test_db_query_returns_error_on_invalid_aql(self, monkeypatch):
        """db_query_tool returns error string for invalid AQL."""
        from ollarma.fleet_tools import db_query_tool

        monkeypatch.setenv("ARANGO_USER", "root")
        monkeypatch.setenv("ARANGO_PASSWORD", "")

        mock_db = MagicMock()
        mock_db.aql.execute.side_effect = Exception("AQL syntax error")
        mock_client_instance = MagicMock()
        mock_client_instance.db.return_value = mock_db

        with patch("ollarma.fleet_tools._import_arango_client") as mock_import:
            mock_import.return_value = MagicMock(return_value=mock_client_instance)
            result = db_query_tool(
                "INVALID AQL",
                {"host": "localhost", "port": 8531, "db": "testdb"},
            )
        assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# mcp_query_tool tests
# ---------------------------------------------------------------------------


class TestMcpQueryTool:
    """Tests for mcp_query_tool stdio MCP client wrapper."""

    def test_mcp_query_returns_tool_output(self):
        """mcp_query_tool returns formatted output from the MCP call helper."""
        from ollarma.fleet_tools import mcp_query_tool

        with patch(
            "ollarma.fleet_tools._call_mcp_tool",
            new_callable=AsyncMock,
        ) as mock_call:
            mock_call.return_value = "ok"
            result = mcp_query_tool("test query", "python -m mcp_server")
            assert result == "ok"

    def test_mcp_query_passes_tool_name_and_arguments(self):
        """mcp_query_tool forwards tool_name and explicit arguments to helper."""
        from ollarma.fleet_tools import mcp_query_tool

        with patch(
            "ollarma.fleet_tools._call_mcp_tool",
            new_callable=AsyncMock,
        ) as mock_call:
            mock_call.return_value = "ok"
            result = mcp_query_tool(
                "ignored query",
                "python -m mcp_server",
                tool_name="overwatch_health",
                arguments={"project_id": "ow"},
            )
            assert result == "ok"
            assert mock_call.await_args.args[2] == "overwatch_health"
            assert mock_call.await_args.args[3] == {"project_id": "ow"}

    def test_mcp_query_returns_error_on_timeout(self):
        """mcp_query_tool returns error on timeout."""
        from ollarma.fleet_tools import mcp_query_tool

        def _raise_timeout(coro):
            coro.close()
            raise TimeoutError

        with patch(
            "ollarma.fleet_tools.asyncio.run",
            side_effect=_raise_timeout,
        ):
            result = mcp_query_tool("test query", "python -m mcp_server", timeout=30)
            assert result.startswith("Error:")
            assert "timed out" in result.lower()
