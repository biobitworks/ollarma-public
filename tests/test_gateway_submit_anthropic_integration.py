"""test_gateway_submit_anthropic_integration.py -- Phase 59-01 end-to-end tests.

Exercises the full `/gateway/submit` POST chain through the real Starlette
TestClient, real service.submit_gateway_request, real GatewayClient,
real AnthropicProvider, with the ``httpx`` transport mocked via
``httpx.MockTransport`` (no ``GatewayClient.submit`` mocking -- the plan's
anti-pattern rule).

The ``AnthropicProvider`` pulls its ``http_client_factory`` from the default
constructor; tests inject the factory via monkeypatching the module-level
``PROVIDER_REGISTRY`` entry so the service-layer dispatch receives a fake-
transport adapter.
"""
from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import httpx
import pytest
from starlette.testclient import TestClient

from ollarma import gateway_admission, http_api
from ollarma.escalation import ReasonCode, build_escalation_receipt
from ollarma.gateway import GatewayReceiptStore


def _valid_er_dict(project: str = "demo") -> dict:
    er = build_escalation_receipt(
        project=project,
        lane="local",
        task_class="code",
        reason_code=ReasonCode.SELECTION_MISSING,
        reason_detail="integration test",
        next_action="frontier_or_human",
    )
    return json.loads(er.model_dump_json())


# ---------------------------------------------------------------------------
# Fixtures: isolated repo + gateway config + mocked keychain + mocked anthropic
# ---------------------------------------------------------------------------


_ANTHROPIC_SUCCESS_BODY = {
    "id": "msg_01INTEGRATION",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-4-5-latest",
    "content": [{"type": "text", "text": "integration response"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 5},
}

_ANTHROPIC_AUTH_ERROR_BODY = {
    "type": "error",
    "error": {"type": "authentication_error", "message": "invalid key"},
}


@pytest.fixture
def _enabled_repo(tmp_path: pathlib.Path, monkeypatch):
    """Build a repo with gateway enabled + allowlist + vk + keychain mock."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".planning").mkdir()
    (repo / ".planning" / "config.json").write_text(
        json.dumps(
            {
                "features": {
                    "gateway": {
                        "enabled": True,
                        "allowlist": ["demo"],
                        "virtual_keys": [
                            {
                                "id": "vk_demo",
                                "keychain_service": "ollarma-anthropic-test",
                                "provider": "anthropic",
                            },
                        ],
                    },
                },
            }
        )
    )
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def _keychain_fake(monkeypatch):
    monkeypatch.setattr(
        gateway_admission,
        "_KEYCHAIN_LOOKUP",
        lambda _service: b"fake-sk-ant-INTEGRATIONKEY",
    )


def _install_anthropic_mock(
    monkeypatch, *, status_code: int, body: dict,
):
    """Swap PROVIDER_REGISTRY["anthropic"] for a class whose httpx client
    returns the mocked status_code + body.
    """
    from ollarma.providers import PROVIDER_REGISTRY  # noqa: PLC0415
    from ollarma.providers.anthropic import AnthropicProvider  # noqa: PLC0415

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status_code, json=body, request=request)

    def factory(*, timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    class MockedAnthropicProvider(AnthropicProvider):
        def __init__(self):
            super().__init__(http_client_factory=factory)

    # Patch the registry dict in place so service-layer ``get_provider`` sees it.
    monkeypatch.setitem(PROVIDER_REGISTRY, "anthropic", MockedAnthropicProvider)


# ---------------------------------------------------------------------------
# Plan tests 1-4
# ---------------------------------------------------------------------------


def test_gateway_submit_anthropic_happy_chain_end_to_end(
    _enabled_repo: pathlib.Path, _keychain_fake, monkeypatch,
):
    """Admission approved + mocked 200 -> FrontierReceipt with provider populated."""
    _install_anthropic_mock(
        monkeypatch, status_code=200, body=_ANTHROPIC_SUCCESS_BODY,
    )
    client = TestClient(http_api.app)
    response = client.post(
        "/gateway/submit",
        json={
            "escalation_receipt": _valid_er_dict(),
            "virtual_key_id": "vk_demo",
            "prompt": "Please count to three.",
            "model": "claude-haiku-4-5-latest",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["provider"] == "anthropic"
    assert body["model_id"] == "claude-haiku-4-5-latest"
    assert body["prompt_tokens"] == 10
    assert body["response_tokens"] == 5
    assert Decimal(body["cost_usd"]) > Decimal("0")
    assert body["latency_ms"] >= 0
    assert body["provider_request_id"] == "msg_01INTEGRATION"
    assert body["reason_code"] is None

    # Admission + receipt both landed on the hash chain.
    store = GatewayReceiptStore(_enabled_repo)
    admissions = store.load_admissions()
    receipts = store.load_receipts()
    assert len(admissions) == 1
    assert admissions[0].outcome == "accept"
    assert len(receipts) == 1
    assert receipts[0].status == "succeeded"
    assert store.verify_chain("admissions")
    assert store.verify_chain("receipts")


def test_gateway_submit_anthropic_key_missing_returns_kc_miss_reject(
    _enabled_repo: pathlib.Path, monkeypatch,
):
    """Keychain returns None -> admission rejects with VIRTUAL_KEY_KEYCHAIN_MISS."""
    monkeypatch.setattr(gateway_admission, "_KEYCHAIN_LOOKUP", lambda _s: None)
    # Provider mock installed but should never fire.
    _install_anthropic_mock(
        monkeypatch, status_code=200, body=_ANTHROPIC_SUCCESS_BODY,
    )
    client = TestClient(http_api.app)
    response = client.post(
        "/gateway/submit",
        json={
            "escalation_receipt": _valid_er_dict(),
            "virtual_key_id": "vk_demo",
            "prompt": "test",
            "model": "claude-haiku-4-5-latest",
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "failed"
    assert body["reason_code"] == "VIRTUAL_KEY_KEYCHAIN_MISS"


def test_gateway_submit_anthropic_provider_auth_error_returns_failed_receipt(
    _enabled_repo: pathlib.Path, _keychain_fake, monkeypatch,
):
    """Anthropic 401 -> HTTP 200 (receipt is a valid delivery of a failure)
    with status=failed + reason_code=PROVIDER_AUTH_FAILED."""
    _install_anthropic_mock(
        monkeypatch, status_code=401, body=_ANTHROPIC_AUTH_ERROR_BODY,
    )
    client = TestClient(http_api.app)
    response = client.post(
        "/gateway/submit",
        json={
            "escalation_receipt": _valid_er_dict(),
            "virtual_key_id": "vk_demo",
            "prompt": "test",
            "model": "claude-haiku-4-5-latest",
        },
    )
    # HTTP 200 -- the gateway successfully delivered a structured failure.
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["reason_code"] == "PROVIDER_AUTH_FAILED"
    assert body["provider"] == "anthropic"
    assert Decimal(body["cost_usd"]) == Decimal("0")

    store = GatewayReceiptStore(_enabled_repo)
    receipts = store.load_receipts()
    assert len(receipts) == 1
    assert receipts[0].status == "failed"
    assert receipts[0].reason_code == "PROVIDER_AUTH_FAILED"


def test_gateway_submit_anthropic_key_never_in_logs_or_receipt(
    _enabled_repo: pathlib.Path, monkeypatch,
):
    """I-06 invariant: the raw Keychain bytes never appear in the on-disk
    FrontierReceipt, GatewayAdmissionEntry, or rate_state.json.
    """
    secret = b"fake-sk-ant-GREP-INVARIANT-CANARY-ZZZZZZZZ"
    monkeypatch.setattr(
        gateway_admission, "_KEYCHAIN_LOOKUP", lambda _s: secret,
    )
    _install_anthropic_mock(
        monkeypatch, status_code=200, body=_ANTHROPIC_SUCCESS_BODY,
    )
    client = TestClient(http_api.app)
    response = client.post(
        "/gateway/submit",
        json={
            "escalation_receipt": _valid_er_dict(),
            "virtual_key_id": "vk_demo",
            "prompt": "test",
            "model": "claude-haiku-4-5-latest",
        },
    )
    assert response.status_code == 200

    # Grep every file under .ollarma/gateway for the raw bytes.
    gateway_dir = _enabled_repo / ".ollarma" / "gateway"
    assert gateway_dir.exists()
    for path in gateway_dir.rglob("*"):
        if not path.is_file():
            continue
        blob = path.read_bytes()
        assert secret not in blob, (
            f"raw key bytes leaked into {path.relative_to(_enabled_repo)}"
        )
        # Also check the decoded form (some writers may utf-8 serialize).
        assert secret.decode("utf-8") not in blob.decode(
            "utf-8", errors="ignore",
        ), f"decoded key leaked into {path.relative_to(_enabled_repo)}"

    # Belt-and-suspenders: response body must not contain the key either.
    assert secret not in response.content
    assert secret.decode("utf-8") not in response.text
