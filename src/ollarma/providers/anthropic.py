"""providers.anthropic -- Anthropic Messages API adapter (Phase 59, FRONT-01..06).

Ships the first frontier-provider integration under the v5.0 gateway. Direct
``httpx`` client -- no ``anthropic`` SDK dependency (D-59-02, D-105).

Raw-key invariant (I-06)
------------------------
The ``virtual_key_bytes`` parameter of ``submit`` flows through ``_decode_key``
at the single call site where the ``x-api-key`` header is assembled. The
decoded string lives only on the stack frame of ``_submit_http`` for the
duration of ``client.post``. It never touches:
  - a log line (all logging uses model-level strings, not key bytes)
  - a ProviderResponse field (``response_body`` is the JSON-parsed response
    from Anthropic, which never echoes the submitted key)
  - a FrontierReceipt (the gateway copies ProviderResponse fields into the
    receipt; no header is persisted)

Failure taxonomy
----------------
No ``except Exception`` in this module. Narrow types only:
  - ``httpx.TimeoutException`` (includes ConnectTimeout + ReadTimeout)
  - ``httpx.ConnectError``
  - ``httpx.HTTPStatusError``     (raised from ``response.raise_for_status()``)
  - ``ValueError``                (JSON parse / schema guard)
  - ``KeyError``                  (missing required fields in response)

Every narrow-catch produces a ``ProviderResponse`` with ``status="failed"``
and a specific ``reason_code``. No silent fallback (invariant I-02).
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


__all__ = [
    "AnthropicProvider",
]


# ---------------------------------------------------------------------------
# Anthropic endpoint + per-model config (D-59-02, D-59-04, D-59-05)
# ---------------------------------------------------------------------------

_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# (input_usd_per_1m, output_usd_per_1m). Anthropic Apr-2026 published rates.
# Unknown model -> zero cost + warning (D-59-05 anti-overestimate rule).
_ANTHROPIC_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-sonnet-4-5-latest": (Decimal("3"), Decimal("15")),
    "claude-haiku-4-5-latest": (Decimal("1"), Decimal("5")),
    "claude-opus-4-5-latest": (Decimal("15"), Decimal("75")),
    "claude-3-5-sonnet-20241022": (Decimal("3"), Decimal("15")),
}

# Per-model input context caps. Conservative default for unknown models.
_ANTHROPIC_CONTEXT_CAPS: dict[str, int] = {
    "claude-sonnet-4-5-latest": 200_000,
    "claude-haiku-4-5-latest": 200_000,
    "claude-opus-4-5-latest": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
}
_DEFAULT_UNKNOWN_CAP = 100_000

# max_tokens cap for output. Anthropic's default is 4096; keep it small to
# bound benchmark cost.
_DEFAULT_MAX_OUTPUT_TOKENS = 1024


def _decode_key(b: bytes) -> str:
    """Decode the raw Keychain bytes to a UTF-8 header value.

    Sole decode site for the raw key; callers must not re-implement decoding.
    """
    return b.decode("utf-8").strip()


class AnthropicProvider(BaseProvider):
    """Anthropic Messages API provider adapter.

    ``http_client_factory`` is a seam for tests; in production the adapter
    constructs a default ``httpx.Client`` with the configured timeout. Tests
    inject a factory that returns a client bound to ``httpx.MockTransport``.
    """

    def __init__(
        self,
        timeout_s: float = 60.0,
        *,
        http_client_factory: Callable[..., httpx.Client] | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._factory = http_client_factory

    # -- Public API -----------------------------------------------------------

    def submit(
        self,
        prompt: str,
        model: str,
        virtual_key_bytes: bytes,
    ) -> ProviderResponse:
        """Submit one prompt to Anthropic, return a ProviderResponse.

        Never raises on provider / transport errors -- all known failure modes
        map to ``status="failed"`` + a specific ``reason_code``. Only raises
        on truly unexpected conditions (which propagate per DEBT-10).
        """
        # 1. Local pre-truncation (D-59-04; shared helper since 60-01).
        #    The server may still reject for exceeded context; that path is
        #    handled via map_http_error(400).
        prompt_to_send, truncation_event = truncate_prompt(
            prompt,
            model,
            _ANTHROPIC_CONTEXT_CAPS,
            default_cap=_DEFAULT_UNKNOWN_CAP,
        )
        return self._submit_http(
            prompt_to_send,
            model,
            virtual_key_bytes,
            truncation_event=truncation_event,
        )

    # -- HTTP ----------------------------------------------------------------

    def _submit_http(
        self,
        prompt: str,
        model: str,
        virtual_key_bytes: bytes,
        *,
        truncation_event: dict | None,
    ) -> ProviderResponse:
        """Issue the HTTP POST + build a ProviderResponse from the outcome."""
        payload = {
            "model": model,
            "max_tokens": _DEFAULT_MAX_OUTPUT_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }

        # Header assembly is the ONLY point where the decoded key exists as
        # str. The httpx.Client serializes headers; we never hold the decoded
        # value in a module-level name.
        headers = {
            "x-api-key": _decode_key(virtual_key_bytes),
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        if self._factory is not None:
            client_ctx = self._factory(timeout=self._timeout_s)
        else:
            client_ctx = httpx.Client(timeout=self._timeout_s)

        t0 = time.perf_counter()
        try:
            with client_ctx as client:
                try:
                    response = client.post(
                        _ANTHROPIC_ENDPOINT, json=payload, headers=headers,
                    )
                except httpx.TimeoutException as exc:
                    return self._build_failed(
                        model=model,
                        reason_code=GatewayReasonCode.PROVIDER_NETWORK_TIMEOUT,
                        raw_error=f"httpx.TimeoutException: {type(exc).__name__}",
                        latency_ms=_elapsed_ms(t0),
                        truncation_event=truncation_event,
                    )
                except httpx.ConnectError as exc:
                    return self._build_failed(
                        model=model,
                        reason_code=GatewayReasonCode.PROVIDER_NETWORK_TIMEOUT,
                        raw_error=f"httpx.ConnectError: {type(exc).__name__}",
                        latency_ms=_elapsed_ms(t0),
                        truncation_event=truncation_event,
                    )
        finally:
            # Ensure the decoded key string is not referenced after the call.
            headers["x-api-key"] = ""
            del headers

        latency_ms = _elapsed_ms(t0)

        if response.status_code != 200:
            body_snippet = (response.text or "")[:500]
            reason_code, _detail = map_http_error(
                response.status_code, body_snippet,
            )
            return self._build_failed(
                model=model,
                reason_code=reason_code,
                raw_error=f"HTTP {response.status_code}: {body_snippet}",
                latency_ms=latency_ms,
                truncation_event=truncation_event,
            )

        # 2. Parse success body. Narrow-catch JSON + shape errors.
        try:
            body = response.json()
        except ValueError as exc:
            return self._build_failed(
                model=model,
                reason_code=GatewayReasonCode.PROVIDER_NETWORK_TIMEOUT,
                raw_error=f"response body is not JSON: {exc}",
                latency_ms=latency_ms,
                truncation_event=truncation_event,
            )
        if not isinstance(body, dict):
            return self._build_failed(
                model=model,
                reason_code=GatewayReasonCode.PROVIDER_NETWORK_TIMEOUT,
                raw_error="response body is not a JSON object",
                latency_ms=latency_ms,
                truncation_event=truncation_event,
            )

        usage = body.get("usage")
        if not isinstance(usage, dict):
            return self._build_failed(
                model=model,
                reason_code=GatewayReasonCode.PROVIDER_NETWORK_TIMEOUT,
                raw_error="response missing 'usage' object",
                latency_ms=latency_ms,
                truncation_event=truncation_event,
            )
        prompt_tokens_raw = usage.get("input_tokens")
        response_tokens_raw = usage.get("output_tokens")
        if not isinstance(prompt_tokens_raw, int) or not isinstance(
            response_tokens_raw, int,
        ):
            return self._build_failed(
                model=model,
                reason_code=GatewayReasonCode.PROVIDER_NETWORK_TIMEOUT,
                raw_error="response usage fields are not integers",
                latency_ms=latency_ms,
                truncation_event=truncation_event,
            )

        provider_request_id_raw = body.get("id")
        provider_request_id = (
            provider_request_id_raw
            if isinstance(provider_request_id_raw, str)
            else None
        )
        returned_model_raw = body.get("model")
        returned_model = (
            returned_model_raw if isinstance(returned_model_raw, str) else model
        )

        cost = compute_cost_usd(
            prompt_tokens_raw,
            response_tokens_raw,
            _ANTHROPIC_PRICING,
            returned_model,
        )

        return ProviderResponse(
            model_id=returned_model,
            prompt_tokens=prompt_tokens_raw,
            response_tokens=response_tokens_raw,
            cost_usd=cost,
            latency_ms=latency_ms,
            status="succeeded",
            response_body=body,
            provider_request_id=provider_request_id,
            truncation_event=truncation_event,
            raw_error=None,
            reason_code=None,
        )

    # -- Helpers -------------------------------------------------------------

    def _build_failed(
        self,
        *,
        model: str,
        reason_code: GatewayReasonCode,
        raw_error: str,
        latency_ms: int,
        truncation_event: dict | None,
    ) -> ProviderResponse:
        """Construct a ``status="failed"`` ProviderResponse.

        No cost, zero tokens; raw_error carries the operator-readable cause
        (never the raw key bytes -- the surrounding code never formats the
        key into error strings).
        """
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


def _elapsed_ms(t0: float) -> int:
    """Monotonic elapsed time since ``t0`` in milliseconds (integer)."""
    return max(0, int((time.perf_counter() - t0) * 1000))
