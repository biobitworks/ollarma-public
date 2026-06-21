"""reserved_models.py -- single source of truth for Antigence/Sentinel-reserved models.

Pure leaf module (no intra-package imports) so service.py, routing_ladder.py, and
execution_policy.py can all share the reservation policy without circular imports.

Reserved models are owned by the Antigence/Sentinel immune lane. Ollarma's generic and
project-scoped lanes (chat, route, workflow, benchmark default, pipeline control, the
routing ladder, and selection-artifact resolution) must NEVER auto-select or accept a
reserved model. Antigence/Sentinel reach these models *directly* via the Ollama HTTP API
(their OLLAMA_MODEL env), not through Ollarma, so this reservation does not constrain
their access -- it only prevents Ollarma from poaching the reserved tier.
"""
from __future__ import annotations

# qwen3:1.7b is reserved for Antigence/Sentinel (see ~/.antigence/llm/roles.json, which
# maps every antigent role to it). The Ollarma lean bridge is a DIFFERENT model
# (qwen2.5:1.5b) -- do not conflate the two.
RESERVED_ANTIGENCE_SENTINEL_MODELS: frozenset[str] = frozenset({"qwen3:1.7b"})
RESERVED_MODEL_REASON_CODE = "RESERVED_MODEL_ANTIGENCE_SENTINEL"


def is_reserved_model(model: str | None) -> bool:
    """Return True if *model* is reserved for Antigence/Sentinel direct use."""
    if not model:
        return False
    return model.strip() in RESERVED_ANTIGENCE_SENTINEL_MODELS


def assert_model_not_reserved(model: str | None, *, context: str) -> None:
    """Refuse Antigence/Sentinel-reserved models on generic Ollarma lanes."""
    if is_reserved_model(model):
        raise ValueError(
            f"{RESERVED_MODEL_REASON_CODE}: {model!r} is reserved for "
            f"Antigence/Sentinel direct use and cannot be selected by Ollarma {context}."
        )


def drop_reserved_models(models: tuple[str, ...]) -> tuple[str, ...]:
    """Remove Antigence/Sentinel-reserved tags from a generic candidate list."""
    return tuple(model for model in models if not is_reserved_model(model))
