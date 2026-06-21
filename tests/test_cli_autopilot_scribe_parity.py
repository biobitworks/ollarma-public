"""CLI autopilot scribe parity (Phase 57.1-01, DEBT-56.2, closes F-01).

Verifies that ``ollarma autopilot`` CLI invocations produce the same
scribe-entry trail as HTTP ``POST /autopilot`` calls. Previously only the
HTTP surface wrote ``pre_dispatch`` / ``end_of_run`` entries via
``scribe_hooks``; cron-driven or headless CLI usage wrote nothing, breaking
the "chat-session independence" invariant the substrate audit (F-01) flagged
as a blocker.

Tests:
  A. CLI discovery mode writes a started + completed pair tagged ``cli:autopilot``.
  B. CLI failure path (unknown project) writes a started + blocked pair.
  C. CLI and HTTP both write exactly two scribe entries per invocation
     (parity at the entry-count level; entrypoint string differs by design).
"""
from __future__ import annotations

import pathlib
from unittest.mock import patch

import orjson
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from ollarma import scribe_hooks
from ollarma.autopilot import (
    AssetInventory,
    AutopilotReport,
    TierMapping,
)
from ollarma.cli import app
from ollarma.fleet import AdapterConfig


runner = CliRunner()


@pytest.fixture
def temp_cwd(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Run under tmp_path so scribe I/O doesn't touch the real repo."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _enable_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hooks on by default; strip any env override."""
    monkeypatch.delenv(scribe_hooks.HOOK_ENV_VAR, raising=False)


def _read_entries(cwd: pathlib.Path) -> list[dict]:
    log = cwd / ".ollarma" / "session-log.jsonl"
    if not log.exists():
        return []
    lines = log.read_bytes().splitlines()
    return [orjson.loads(ln) for ln in lines if ln.strip()]


def _make_adapter(
    name: str = "test-project",
    root: str = "/tmp/test-project",
) -> AdapterConfig:
    return AdapterConfig(
        project_name=name,
        project_root=root,
        project_type="python",
        tools=["gh", "git_cmd"],
        antibodies=["prompt_injection"],
        adapter_source="test",
    )


def _make_report(name: str = "test-project", root: str = "/tmp/test-project") -> AutopilotReport:
    return AutopilotReport(
        project_name=name,
        project_root=root,
        inventory=AssetInventory(
            project_name=name, project_root=root, assets=(), counts={},
        ),
        tier_map={
            "code": TierMapping(
                tier="code", model="qwen3:8b", model_size_b=8.0,
                quality_mean=0.85, degraded_confidence=False,
            ),
        },
        results=(),
        total_assets=0,
        passed=0,
        failed=0,
        escalation_needed=0,
        tokens_consumed_local=0,
        run_executed=False,
    )


# ---------------------------------------------------------------------------
# Test A: CLI discovery writes a started + completed pair
# ---------------------------------------------------------------------------


class TestCliAutopilotScribeDiscovery:
    @patch("ollarma.cli.run_autopilot")
    @patch("ollarma.service.resolve_project_adapter")
    @patch("ollarma.service.list_projects")
    def test_cli_autopilot_discovery_writes_scribe_pair(
        self,
        mock_list_projects,
        mock_resolve_adapter,
        mock_run,
        temp_cwd: pathlib.Path,
    ) -> None:
        adapter = _make_adapter()
        mock_list_projects.return_value = {"test-project": adapter}
        mock_resolve_adapter.return_value = adapter
        mock_run.return_value = _make_report()

        result = runner.invoke(app, ["autopilot", "test-project"])
        assert result.exit_code == 0

        entries = _read_entries(temp_cwd)
        assert len(entries) == 2, f"expected 2 scribe entries, got {entries}"
        assert entries[0]["state"] == "started"
        assert entries[1]["state"] == "completed"
        for entry in entries:
            assert entry["project"] == "test-project"
            assert entry["task"] == "autopilot:discover:test-project"
            assert "cli:autopilot" in entry["notes"]


# ---------------------------------------------------------------------------
# Test B: CLI failure path writes a started + blocked pair
# ---------------------------------------------------------------------------


class TestCliAutopilotScribeFailure:
    @patch("ollarma.service.resolve_project_adapter")
    @patch("ollarma.service.list_projects")
    def test_cli_autopilot_failure_writes_failed_end_entry(
        self,
        mock_list_projects,
        mock_resolve_adapter,
        temp_cwd: pathlib.Path,
    ) -> None:
        # Unknown project -> ValueError -> CLI prints error and typer.Exit(1).
        # typer.Exit is a SystemExit subclass, which `hooked` treats as an
        # exception path and writes a ``blocked`` end_of_run entry.
        mock_list_projects.return_value = {}
        mock_resolve_adapter.side_effect = ValueError("not found")

        result = runner.invoke(app, ["autopilot", "nonexistent"])
        assert result.exit_code == 1

        entries = _read_entries(temp_cwd)
        assert len(entries) == 2, f"expected 2 scribe entries, got {entries}"
        assert entries[0]["state"] == "started"
        assert entries[-1]["state"] == "blocked"
        assert entries[-1]["project"] == "nonexistent"
        assert entries[-1]["task"] == "autopilot:discover:nonexistent"
        assert "cli:autopilot" in entries[-1]["notes"]


# ---------------------------------------------------------------------------
# Test C: CLI vs HTTP entry-count parity
# ---------------------------------------------------------------------------


class TestCliHttpAutopilotScribeParity:
    def test_cli_and_http_autopilot_scribe_entry_count_parity(
        self,
        temp_cwd: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same project invoked via CLI and HTTP must each add +2 entries.

        The ``entrypoint`` tag differs (``cli:autopilot`` vs ``submit_autopilot``)
        but the pre + end entry-count contract is identical. This is the
        parity the F-01 audit asks us to guarantee.
        """
        # Silence the admission / recovery check for HTTP path.
        from ollarma import admission
        monkeypatch.setattr(admission, "check_recovery", lambda **_k: None)

        adapter = _make_adapter()

        # Patch once for both surfaces so the inner body is deterministic.
        monkeypatch.setattr(
            "ollarma.service.list_projects",
            lambda **_k: {"test-project": adapter},
        )
        monkeypatch.setattr(
            "ollarma.service.resolve_project_adapter",
            lambda *_a, **_k: adapter,
        )
        monkeypatch.setattr(
            "ollarma.cli.run_autopilot",
            lambda **_k: _make_report(),
        )
        # HTTP path calls service.submit_autopilot -> _submit_autopilot_body;
        # short-circuit the body so we don't need a real adapter on disk.
        monkeypatch.setattr(
            "ollarma.service._submit_autopilot_body",
            lambda **_k: _make_report(),
        )

        # --- CLI invocation: expect +2 ---
        baseline = len(_read_entries(temp_cwd))
        cli_result = runner.invoke(app, ["autopilot", "test-project"])
        assert cli_result.exit_code == 0
        after_cli = len(_read_entries(temp_cwd))
        cli_delta = after_cli - baseline

        # --- HTTP invocation: expect +2 ---
        from ollarma.http_api import app as http_app
        client = TestClient(http_app, raise_server_exceptions=False)
        response = client.post("/autopilot", json={"project": "test-project"})
        assert response.status_code == 200, response.text
        after_http = len(_read_entries(temp_cwd))
        http_delta = after_http - after_cli

        assert cli_delta == 2, f"CLI wrote {cli_delta} scribe entries (expected 2)"
        assert http_delta == 2, f"HTTP wrote {http_delta} scribe entries (expected 2)"
        assert cli_delta == http_delta, "CLI/HTTP scribe parity violated"

        # Sanity: entrypoint tags differ by design (grep-able per-surface).
        entries = _read_entries(temp_cwd)
        cli_notes = [e["notes"] for e in entries if "cli:autopilot" in e["notes"]]
        http_notes = [e["notes"] for e in entries if "submit_autopilot" in e["notes"]]
        assert len(cli_notes) == 2
        assert len(http_notes) == 2
