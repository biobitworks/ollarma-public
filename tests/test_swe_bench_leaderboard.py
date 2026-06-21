"""test_swe_bench_leaderboard.py -- Phase 62-01 LeaderboardArtifact +
verify_leaderboard_chain tests.

All tests use synthetic SWEBenchRun records; no provider calls, no filesystem
beyond tmp_path.
"""
from __future__ import annotations

import pathlib
from decimal import Decimal

import orjson

from ollarma.swe_bench import (
    LeaderboardArtifact,
    LocalLaneRunner,
    SWEBenchProblem,
    SWEBenchRun,
    build_leaderboard,
    run_dir,
    verify_leaderboard_chain,
    write_leaderboard,
)


def _local_run(instance_id: str, status: str) -> SWEBenchRun:
    return SWEBenchRun(
        instance_id=instance_id,
        lane="local",
        status=status,
        duration_s=0.1,
    )


def _frontier_run(
    instance_id: str, status: str, cost: str = "0",
) -> SWEBenchRun:
    return SWEBenchRun(
        instance_id=instance_id,
        lane="frontier",
        status=status,
        duration_s=0.1,
        cost_usd=Decimal(cost),
        frontier_receipts=[{"status": "succeeded" if status == "passed" else "failed"}],
    )


def test_build_leaderboard_computes_pass_at_1_per_lane():
    local_runs = [
        _local_run("p-1", "passed"),
        _local_run("p-2", "failed"),
        _local_run("p-3", "passed"),
    ]
    frontier_runs = [
        _frontier_run("p-1", "passed", "0.10"),
        _frontier_run("p-2", "passed", "0.05"),
        _frontier_run("p-3", "failed", "0.02"),
    ]
    artifact = build_leaderboard(
        local_runs, frontier_runs,
        subset_spec="first-3", run_id="test-run-01",
    )

    assert isinstance(artifact, LeaderboardArtifact)
    assert artifact.total_problems == 3
    assert artifact.per_lane_pass_at_1["local"] == "0.66667"
    assert artifact.per_lane_pass_at_1["frontier"] == "0.66667"
    # Per-problem breakdown enumerates both lanes
    ids = sorted(row["instance_id"] for row in artifact.per_problem)
    assert ids == ["p-1", "p-2", "p-3"]


def test_build_leaderboard_aggregates_frontier_cost():
    local_runs = [_local_run("p-1", "passed")]
    frontier_runs = [
        _frontier_run("p-1", "passed", "0.10"),
        _frontier_run("p-2", "failed", "0.05"),
    ]
    artifact = build_leaderboard(
        local_runs, frontier_runs,
        subset_spec="first-2", run_id="test-run-02",
    )

    assert Decimal(artifact.per_lane_cost_usd["frontier"]) == Decimal("0.15")
    assert Decimal(artifact.per_lane_cost_usd["local"]) == Decimal("0")


def test_verify_leaderboard_chain_passes_for_valid_streams(tmp_path):
    """Write a real LocalLaneRunner receipt stream + verify it cleanly."""
    def _dispatch(_p, _project):
        return ({"receipts": []}, None)

    runner = LocalLaneRunner(
        repo_root=tmp_path,
        autopilot_dispatch=_dispatch,
        work_dir=tmp_path,
        run_id="verify-run-01",
    )
    for i in range(2):
        runner.run(
            SWEBenchProblem(
                instance_id=f"verify-{i}", repo="r/r", base_commit="a",
                problem_statement="s", test_patch="",
                test_cmd="python3 -c 'assert 1 == 1'",
            ),
            project="demo",
            timeout_s=5,
        )

    artifact = build_leaderboard(
        local_runs=[], frontier_runs=[],
        subset_spec="first-2", run_id="verify-run-01",
    )
    write_leaderboard(artifact, tmp_path)

    result = verify_leaderboard_chain("verify-run-01", tmp_path)
    assert result.passed is True
    assert result.first_break_detail is None
    # Local row checked 2 lines, frontier shares dir so same head.
    local_row = next(r for r in result.rows if r.stream == "local")
    assert local_row.lines_checked == 2
    assert local_row.status == "clean"


def test_verify_leaderboard_chain_detects_hash_break(tmp_path):
    """Tamper with a receipts.jsonl line; verify should mark the chain broken."""
    def _dispatch(_p, _project):
        return ({"receipts": []}, None)

    runner = LocalLaneRunner(
        repo_root=tmp_path,
        autopilot_dispatch=_dispatch,
        work_dir=tmp_path,
        run_id="tamper-run-01",
    )
    for i in range(2):
        runner.run(
            SWEBenchProblem(
                instance_id=f"tamper-{i}", repo="r/r", base_commit="a",
                problem_statement="s", test_patch="",
                test_cmd="python3 -c 'assert 1 == 1'",
            ),
            project="demo",
            timeout_s=5,
        )

    # Corrupt the second line's body so row_hash no longer matches.
    receipts_path = run_dir(tmp_path, "tamper-run-01") / "receipts.jsonl"
    lines = receipts_path.read_bytes().splitlines()
    assert len(lines) == 2
    line1 = orjson.loads(lines[1])
    line1["body"]["excerpt"] = "TAMPERED"
    lines[1] = orjson.dumps(line1)
    receipts_path.write_bytes(b"\n".join(lines) + b"\n")

    result = verify_leaderboard_chain("tamper-run-01", tmp_path)
    assert result.passed is False
    assert result.first_break_detail is not None
    assert "row_hash mismatch" in result.first_break_detail
