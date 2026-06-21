"""test_swe_bench_frontier_runner.py -- Phase 62-01 FrontierLaneRunner tests.

All tests mock the Anthropic provider via ``http_client_factory`` +
``httpx.MockTransport``. No live HTTP.
"""
from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import httpx
import pytest

from ollarma import gateway_admission
from ollarma.gateway_admission import AdmissionPolicy
from ollarma.gateway_client import GatewayClient
from ollarma.swe_bench import FrontierLaneRunner, SWEBenchProblem


_SUCCESS_BODY = {
    "id": "msg_01FRONTIER",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-4-5-latest",
    "content": [{"type": "text", "text": "```patch\n+ return 1\n```"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 12, "output_tokens": 7},
}

_AUTH_ERROR_BODY = {
    "type": "error",
    "error": {"type": "authentication_error", "message": "invalid key"},
}

_RATE_LIMIT_BODY = {
    "type": "error",
    "error": {"type": "rate_limit_error", "message": "slow down"},
}


def _mk_problem(instance_id: str = "synthetic-frontier-1") -> SWEBenchProblem:
    return SWEBenchProblem(
        instance_id=instance_id,
        repo="example/repo",
        base_commit="0" * 40,
        problem_statement="Make the function return 1.",
        test_patch="",
        test_cmd="python3 -c 'assert 1 == 1'",
    )


def _policy_and_vk(monkeypatch) -> AdmissionPolicy:
    monkeypatch.setattr(
        gateway_admission,
        "_KEYCHAIN_LOOKUP",
        lambda _s: b"fake-sk-ant-test",
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


def _install_mock_provider(monkeypatch, *, status_code: int, body: dict) -> None:
    from ollarma.providers import PROVIDER_REGISTRY
    from ollarma.providers.anthropic import AnthropicProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status_code, json=body, request=request)

    def factory(*, timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    class Mocked(AnthropicProvider):
        def __init__(self):
            super().__init__(http_client_factory=factory)

    monkeypatch.setitem(PROVIDER_REGISTRY, "anthropic", Mocked)


def test_frontier_runner_happy_path_mocked_provider(tmp_path, monkeypatch):
    """Happy path: provider 200 -> SWEBenchRun(lane=frontier, status=passed,
    cost_usd > 0, frontier_receipts=[...])."""
    policy = _policy_and_vk(monkeypatch)
    _install_mock_provider(monkeypatch, status_code=200, body=_SUCCESS_BODY)

    client = GatewayClient(tmp_path)
    runner = FrontierLaneRunner(
        repo_root=tmp_path,
        gateway_client=client,
        admission_policy=policy,
        virtual_key_bytes=b"fake-sk-ant-test",
    )
    record = runner.run(
        _mk_problem(),
        project="demo",
        virtual_key_id="vk_demo",
        model="claude-haiku-4-5-latest",
    )

    assert record.lane == "frontier"
    assert record.status == "passed"
    assert record.reason_code is None
    assert len(record.frontier_receipts) == 1
    assert record.frontier_receipts[0]["status"] == "succeeded"
    assert record.frontier_receipts[0]["prompt_tokens"] == 12
    assert record.cost_usd > Decimal("0")
    assert record.escalation_receipt_id is not None

    # Per-run receipts.jsonl landed.
    lines = [
        json.loads(line)
        for line in runner.receipts_path.read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["body"]["status"] == "passed"
    assert lines[0]["header"]["parent_hash"] == "0" * 64


def test_frontier_runner_provider_auth_failure_records_failed(tmp_path, monkeypatch):
    """Provider returns 401 -> SWEBenchRun(status=failed,
    reason_code=PROVIDER_AUTH_FAILED). No silent local fallback."""
    policy = _policy_and_vk(monkeypatch)
    _install_mock_provider(monkeypatch, status_code=401, body=_AUTH_ERROR_BODY)

    client = GatewayClient(tmp_path)
    runner = FrontierLaneRunner(
        repo_root=tmp_path,
        gateway_client=client,
        admission_policy=policy,
        virtual_key_bytes=b"fake-sk-ant-test",
    )
    record = runner.run(
        _mk_problem("synth-auth-fail"),
        project="demo",
        virtual_key_id="vk_demo",
        model="claude-haiku-4-5-latest",
    )

    assert record.lane == "frontier"
    assert record.status == "failed"
    assert record.reason_code == "PROVIDER_AUTH_FAILED"
    # A frontier receipt is still recorded -- the gateway delivered a
    # structured failure, not a silent drop.
    assert len(record.frontier_receipts) == 1
    assert record.frontier_receipts[0]["status"] == "failed"
    assert record.cost_usd == Decimal("0")


def test_frontier_runner_provider_rate_limited_records_failed(tmp_path, monkeypatch):
    """Provider returns 429 -> SWEBenchRun(status=failed,
    reason_code=PROVIDER_RATE_LIMITED)."""
    policy = _policy_and_vk(monkeypatch)
    _install_mock_provider(monkeypatch, status_code=429, body=_RATE_LIMIT_BODY)

    client = GatewayClient(tmp_path)
    runner = FrontierLaneRunner(
        repo_root=tmp_path,
        gateway_client=client,
        admission_policy=policy,
        virtual_key_bytes=b"fake-sk-ant-test",
    )
    record = runner.run(
        _mk_problem("synth-rate-limit"),
        project="demo",
        virtual_key_id="vk_demo",
        model="claude-haiku-4-5-latest",
    )

    assert record.status == "failed"
    assert record.reason_code == "PROVIDER_RATE_LIMITED"
    assert record.cost_usd == Decimal("0")


def test_frontier_runner_no_silent_local_fallback(tmp_path, monkeypatch):
    """Invariant I-02: provider failure does NOT trigger a local-lane retry.

    The FrontierLaneRunner has no autopilot_dispatch hook; if it had a silent
    fallback we'd observe a second receipt in the per-run stream, plus a
    local-lane SWEBenchRun. This test asserts neither happens.
    """
    policy = _policy_and_vk(monkeypatch)
    _install_mock_provider(monkeypatch, status_code=401, body=_AUTH_ERROR_BODY)

    client = GatewayClient(tmp_path)
    runner = FrontierLaneRunner(
        repo_root=tmp_path,
        gateway_client=client,
        admission_policy=policy,
        virtual_key_bytes=b"fake-sk-ant-test",
    )
    record = runner.run(
        _mk_problem(),
        project="demo",
        virtual_key_id="vk_demo",
        model="claude-haiku-4-5-latest",
    )

    assert record.lane == "frontier"
    assert record.status == "failed"

    # Exactly one receipt: the frontier dispatch record. No retry.
    lines = [
        json.loads(line)
        for line in runner.receipts_path.read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["body"]["lane"] == "frontier"
    # The gateway stream also holds exactly one FrontierReceipt (failed).
    from ollarma.gateway import GatewayReceiptStore
    store = GatewayReceiptStore(tmp_path)
    assert len(store.load_receipts()) == 1
