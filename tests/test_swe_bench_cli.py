"""test_swe_bench_cli.py -- structural end-to-end of fetch + run + status.

Uses the minimal fixture + a mocked autopilot dispatch. No network, no Ollama.
"""
from __future__ import annotations

import pathlib

from typer.testing import CliRunner

from ollarma import cli, swe_bench


FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "swe_bench" / "minimal_subset.jsonl"


def test_swe_bench_cli_fetch_then_run_then_status(tmp_path, monkeypatch):
    payload = FIXTURE_PATH.read_bytes()
    monkeypatch.setattr(swe_bench, "_download_bytes", lambda *_a, **_kw: payload)

    # Route the CLI's dispatch thunk through a mock (NOT the live autopilot).
    def _mock_dispatch(_problem, _project):
        return ({"receipts": []}, None)

    monkeypatch.setattr(cli, "_cli_autopilot_dispatch", _mock_dispatch)

    runner = CliRunner()

    # fetch
    result = runner.invoke(
        cli.app, ["swe-bench", "fetch", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "fetched" in result.output

    # run (only first 2 problems: skip the sleep-forever fixture to keep
    # the structural test fast; use a short timeout so the fail case exits
    # quickly too).
    result = runner.invoke(
        cli.app,
        [
            "swe-bench", "run",
            "--lane", "local",
            "--subset", "first-2",
            "--project", "demo",
            "--repo-root", str(tmp_path),
            "--timeout-s", "5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "synthetic-pass-1" in result.output
    assert "synthetic-fail-2" in result.output

    # status should now report a cached dataset and at least one run.
    result = runner.invoke(
        cli.app, ["swe-bench", "status", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "cache:" in result.output
    assert "runs:" in result.output
    # latest run dir should exist and contain a receipts.jsonl file.
    runs_dir = tmp_path / ".ollarma" / "benchmarks" / "swe-bench-lite" / "runs"
    assert runs_dir.is_dir()
    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) >= 1
    assert (run_dirs[0] / "receipts.jsonl").exists()
