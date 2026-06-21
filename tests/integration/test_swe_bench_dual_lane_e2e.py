"""test_swe_bench_dual_lane_e2e.py -- Phase 62-01 SWE-07 end-to-end.

Runs 2 synthetic problems through both lanes (local autopilot mocked;
Anthropic provider mocked via httpx.MockTransport), builds the leaderboard,
verifies the chain, and reconstructs a frontier run's escalation via
``receipts_trace.trace``.
"""
from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from ollarma import gateway_admission, receipts_trace
from ollarma.gateway_admission import AdmissionPolicy
from ollarma.gateway_client import GatewayClient
from ollarma.swe_bench import (
    FrontierLaneRunner,
    LocalLaneRunner,
    SWEBenchProblem,
    build_leaderboard,
    verify_leaderboard_chain,
    write_leaderboard,
)


_SUCCESS_BODY = {
    "id": "msg_01DUAL",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-4-5-latest",
    "content": [{"type": "text", "text": "patch would go here"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 8, "output_tokens": 4},
}


def _install_mock_provider(monkeypatch) -> None:
    from ollarma.providers import PROVIDER_REGISTRY
    from ollarma.providers.anthropic import AnthropicProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json=_SUCCESS_BODY, request=request)

    def factory(*, timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    class Mocked(AnthropicProvider):
        def __init__(self):
            super().__init__(http_client_factory=factory)

    monkeypatch.setitem(PROVIDER_REGISTRY, "anthropic", Mocked)


def _mk_problems() -> list[SWEBenchProblem]:
    return [
        SWEBenchProblem(
            instance_id="dual-synth-1",
            repo="ex/r", base_commit="a" * 40,
            problem_statement="Return 1.",
            test_patch="",
            test_cmd="python3 -c 'assert 1 == 1'",
        ),
        SWEBenchProblem(
            instance_id="dual-synth-2",
            repo="ex/r", base_commit="b" * 40,
            problem_statement="Return 2.",
            test_patch="",
            test_cmd="python3 -c 'assert 1 == 1'",
        ),
    ]


def _setup_gateway(tmp_path, monkeypatch) -> AdmissionPolicy:
    monkeypatch.setattr(
        gateway_admission,
        "_KEYCHAIN_LOOKUP",
        lambda _s: b"fake-sk-ant-dual",
    )
    return AdmissionPolicy({
        "enabled": True,
        "allowlist": ["demo"],
        "virtual_keys": [
            {
                "id": "vk_demo",
                "keychain_service": "ollarma-anthropic-test",
                "provider": "anthropic",
            },
        ],
    })


def test_dual_lane_e2e_produces_leaderboard_and_verifies(tmp_path, monkeypatch):
    """Run 2 problems x 2 lanes; build leaderboard; verify chains."""
    policy = _setup_gateway(tmp_path, monkeypatch)
    _install_mock_provider(monkeypatch)

    problems = _mk_problems()

    # Local lane -- mocked autopilot, ``test_cmd`` executes cleanly so status=passed
    def _dispatch(_p, _project):
        return ({"receipts": []}, None)

    local_runner = LocalLaneRunner(
        repo_root=tmp_path,
        autopilot_dispatch=_dispatch,
        work_dir=tmp_path,
        run_id="dual-local-run",
    )
    local_runs = [
        local_runner.run(p, project="demo", timeout_s=5) for p in problems
    ]
    assert all(r.status == "passed" for r in local_runs)

    # Frontier lane -- mocked provider returns success for all problems
    client = GatewayClient(tmp_path)
    frontier_runner = FrontierLaneRunner(
        repo_root=tmp_path,
        gateway_client=client,
        admission_policy=policy,
        run_id="dual-frontier-run",
        virtual_key_bytes=b"fake-sk-ant-dual",
    )
    frontier_runs = [
        frontier_runner.run(
            p, project="demo",
            virtual_key_id="vk_demo",
            model="claude-haiku-4-5-latest",
        )
        for p in problems
    ]
    assert all(r.status == "passed" for r in frontier_runs)
    assert all(r.escalation_receipt_id is not None for r in frontier_runs)

    # Build leaderboard + persist.
    artifact = build_leaderboard(
        local_runs, frontier_runs,
        subset_spec="first-2",
        run_id="dual-local-run",
    )
    assert artifact.per_lane_pass_at_1["local"] == "1.00000"
    assert artifact.per_lane_pass_at_1["frontier"] == "1.00000"
    assert artifact.total_problems == 2

    json_path, md_path = write_leaderboard(artifact, tmp_path)
    assert json_path.exists() and md_path.exists()
    loaded = json.loads(json_path.read_bytes())
    assert loaded["per_lane_pass_at_1"]["frontier"] == "1.00000"

    # Verify both streams -- walks local per-run stream + gateway streams.
    result = verify_leaderboard_chain(
        "dual-local-run", tmp_path,
        frontier_run_id="dual-frontier-run",
    )
    assert result.passed is True, result.first_break_detail
    # Must have exercised both lanes + both gateway streams.
    streams = {row.stream for row in result.rows}
    assert "local" in streams
    assert "frontier" in streams
    assert "gateway:admissions" in streams
    assert "gateway:receipts" in streams


def test_dual_lane_e2e_receipts_reconstructable_via_trace_cli(tmp_path, monkeypatch):
    """Extract a frontier escalation_receipt_id and walk the gateway chain
    with receipts_trace.trace -- chain intact (EXTERNAL_ESCALATION is OK)."""
    policy = _setup_gateway(tmp_path, monkeypatch)
    _install_mock_provider(monkeypatch)

    problems = _mk_problems()

    client = GatewayClient(tmp_path)
    runner = FrontierLaneRunner(
        repo_root=tmp_path,
        gateway_client=client,
        admission_policy=policy,
        run_id="trace-frontier-run",
        virtual_key_bytes=b"fake-sk-ant-dual",
    )
    records = [
        runner.run(
            p, project="demo",
            virtual_key_id="vk_demo",
            model="claude-haiku-4-5-latest",
        )
        for p in problems
    ]
    er_id = records[0].escalation_receipt_id
    assert er_id is not None

    result = receipts_trace.trace(er_id, tmp_path)
    assert result.chain_intact is True, [r.linkage_detail for r in result.rows]
    assert result.exit_code == 0

    stages = [r.stage for r in result.rows]
    assert stages == ["escalation", "admission", "frontier"]
    # Admission and frontier must MATCH (admission_receipt_hash linkage).
    frontier_row = next(r for r in result.rows if r.stage == "frontier")
    assert frontier_row.linkage_status == "MATCH"
