"""providers.base -- Provider-agnostic protocol + intermediate response shape.

Phase 59 introduced the abstraction with Anthropic as the first consumer.
Phase 60 (OpenAI) is the second consumer; the shared helpers ``truncate_prompt``
and ``compute_cost_usd`` were factored here at 60-01 and are now the FRONT-07
proof — both adapters import them rather than reimplement.

Exports
-------
- ``ProviderResponse``     -- frozen dataclass. Intermediate shape a provider
  adapter returns from ``submit()``. NOT persisted to disk; the gateway maps
  this into a ``FrontierReceipt`` which is the stable on-disk contract.
- ``BaseProvider``         -- Protocol for provider adapters. Every provider
  adapter implements ``submit(prompt, model, virtual_key_bytes)``.
- ``map_http_error``       -- Shared HTTP status -> ``GatewayReasonCode`` mapper.
- ``truncate_prompt``      -- Shared pre-truncation (Phase 60 refactor, FRONT-07).
- ``compute_cost_usd``     -- Shared per-token cost computation (Phase 60, FRONT-07).

Design bindings to ``.planning/phases/59-anthropic-provider-adapter/59-CONTEXT.md``
- D-59-01  module layout, ProviderResponse fields
- D-59-04  pre-truncation mechanism (generalized at 60-01)
- D-59-05  cost computation (generalized at 60-01)
- D-59-06  failure taxonomy (HTTP status -> reason code)

Stability
---------
``ProviderResponse`` is an *internal intermediate* (contract §3 subsection
added in Phase 59). Sibling projects that consume frontier-provider output
must rely on ``ollarma.gateway.FrontierReceipt`` (schema_version=1), not on
this dataclass.
"""
from __future__ import annotations

import dataclasses
import warnings
from decimal import Decimal
from typing import Protocol

from ollarma.gateway import GatewayReasonCode


__all__ = [
    "ProviderResponse",
    "BaseProvider",
    "map_http_error",
    "truncate_prompt",
    "compute_cost_usd",
]


# 4 chars/token is the standard English approximation used by every major
# frontier provider's sizing guidance; both Anthropic and OpenAI publish this
# as a coarse pre-truncation heuristic.
_CHARS_PER_TOKEN = 4
# Safety margin used when truncating to the cap: leave 10% headroom so the
# server doesn't reject on boundary cases. Applied uniformly across providers.
_TRUNCATION_SAFETY_MARGIN = 0.9


@dataclasses.dataclass(frozen=True)
class ProviderResponse:
    """Canonical intermediate shape returned by any provider adapter.

    The gateway (``GatewayClient.submit`` in Phase 59) translates this into a
    ``FrontierReceipt`` for persistence. Fields are deliberately provider-
    agnostic; provider-specific extras belong in ``response_body``.

    Attributes
    ----------
    provider_request_id :
        Provider-supplied request id (e.g., Anthropic ``id``); ``None`` when
        the provider did not return one.
    model_id :
        Canonical provider-returned model identifier. For Anthropic this is
        the ``model`` field echoed in the response body.
    prompt_tokens, response_tokens :
        Input/output token counts as reported by the provider. Zero when the
        call failed before any tokens were consumed.
    cost_usd :
        Computed by the adapter from provider pricing + token counts. Always
        ``Decimal``; never ``float`` (I-03 / D-08 parity with FrontierReceipt).
    latency_ms :
        Wall-clock time spent on the provider call, measured at the adapter.
    response_body :
        Full provider JSON body (dict). Useful for downstream inspection but
        not persisted to the FrontierReceipt itself.
    truncation_event :
        ``None`` when no truncation occurred; otherwise a dict with
        ``original_estimated_tokens`` + ``truncated_estimated_tokens``.
    raw_error :
        ``None`` on success; short string describing the failure on error
        paths (HTTP 4xx/5xx body or client exception repr). Never contains
        secret material.
    status :
        ``"succeeded"`` or ``"failed"``. Mirrors FrontierReceipt.status values
        (dry_run/disabled never flow through a provider adapter).
    reason_code :
        Populated on ``status="failed"``; one of the provider-stage members
        of ``GatewayReasonCode``. ``None`` on success.
    """

    model_id: str
    prompt_tokens: int
    response_tokens: int
    cost_usd: Decimal
    latency_ms: int
    status: str  # "succeeded" | "failed"
    response_body: dict
    provider_request_id: str | None = None
    truncation_event: dict | None = None
    raw_error: str | None = None
    reason_code: GatewayReasonCode | None = None


class BaseProvider(Protocol):
    """Protocol every provider adapter satisfies.

    Providers MUST NOT raise on HTTP 4xx/5xx or transport errors -- they must
    catch the narrow exception types they know about and return a
    ``ProviderResponse`` with ``status="failed"`` + a specific
    ``reason_code``. Any other exception propagates (DEBT-10 -- no bare
    ``except Exception``).
    """

    def submit(
        self,
        prompt: str,
        model: str,
        virtual_key_bytes: bytes,
    ) -> ProviderResponse:  # pragma: no cover -- Protocol
        ...


# ---------------------------------------------------------------------------
# Shared HTTP error -> reason_code mapper (D-59-06)
# ---------------------------------------------------------------------------


def map_http_error(status_code: int, body: str) -> tuple[GatewayReasonCode, str]:
    """Map an HTTP status code (+ body snippet) to a structured reason code.

    Uniform for Phase 59 (Anthropic); Phase 60 may split per-provider if the
    OpenAI error surface diverges meaningfully (e.g., 400 body shape). The
    body snippet flows into the ``raw_error`` field on the ProviderResponse
    and is truncated at 500 chars upstream.

    Mapping:
      - 401            -> PROVIDER_AUTH_FAILED
      - 429            -> PROVIDER_RATE_LIMITED
      - 400 w/ "context"/"too long"/"maximum" substring -> PROVIDER_CONTEXT_EXCEEDED
      - Everything else (500, 502, 503, 504, other 4xx) -> PROVIDER_NETWORK_TIMEOUT
        (generic bucket; never silent -- raw_error captures the specific body).
    """
    if status_code == 401:
        return GatewayReasonCode.PROVIDER_AUTH_FAILED, "provider rejected credentials"
    if status_code == 429:
        return GatewayReasonCode.PROVIDER_RATE_LIMITED, "provider rate-limited"
    if status_code == 400:
        lowered = body.lower()
        if (
            "context" in lowered
            or "too long" in lowered
            or "maximum" in lowered
            or "max_tokens" in lowered
        ):
            return (
                GatewayReasonCode.PROVIDER_CONTEXT_EXCEEDED,
                "provider reports context exceeded",
            )
    return (
        GatewayReasonCode.PROVIDER_NETWORK_TIMEOUT,
        f"provider HTTP {status_code} error",
    )


# ---------------------------------------------------------------------------
# Shared pre-truncation (Phase 60-01 FRONT-07 refactor; was Anthropic-only)
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: 4 chars/token (English guidance for both APIs)."""
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def truncate_prompt(
    prompt: str,
    model: str,
    context_caps: dict[str, int],
    default_cap: int = 100_000,
) -> tuple[str, dict | None]:
    """Return ``(possibly-truncated-prompt, truncation_event_or_None)``.

    Per-provider caps live in each adapter's table (``_ANTHROPIC_CONTEXT_CAPS``,
    ``_OPENAI_CONTEXT_CAPS``); the truncation *mechanism* — estimate, compare,
    truncate char-wise with a safety margin, emit a structured event — is
    provider-agnostic and shared here.

    When the rough token estimate exceeds the model's cap, truncate the prompt
    character-wise to ``safety_margin * cap * chars_per_token`` and return a
    truncation_event dict the gateway copies into the FrontierReceipt. The
    server may still reject on exceeded context; that path maps via
    ``map_http_error`` on HTTP 400.
    """
    cap = context_caps.get(model, default_cap)
    est = _estimate_tokens(prompt)
    if est <= cap:
        return prompt, None
    safe_cap = int(cap * _TRUNCATION_SAFETY_MARGIN)
    target_chars = safe_cap * _CHARS_PER_TOKEN
    truncated = prompt[:target_chars]
    return truncated, {
        "original_estimated_tokens": est,
        "truncated_estimated_tokens": _estimate_tokens(truncated),
    }


# ---------------------------------------------------------------------------
# Shared cost computation (Phase 60-01 FRONT-07 refactor; was Anthropic-only)
# ---------------------------------------------------------------------------


def compute_cost_usd(
    prompt_tokens: int,
    response_tokens: int,
    pricing_table: dict[str, tuple[Decimal, Decimal]],
    model_id: str,
) -> Decimal:
    """Compute USD cost from token counts + per-model pricing.

    Per-provider pricing tables map ``model_id -> (input_usd_per_1m,
    output_usd_per_1m)``. Unknown model → ``Decimal("0")`` + RuntimeWarning
    (D-59-05 anti-overestimate rule). Never silently falls back to a default
    rate — that would break cost accounting trust across providers.
    """
    rates = pricing_table.get(model_id)
    if rates is None:
        warnings.warn(
            f"no pricing entry for model {model_id!r}; cost_usd set to 0. "
            f"Add {model_id!r} to the provider's pricing table to enable "
            "accounting.",
            RuntimeWarning,
            stacklevel=2,
        )
        return Decimal("0")
    in_rate, out_rate = rates
    million = Decimal("1000000")
    return (
        (Decimal(prompt_tokens) / million) * in_rate
        + (Decimal(response_tokens) / million) * out_rate
    )
