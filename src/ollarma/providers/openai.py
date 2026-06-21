"""providers.openai -- OpenAI Chat Completions adapter (Phase 60, FRONT-05).

Second frontier-provider integration under the v5.0 gateway. Direct ``httpx``
client (no ``openai`` SDK dep). Shared mechanisms (truncation, cost, HTTP
error mapping) live in ``providers.base`` — this module is the thin OpenAI-
specific layer (endpoint, payload shape, response field names, pricing).

FRONT-07 proof: adapter stays under the 200 non-blank non-comment LOC budget
asserted by ``tests/test_provider_abstraction.py``.

Raw-key invariant (I-06): decoded key lives only on the stack of
``_submit_http`` during ``client.post``; never persisted / logged / echoed.

Failure taxonomy: narrow excepts only (TimeoutException, ConnectError,
ValueError). No ``except Exception``.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable

import httpx

from ollarma.gateway import GatewayReasonCode
from ollarma.providers.base import (
    BaseProvider,
    ProviderResponse,
    compute_cost_usd,
    map_http_error,
    truncate_prompt,
)


__all__ = ["OpenAIProvider"]


_OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"

# (input_usd_per_1m, output_usd_per_1m). OpenAI Apr-2026 published rates.
_OPENAI_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o": (Decimal("2.50"), Decimal("10")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "o1": (Decimal("15"), Decimal("60")),
    "o1-mini": (Decimal("3"), Decimal("12")),
}

_OPENAI_CONTEXT_CAPS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o1": 200_000,
    "o1-mini": 200_000,
}
_DEFAULT_UNKNOWN_CAP = 100_000
_DEFAULT_MAX_OUTPUT_TOKENS = 4096


def _decode_key(b: bytes) -> str:
    """Sole decode site for raw Keychain bytes -> Bearer token value."""
    return b.decode("utf-8").strip()


def _elapsed_ms(t0: float) -> int:
    return max(0, int((time.perf_counter() - t0) * 1000))


class OpenAIProvider(BaseProvider):
    """OpenAI Chat Completions API adapter.

    ``http_client_factory`` is a test seam; production uses a default
    ``httpx.Client``.
    """

    def __init__(
        self,
        timeout_s: float = 60.0,
        *,
        http_client_factory: Callable[..., httpx.Client] | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._factory = http_client_factory

    def submit(
        self,
        prompt: str,
        model: str,
        virtual_key_bytes: bytes,
    ) -> ProviderResponse:
        """Submit one prompt to OpenAI; never raises on provider/transport errors."""
        prompt_to_send, truncation_event = truncate_prompt(
            prompt, model, _OPENAI_CONTEXT_CAPS, default_cap=_DEFAULT_UNKNOWN_CAP,
        )
        return self._submit_http(
            prompt_to_send, model, virtual_key_bytes,
            truncation_event=truncation_event,
        )

    def _fail(
        self,
        *,
        model: str,
        reason_code: GatewayReasonCode,
        raw_error: str,
        latency_ms: int,
        truncation_event: dict | None,
    ) -> ProviderResponse:
        return ProviderResponse(
            model_id=model,
            prompt_tokens=0,
            response_tokens=0,
            cost_usd=Decimal("0"),
            latency_ms=latency_ms,
            status="failed",
            response_body={},
            provider_request_id=None,
            truncation_event=truncation_event,
            raw_error=raw_error[:500],
            reason_code=reason_code,
        )

    def _submit_http(
        self,
        prompt: str,
        model: str,
        virtual_key_bytes: bytes,
        *,
        truncation_event: dict | None,
    ) -> ProviderResponse:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": _DEFAULT_MAX_OUTPUT_TOKENS,
        }
        # Header assembly is the ONLY point where the decoded key exists as str.
        headers = {
            "Authorization": f"Bearer {_decode_key(virtual_key_bytes)}",
            "content-type": "application/json",
        }
        if self._factory is not None:
            client_ctx = self._factory(timeout=self._timeout_s)
        else:
            client_ctx = httpx.Client(timeout=self._timeout_s)

        t0 = time.perf_counter()
        timeout_net = GatewayReasonCode.PROVIDER_NETWORK_TIMEOUT
        fail = lambda code, err: self._fail(  # noqa: E731
            model=model, reason_code=code, raw_error=err,
            latency_ms=_elapsed_ms(t0), truncation_event=truncation_event,
        )
        try:
            with client_ctx as client:
                try:
                    response = client.post(
                        _OPENAI_ENDPOINT, json=payload, headers=headers,
                    )
                except httpx.TimeoutException as exc:
                    return fail(timeout_net, f"httpx.TimeoutException: {type(exc).__name__}")
                except httpx.ConnectError as exc:
                    return fail(timeout_net, f"httpx.ConnectError: {type(exc).__name__}")
        finally:
            headers["Authorization"] = ""
            del headers

        latency_ms = _elapsed_ms(t0)

        if response.status_code != 200:
            body_snippet = (response.text or "")[:500]
            reason_code, _ = map_http_error(response.status_code, body_snippet)
            return self._fail(
                model=model, reason_code=reason_code,
                raw_error=f"HTTP {response.status_code}: {body_snippet}",
                latency_ms=latency_ms, truncation_event=truncation_event,
            )

        try:
            body = response.json()
        except ValueError as exc:
            return self._fail(
                model=model, reason_code=timeout_net,
                raw_error=f"response body is not JSON: {exc}",
                latency_ms=latency_ms, truncation_event=truncation_event,
            )
        if not isinstance(body, dict):
            return self._fail(
                model=model, reason_code=timeout_net,
                raw_error="response body is not a JSON object",
                latency_ms=latency_ms, truncation_event=truncation_event,
            )

        usage = body.get("usage")
        if not isinstance(usage, dict):
            return self._fail(
                model=model, reason_code=timeout_net,
                raw_error="response missing 'usage' object",
                latency_ms=latency_ms, truncation_event=truncation_event,
            )
        p_raw = usage.get("prompt_tokens")
        r_raw = usage.get("completion_tokens")
        if not isinstance(p_raw, int) or not isinstance(r_raw, int):
            return self._fail(
                model=model, reason_code=timeout_net,
                raw_error="response usage fields are not integers",
                latency_ms=latency_ms, truncation_event=truncation_event,
            )

        pid_raw = body.get("id")
        pid = pid_raw if isinstance(pid_raw, str) else None
        rm_raw = body.get("model")
        returned_model = rm_raw if isinstance(rm_raw, str) else model

        cost = compute_cost_usd(p_raw, r_raw, _OPENAI_PRICING, returned_model)

        return ProviderResponse(
            model_id=returned_model,
            prompt_tokens=p_raw,
            response_tokens=r_raw,
            cost_usd=cost,
            latency_ms=latency_ms,
            status="succeeded",
            response_body=body,
            provider_request_id=pid,
            truncation_event=truncation_event,
            raw_error=None,
            reason_code=None,
        )
