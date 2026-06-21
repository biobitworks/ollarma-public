"""Tests for `ollarma recover` CLI (phase 43, CLI-01/02, TEST-03)."""
from __future__ import annotations

import pathlib
import subprocess

import pytest
from typer.testing import CliRunner

from ollarma.cli import app


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


runner = CliRunner()


class TestRecoverScan:
    def test_scan_clean_repo_exits_zero(self, clean_repo: pathlib.Path) -> None:
        result = runner.invoke(
            app, ["recover", "scan", "--project-root", str(clean_repo)],
        )
        assert result.exit_code == 0, result.output
        assert "clean" in result.output.lower()

    def test_scan_writes_packet(self, clean_repo: pathlib.Path) -> None:
        result = runner.invoke(
            app, ["recover", "scan", "--project-root", str(clean_repo)],
        )
        assert result.exit_code == 0, result.output
        latest = clean_repo / ".ollarma" / "incidents" / "latest.json"
        assert latest.exists()

    def test_scan_json_output(self, clean_repo: pathlib.Path) -> None:
        result = runner.invoke(
            app, ["recover", "scan", "--project-root", str(clean_repo), "--json"],
        )
        assert result.exit_code == 0, result.output
        import orjson
        packet = orjson.loads(result.output)
        assert packet["state"] == "clean"
        assert packet["schema_version"] == 1

    def test_scan_nonzero_on_stranded(self, clean_repo: pathlib.Path) -> None:
        # Simulate a stranded sidecar worktree.
        sc = clean_repo / ".claude" / "worktrees" / "foo"
        sc.parent.mkdir(parents=True)
        _git(clean_repo, "worktree", "add", "-b", "claude/foo", str(sc))
        (sc / "scratch.py").write_text("print('wip')\n")

        result = runner.invoke(
            app, ["recover", "scan", "--project-root", str(clean_repo)],
        )
        assert result.exit_code == 1, result.output
        assert "stranded_worktree_detected" in result.output.lower() \
            or "stranded-worktree-detected" in result.output.lower() \
            or "stranded" in result.output.lower()


class TestRecoverReport:
    def test_report_without_prior_scan_errors(
        self, clean_repo: pathlib.Path,
    ) -> None:
        result = runner.invoke(
            app, ["recover", "report", "--project-root", str(clean_repo)],
        )
        assert result.exit_code == 2, result.output
        assert "no recovery packet" in result.output.lower()

    def test_report_after_scan(self, clean_repo: pathlib.Path) -> None:
        # Run scan first
        runner.invoke(
            app, ["recover", "scan", "--project-root", str(clean_repo)],
        )
        result = runner.invoke(
            app, ["recover", "report", "--project-root", str(clean_repo)],
        )
        assert result.exit_code == 0, result.output
        assert "clean" in result.output.lower()

    def test_report_json_output(self, clean_repo: pathlib.Path) -> None:
        runner.invoke(
            app, ["recover", "scan", "--project-root", str(clean_repo)],
        )
        result = runner.invoke(
            app,
            ["recover", "report", "--project-root", str(clean_repo), "--json"],
        )
        assert result.exit_code == 0, result.output
        import orjson
        packet = orjson.loads(result.output)
        assert packet["state"] == "clean"
