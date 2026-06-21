"""test_provider_openai.py -- Phase 60-01 OpenAI adapter unit tests.

All tests mock at the ``httpx`` transport boundary using ``httpx.MockTransport``.
The ``OpenAIProvider``'s ``http_client_factory`` seam is the single injection
point; no external mocking library required.
"""
from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import httpx

from ollarma.gateway import GatewayReasonCode
from ollarma.providers.openai import (
    OpenAIProvider,
    _OPENAI_CONTEXT_CAPS,
    _OPENAI_PRICING,
)
from ollarma.providers.base import ProviderResponse


_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openai"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _factory_returning(
    *, status_code: int, body: dict, raise_exc: Exception | None = None,
):
    """Build an ``http_client_factory`` returning an ``httpx.Client`` whose
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
# Plan tests 1-6
# ---------------------------------------------------------------------------


def test_openai_submit_success_populates_provider_response():
    """A 200 response with valid usage populates every ProviderResponse field."""
    fixture = _load_fixture("success_gpt4o.json")
    factory = _factory_returning(status_code=200, body=fixture)
    provider = OpenAIProvider(http_client_factory=factory)
    resp = provider.submit(
        prompt="hello",
        model="gpt-4o",
        virtual_key_bytes=b"fake-sk-openai-AAAA",
    )
    assert isinstance(resp, ProviderResponse)
    assert resp.status == "succeeded"
    assert resp.model_id == "gpt-4o"
    assert resp.prompt_tokens == 14
    assert resp.response_tokens == 9
    assert resp.cost_usd > Decimal("0")
    assert resp.latency_ms >= 0
    assert resp.provider_request_id == "chatcmpl-ABCDEF123456"
    assert resp.reason_code is None
    assert resp.raw_error is None
    assert resp.response_body["id"] == "chatcmpl-ABCDEF123456"


def test_openai_truncation_when_prompt_exceeds_cap():
    """Prompt estimated > cap -> truncation_event populated; call proceeds."""
    fixture = _load_fixture("success_gpt4o.json")
    factory = _factory_returning(status_code=200, body=fixture)
    provider = OpenAIProvider(http_client_factory=factory)

    model = "gpt-4o"
    cap = _OPENAI_CONTEXT_CAPS[model]
    # 5 chars/token gives a prompt clearly over cap (truncate_prompt uses 4).
    overlong = "B" * ((cap + 1000) * 5)

    resp = provider.submit(
        prompt=overlong, model=model, virtual_key_bytes=b"fake-sk-openai-AAAA",
    )
    assert resp.status == "succeeded"
    assert resp.truncation_event is not None
    assert resp.truncation_event["original_estimated_tokens"] > cap
    assert resp.truncation_event["truncated_estimated_tokens"] < cap


def test_openai_cost_usd_computed_as_decimal():
    """cost_usd is Decimal; value matches price * tokens / 1M formula."""
    fixture = _load_fixture("success_gpt4o.json")
    factory = _factory_returning(status_code=200, body=fixture)
    provider = OpenAIProvider(http_client_factory=factory)
    resp = provider.submit(
        prompt="hello",
        model="gpt-4o",
        virtual_key_bytes=b"fake-sk-openai-AAAA",
    )
    assert isinstance(resp.cost_usd, Decimal)
    in_rate, out_rate = _OPENAI_PRICING["gpt-4o"]
    million = Decimal("1000000")
    expected = (
        (Decimal(14) / million) * in_rate
        + (Decimal(9) / million) * out_rate
    )
    assert resp.cost_usd == expected


def test_openai_auth_error_maps_to_provider_auth_failed():
    """HTTP 401 -> status=failed with reason_code=PROVIDER_AUTH_FAILED."""
    fixture = _load_fixture("auth_error.json")
    factory = _factory_returning(status_code=401, body=fixture)
    provider = OpenAIProvider(http_client_factory=factory)
    resp = provider.submit(
        prompt="hello",
        model="gpt-4o",
        virtual_key_bytes=b"fake-sk-openai-BAD",
    )
    assert resp.status == "failed"
    assert resp.reason_code == GatewayReasonCode.PROVIDER_AUTH_FAILED
    assert resp.raw_error is not None
    assert "401" in resp.raw_error
    assert resp.cost_usd == Decimal("0")
    assert resp.prompt_tokens == 0


def test_openai_rate_limit_maps_to_provider_rate_limited():
    """HTTP 429 -> status=failed with reason_code=PROVIDER_RATE_LIMITED."""
    fixture = _load_fixture("rate_limit.json")
    factory = _factory_returning(status_code=429, body=fixture)
    provider = OpenAIProvider(http_client_factory=factory)
    resp = provider.submit(
        prompt="hello",
        model="gpt-4o",
        virtual_key_bytes=b"fake-sk-openai-AAAA",
    )
    assert resp.status == "failed"
    assert resp.reason_code == GatewayReasonCode.PROVIDER_RATE_LIMITED
    assert "429" in resp.raw_error


def test_openai_raw_key_bytes_never_in_returned_response():
    """Grep the entire returned ProviderResponse for the raw key bytes.

    The adapter must never echo the key in response_body, raw_error,
    provider_request_id, or any other field. Especially tested on the 401
    path since raw_error is populated there.
    """
    secret_bytes = b"fake-sk-openai-SECRET-BYTES-ABCDEF-DONOTLEAK"
    fixture = _load_fixture("auth_error.json")
    factory = _factory_returning(status_code=401, body=fixture)
    provider = OpenAIProvider(http_client_factory=factory)
    resp = provider.submit(
        prompt="hello",
        model="gpt-4o",
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
    assert secret_bytes not in serialized.encode("utf-8")
