"""test_provider_anthropic.py -- Phase 59-01 Anthropic adapter unit tests.

All tests mock at the ``httpx`` transport boundary using ``httpx.MockTransport``
(no external mocking library required). The ``AnthropicProvider``'s
``http_client_factory`` seam is the single injection point.
"""
from __future__ import annotations

import json
import pathlib
import warnings
from decimal import Decimal

import httpx
import pytest

from ollarma.gateway import GatewayReasonCode
from ollarma.providers.anthropic import (
    AnthropicProvider,
    _ANTHROPIC_PRICING,
    _ANTHROPIC_CONTEXT_CAPS,
)
from ollarma.providers.base import ProviderResponse


_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "anthropic"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _factory_returning(
    *, status_code: int, body: dict, raise_exc: Exception | None = None,
):
    """Build an ``http_client_factory`` that returns an ``httpx.Client`` whose
    transport returns the configured response (or raises on send).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(
            status_code=status_code,
            json=body,
            request=request,
        )

    def factory(*, timeout: float) -> httpx.Client:
        transport = httpx.MockTransport(handler)
        return httpx.Client(transport=transport, timeout=timeout)

    return factory


# ---------------------------------------------------------------------------
# Plan tests 1-8
# ---------------------------------------------------------------------------


def test_anthropic_submit_success_populates_provider_response():
    """A 200 response with valid usage populates every ProviderResponse field."""
    fixture = _load_fixture("success_haiku.json")
    factory = _factory_returning(status_code=200, body=fixture)
    provider = AnthropicProvider(http_client_factory=factory)
    resp = provider.submit(
        prompt="hello",
        model="claude-haiku-4-5-latest",
        virtual_key_bytes=b"fake-sk-ant-AAAA",
    )
    assert isinstance(resp, ProviderResponse)
    assert resp.status == "succeeded"
    assert resp.model_id == "claude-haiku-4-5-latest"
    assert resp.prompt_tokens == 12
    assert resp.response_tokens == 8
    assert resp.cost_usd > Decimal("0")
    assert resp.latency_ms >= 0  # perf_counter can be nearly-zero on fast paths
    assert resp.provider_request_id == "msg_01ABCDEF123456"
    assert resp.reason_code is None
    assert resp.raw_error is None
    assert resp.response_body["id"] == "msg_01ABCDEF123456"


def test_anthropic_truncation_when_prompt_exceeds_cap():
    """Prompt estimated > cap -> truncation_event populated; call proceeds."""
    fixture = _load_fixture("success_haiku.json")
    factory = _factory_returning(status_code=200, body=fixture)
    provider = AnthropicProvider(http_client_factory=factory)

    model = "claude-haiku-4-5-latest"
    cap = _ANTHROPIC_CONTEXT_CAPS[model]
    # 5 chars/token gives us a prompt clearly over the cap.
    overlong = "A" * ((cap + 1000) * 5)

    resp = provider.submit(
        prompt=overlong, model=model, virtual_key_bytes=b"fake-sk-ant-AAAA",
    )
    assert resp.status == "succeeded"
    assert resp.truncation_event is not None
    assert resp.truncation_event["original_estimated_tokens"] > cap
    assert resp.truncation_event["truncated_estimated_tokens"] < cap


def test_anthropic_cost_usd_computed_as_decimal():
    """cost_usd is always Decimal; value matches price * tokens / 1M formula."""
    fixture = _load_fixture("success_haiku.json")
    factory = _factory_returning(status_code=200, body=fixture)
    provider = AnthropicProvider(http_client_factory=factory)
    resp = provider.submit(
        prompt="hello",
        model="claude-haiku-4-5-latest",
        virtual_key_bytes=b"fake-sk-ant-AAAA",
    )
    assert isinstance(resp.cost_usd, Decimal)
    input_rate, output_rate = _ANTHROPIC_PRICING["claude-haiku-4-5-latest"]
    million = Decimal("1000000")
    expected = (
        (Decimal(12) / million) * input_rate
        + (Decimal(8) / million) * output_rate
    )
    assert resp.cost_usd == expected


def test_anthropic_auth_error_maps_to_provider_auth_failed():
    """HTTP 401 -> status=failed with reason_code=PROVIDER_AUTH_FAILED."""
    fixture = _load_fixture("auth_error.json")
    factory = _factory_returning(status_code=401, body=fixture)
    provider = AnthropicProvider(http_client_factory=factory)
    resp = provider.submit(
        prompt="hello",
        model="claude-haiku-4-5-latest",
        virtual_key_bytes=b"fake-sk-ant-BAD",
    )
    assert resp.status == "failed"
    assert resp.reason_code == GatewayReasonCode.PROVIDER_AUTH_FAILED
    assert resp.raw_error is not None
    assert "401" in resp.raw_error
    assert resp.cost_usd == Decimal("0")
    assert resp.prompt_tokens == 0


def test_anthropic_rate_limit_maps_to_provider_rate_limited():
    """HTTP 429 -> status=failed with reason_code=PROVIDER_RATE_LIMITED."""
    fixture = _load_fixture("rate_limit.json")
    factory = _factory_returning(status_code=429, body=fixture)
    provider = AnthropicProvider(http_client_factory=factory)
    resp = provider.submit(
        prompt="hello",
        model="claude-haiku-4-5-latest",
        virtual_key_bytes=b"fake-sk-ant-AAAA",
    )
    assert resp.status == "failed"
    assert resp.reason_code == GatewayReasonCode.PROVIDER_RATE_LIMITED
    assert "429" in resp.raw_error


def test_anthropic_timeout_maps_to_provider_network_timeout():
    """httpx.TimeoutException -> status=failed with PROVIDER_NETWORK_TIMEOUT."""
    factory = _factory_returning(
        status_code=200,
        body={},
        raise_exc=httpx.ReadTimeout("simulated read timeout"),
    )
    provider = AnthropicProvider(http_client_factory=factory)
    resp = provider.submit(
        prompt="hello",
        model="claude-haiku-4-5-latest",
        virtual_key_bytes=b"fake-sk-ant-AAAA",
    )
    assert resp.status == "failed"
    assert resp.reason_code == GatewayReasonCode.PROVIDER_NETWORK_TIMEOUT
    assert "TimeoutException" in resp.raw_error or "Timeout" in resp.raw_error


def test_anthropic_raw_key_bytes_never_in_returned_response():
    """Grep the entire returned ProviderResponse for the raw key bytes.

    The adapter must never echo the key in the response_body, raw_error,
    provider_request_id, or any other field.
    """
    secret_bytes = b"fake-sk-ant-SECRET-BYTES-ABCDEF-DONOTLEAK"
    # Send 401 so raw_error is populated -- the most plausible leak path.
    fixture = _load_fixture("auth_error.json")
    factory = _factory_returning(status_code=401, body=fixture)
    provider = AnthropicProvider(http_client_factory=factory)
    resp = provider.submit(
        prompt="hello",
        model="claude-haiku-4-5-latest",
        virtual_key_bytes=secret_bytes,
    )
    serialized = json.dumps(
        {
            "model_id": resp.model_id,
            "response_body": resp.response_body,
            "raw_error": resp.raw_error,
            "provider_request_id": resp.provider_request_id,
            "status": resp.status,
            "reason_code": (
                resp.reason_code.value if resp.reason_code is not None else None
            ),
        }
    )
    assert secret_bytes.decode("utf-8") not in serialized
    # Belt-and-suspenders: bytes form also absent.
    assert secret_bytes not in serialized.encode("utf-8")


def test_anthropic_unknown_model_zero_cost_with_warning():
    """Unknown model in pricing table -> cost_usd=0 + RuntimeWarning."""
    fixture = {
        "id": "msg_01UNKNOWN",
        "type": "message",
        "role": "assistant",
        "model": "claude-phantom-9000",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }
    factory = _factory_returning(status_code=200, body=fixture)
    provider = AnthropicProvider(http_client_factory=factory)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        resp = provider.submit(
            prompt="hello",
            model="claude-phantom-9000",
            virtual_key_bytes=b"fake-sk-ant-AAAA",
        )
    assert resp.status == "succeeded"
    assert resp.cost_usd == Decimal("0")
    assert any(
        issubclass(w.category, RuntimeWarning)
        and "pricing" in str(w.message)
        for w in captured
    ), f"expected RuntimeWarning about pricing; got {[str(w.message) for w in captured]}"
