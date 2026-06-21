"""test_swe_bench_local_runner.py -- SWE-02 LocalLaneRunner + SWAP_DEGRADED.

All tests mock the autopilot dispatch; no live Ollama calls.
"""
from __future__ import annotations

import pathlib

import orjson

from ollarma.escalation import ReasonCode, build_escalation_receipt
from ollarma.swe_bench import LocalLaneRunner, SWEBenchProblem


def _mk(id_: str, test_cmd: str) -> SWEBenchProblem:
    return SWEBenchProblem(
        instance_id=id_, repo="ex/r", base_commit="abc",
        problem_statement="s", test_patch="", test_cmd=test_cmd,
    )


def _read_receipts(path: pathlib.Path) -> list[dict]:
    lines = path.read_bytes().splitlines()
    return [orjson.loads(line) for line in lines if line.strip()]


def test_local_runner_records_passed_when_autopilot_succeeds(tmp_path):
    problem = _mk("pass-1", 'python3 -c "assert 1 == 1"')

    def _dispatch(_p, _project):
        return ({"receipts": []}, None)

    runner = LocalLaneRunner(
        repo_root=tmp_path,
        autopilot_dispatch=_dispatch,
        work_dir=tmp_path,
    )
    record = runner.run(problem, project="demo", timeout_s=10)

    assert record.status == "passed"
    assert record.reason_code is None
    assert record.lane == "local"

    receipts = _read_receipts(runner.receipts_path)
    assert len(receipts) == 1
    assert receipts[0]["body"]["status"] == "passed"
    # Hash-chain: first receipt parent_hash is genesis.
    assert receipts[0]["header"]["parent_hash"] == "0" * 64
    assert receipts[0]["header"]["sequence"] == 0


def test_local_runner_records_skipped_swap_on_escalation_receipt(tmp_path):
    problem = _mk("pass-1", 'python3 -c "assert 1 == 1"')

    escalation = build_escalation_receipt(
        project="demo",
        lane="local",
        task_class="swe-bench",
        reason_code=ReasonCode.SWAP_DEGRADED,
        reason_detail="swap pressure above threshold; no local capacity",
        resource_snapshot={"swap_used_gb": 7.2},
        next_action="frontier_or_human",
    )

    def _dispatch(_p, _project):
        return (None, escalation)

    # Would-fail test_cmd. The runner MUST NOT execute it under SWAP_DEGRADED.
    problem_fail = _mk("fail-1", "false")

    def _dispatch2(_p, _project):
        return (None, escalation)

    runner = LocalLaneRunner(
        repo_root=tmp_path,
        autopilot_dispatch=_dispatch2,
        work_dir=tmp_path,
    )
    record = runner.run(problem_fail, project="demo", timeout_s=5)

    assert record.status == "skipped_swap"
    assert record.reason_code == "SWAP_DEGRADED"
    assert len(record.routing_receipts) == 1
    assert record.routing_receipts[0]["reason_code"] == "SWAP_DEGRADED"
    assert record.resource_snapshot.get("swap_used_gb") == 7.2

    # No silent fallback: no frontier marker anywhere in the receipt stream.
    receipts = _read_receipts(runner.receipts_path)
    assert len(receipts) == 1
    assert receipts[0]["body"]["status"] == "skipped_swap"
    assert receipts[0]["body"]["reason_code"] == "SWAP_DEGRADED"


def test_local_runner_respects_timeout(tmp_path):
    problem = _mk("timeout-1", "python3 -c 'import time; time.sleep(10)'")

    def _dispatch(_p, _project):
        return ({"receipts": []}, None)

    runner = LocalLaneRunner(
        repo_root=tmp_path,
        autopilot_dispatch=_dispatch,
        work_dir=tmp_path,
    )
    # 2-second timeout; the sandbox must interrupt a sleep(10).
    record = runner.run(problem, project="demo", timeout_s=2)

    assert record.status == "skipped_timeout"
    assert record.reason_code == "SANDBOX_TIMEOUT"
    assert record.duration_s < 10.0  # killed by timeout, not allowed to complete
