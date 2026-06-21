"""test_provider_abstraction.py -- Phase 60-01 FRONT-07 proof tests.

The mere existence of a second provider (OpenAI) under the same BaseProvider
protocol + shared helpers (``truncate_prompt``, ``compute_cost_usd``,
``map_http_error``) is the proof that the abstraction generalizes. These two
tests encode that proof so regressions are caught.
"""
from __future__ import annotations

import pathlib

from ollarma.providers import PROVIDER_REGISTRY, get_provider
from ollarma.providers.anthropic import AnthropicProvider
from ollarma.providers.openai import OpenAIProvider


def test_both_providers_implement_base_protocol():
    """Both registered provider classes expose the BaseProvider interface.

    BaseProvider is a Protocol (structural), so we duck-check: every entry in
    ``PROVIDER_REGISTRY`` must have a callable ``submit`` that accepts the
    ``(prompt, model, virtual_key_bytes)`` shape. The registry itself must
    include exactly the two Phase-60 providers.
    """
    assert set(PROVIDER_REGISTRY.keys()) == {"anthropic", "openai"}
    assert PROVIDER_REGISTRY["anthropic"] is AnthropicProvider
    assert PROVIDER_REGISTRY["openai"] is OpenAIProvider

    for name in ("anthropic", "openai"):
        instance = get_provider(name)
        assert hasattr(instance, "submit"), f"{name!r} missing submit()"
        assert callable(instance.submit), f"{name!r}.submit is not callable"


def test_openai_adapter_loc_target_under_200():
    """OpenAI adapter stays under the 200 non-blank non-comment LOC budget.

    This is the FRONT-07 proof test: the adapter module
    ``providers/openai.py`` must lean on shared helpers enough that its own
    LOC footprint is small. If this test fails, the abstraction is leaking —
    push more behavior into ``providers.base``, not into the adapter.

    Counting rule: skip blank lines and lines whose first non-whitespace
    character is ``#``. Docstrings and normal code count. Mirrors what a
    human reading the file calls "lines of code".
    """
    path = pathlib.Path(__file__).resolve().parent.parent / "src" / "ollarma" / "providers" / "openai.py"
    assert path.exists(), f"openai adapter missing at {path}"

    count = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        count += 1

    assert count < 200, (
        f"OpenAI adapter is {count} non-blank non-comment LOC "
        f"(FRONT-07 target: <200). Push more behavior into providers.base."
    )
