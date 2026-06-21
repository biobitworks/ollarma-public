"""ollarma.providers -- Frontier-provider adapter registry (Phase 59+).

Phase 59 ships Anthropic; Phase 60 adds OpenAI. The registry is a plain
``dict`` so the provider abstraction stays observable and extensible without
a heavier plug-in system.
"""
from __future__ import annotations

from ollarma.providers.anthropic import AnthropicProvider
from ollarma.providers.base import BaseProvider, ProviderResponse, map_http_error
from ollarma.providers.openai import OpenAIProvider


__all__ = [
    "BaseProvider",
    "ProviderResponse",
    "AnthropicProvider",
    "OpenAIProvider",
    "PROVIDER_REGISTRY",
    "get_provider",
    "map_http_error",
]


PROVIDER_REGISTRY: dict[str, type[BaseProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def get_provider(name: str) -> BaseProvider:
    """Return a fresh provider instance registered under ``name``.

    Raises ``KeyError`` (narrow type, per DEBT-10) when ``name`` is not
    registered. Callers should NOT wrap this in a bare ``except Exception``
    -- the missing-provider case is an operator-config error and deserves a
    specific error path (gateway maps it to ``status="failed"`` +
    ``PROVIDER_AUTH_FAILED`` upstream, or surfaces it as a 500 if the vk
    registry entry itself references an unknown provider).
    """
    cls = PROVIDER_REGISTRY.get(name)
    if cls is None:
        raise KeyError(
            f"provider {name!r} is not registered; "
            f"known providers: {sorted(PROVIDER_REGISTRY.keys())}"
        )
    return cls()
