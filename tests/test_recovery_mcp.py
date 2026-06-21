"""Tests for recovery MCP tools (phase 43, MCP-01/02, TEST-04)."""
from __future__ import annotations

import pathlib
import subprocess

import pytest


def _git(repo: pathlib.Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")


@pytest.fixture
def clean_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


class TestMCPToolRegistration:
    def test_scan_recovery_state_registered(self) -> None:
        from ollarma import mcp_server  # noqa: PLC0415
        assert hasattr(mcp_server, "scan_recovery_state")
        assert callable(mcp_server.scan_recovery_state)

    def test_read_recovery_report_registered(self) -> None:
        from ollarma import mcp_server  # noqa: PLC0415
        assert hasattr(mcp_server, "read_recovery_report")
        assert callable(mcp_server.read_recovery_report)


class TestScanRecoveryStateTool:
    def test_scan_returns_packet_dict(self, clean_repo: pathlib.Path) -> None:
        from ollarma import mcp_server  # noqa: PLC0415
        result = mcp_server.scan_recovery_state(project_root=str(clean_repo))
        assert result["state"] == "clean"
        assert result["blocker_code"] == "OK"
        assert result["schema_version"] == 1

    def test_scan_persists_packet(self, clean_repo: pathlib.Path) -> None:
        from ollarma import mcp_server  # noqa: PLC0415
        mcp_server.scan_recovery_state(project_root=str(clean_repo))
        latest = clean_repo / ".ollarma" / "incidents" / "latest.json"
        assert latest.exists()

    def test_scan_annotated_writes_local_files(self) -> None:
        """MCP-01: scan_recovery_state is annotated WRITES_LOCAL_FILES."""
        from ollarma import mcp_server  # noqa: PLC0415
        # FastMCP exposes tools via mcp._tool_manager._tools or similar.
        # The annotation lives on the FunctionTool wrapper. Walk the registry
        # if available; otherwise fall back to reading the source marker.
        import inspect
        src = inspect.getsource(mcp_server.scan_recovery_state)
        # The decorator is on the original function so the source might not
        # show the annotation. Confirm via module-level search instead.
        mod_src = pathlib.Path(inspect.getfile(mcp_server)).read_text()
        # Find the block for scan_recovery_state and look for WRITES_LOCAL_FILES
        idx = mod_src.find("def scan_recovery_state(")
        assert idx > 0, "scan_recovery_state not found"
        preceding = mod_src[max(0, idx - 200):idx]
        assert "WRITES_LOCAL_FILES" in preceding


class TestReadRecoveryReportTool:
    def test_read_without_scan_returns_no_report(
        self, clean_repo: pathlib.Path,
    ) -> None:
        from ollarma import mcp_server  # noqa: PLC0415
        result = mcp_server.read_recovery_report(project_root=str(clean_repo))
        assert result.get("has_report") is False

    def test_read_after_scan_returns_packet(
        self, clean_repo: pathlib.Path,
    ) -> None:
        from ollarma import mcp_server  # noqa: PLC0415
        mcp_server.scan_recovery_state(project_root=str(clean_repo))
        result = mcp_server.read_recovery_report(project_root=str(clean_repo))
        assert result.get("has_report") is True
        assert result["packet"]["state"] == "clean"

    def test_read_annotated_read_only(self) -> None:
        """MCP-02: read_recovery_report is annotated READ_ONLY."""
        from ollarma import mcp_server  # noqa: PLC0415
        import inspect
        mod_src = pathlib.Path(inspect.getfile(mcp_server)).read_text()
        idx = mod_src.find("def read_recovery_report(")
        assert idx > 0, "read_recovery_report not found"
        preceding = mod_src[max(0, idx - 200):idx]
        assert "READ_ONLY" in preceding
