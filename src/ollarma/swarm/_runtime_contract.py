"""_runtime_contract.py -- Shared hardware-contract constants for ollarma swarms.

Single source of truth for runtime knobs used by BOTH:

* Phase 70 predictive deliberative swarm (``ollarma.swarm.engine``)
* Phase 66+ execution-lane swarm (``ollarma.swarm.lane``)

These constants encode the CLAUDE.md / PROMPT-OLLARMA-SWARM-001 hardware contract:
single-GPU, sequential, deterministic, MLX-resident, thermal-honest. The two
sister subpackages MUST NOT diverge on these values; if a future model needs
different knobs it should pass them explicitly per-call rather than redefining
the defaults.

Constants
---------
DEFAULT_NUM_CTX (int)            -- KV cache window in tokens. 1024 keeps
                                    M1 Max 32GB unified memory comfortably
                                    under-budget while still permitting
                                    one-shot persona prompts.
DEFAULT_NUM_PREDICT (int)        -- Max output tokens per call. 200 is enough
                                    for a one-paragraph stance/post AND for
                                    a four-line lane output (planner steps,
                                    executor result, reviewer findings,
                                    synthesizer summary).
DEFAULT_TEMPERATURE (float)      -- 0.0 with seed=42 gives byte-identical
                                    output for byte-identical prompts on
                                    Ollama 0.19+ MLX backend.
DEFAULT_SEED (int)               -- Pinned generation seed (Pitfall:
                                    different from any persona-draw seed).
DEFAULT_NUM_GPU (int)            -- 999 = "use all GPU layers" (Ollama idiom).
DEFAULT_LEASE_TTL_SECONDS (int)  -- Lane-runtime lease default; long enough
                                    for slow Ollama call + thermal throttle,
                                    short enough for honest expiry.
DEFAULT_RESPONSE_FORMAT (str)    -- "json" -- triggers Ollama constrained
                                    decoding when used as the ``format=``
                                    kwarg on ``client.generate``.
DEFAULT_LANE_TOKEN_BUDGET (int)  -- Phase 67 per-lane token budget. Mirrors
                                    ``num_ctx`` headroom; caller's
                                    ``on_role_invoke`` is responsible for
                                    raising :class:`TokenExhaustedError`
                                    when its own counter exceeds this.
                                    Operator override via
                                    ``LaneRun.metadata["token_budget"]``.
SWAP_DEGRADED_PCT_THRESHOLD (int)
                                 -- Phase 67 swap-pressure guard. Resume is
                                    refused (BLOCKED, ``SWAP_DEGRADED``) when
                                    the injected ``swap_percent_provider``
                                    returns >= this percent. Phase 68 wires
                                    a real psutil/vm_stat provider.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from ollarma.swarm.lane.schemas import LaneRoleLadder

DEFAULT_NUM_CTX: Final[int] = 1024
DEFAULT_NUM_PREDICT: Final[int] = 200
DEFAULT_TEMPERATURE: Final[float] = 0.0
DEFAULT_SEED: Final[int] = 42
DEFAULT_NUM_GPU: Final[int] = 999
DEFAULT_LEASE_TTL_SECONDS: Final[int] = 60
DEFAULT_RESPONSE_FORMAT: Final[str] = "json"

# Phase 67 additions -- checkpoint/resume + token-loss recovery.
DEFAULT_LANE_TOKEN_BUDGET: Final[int] = 4096
SWAP_DEGRADED_PCT_THRESHOLD: Final[int] = 50

# Phase 68 additions -- routing-ladder thresholds + role defaults.
SWAP_BLOCKED_PCT_THRESHOLD: Final[int] = 80
"""Phase 68 swap-pressure refusal threshold.

When the live swap-percent provider returns >= this value, ``select_lane_model``
returns outcome ``"blocked_escalate"`` and the lane is refused (no model is
called). Strictly greater than ``SWAP_DEGRADED_PCT_THRESHOLD`` so the
DEGRADED -> BLOCKED escalation is monotonic.
"""

def _build_default_lane_ladder() -> dict[str, "LaneRoleLadder"]:
    """Construct the Phase 68 default per-role ladder.

    Built via a function (and exposed lazily through module ``__getattr__``)
    so that importing :class:`LaneRoleLadder` does not happen at
    module-import time. The ``ollarma.swarm.lane`` package eagerly imports
    :mod:`ollarma.swarm.lane.runtime`, which in turn imports
    :mod:`ollarma.swarm._runtime_contract` -- a top-level import here would
    create a circular initialization loop.
    """
    from ollarma.swarm.lane.schemas import LaneRoleLadder

    return {
        "planner":     LaneRoleLadder(preferred="qwen2.5:1.5b",     rescue="qwen2.5:1.5b"),
        "executor":    LaneRoleLadder(preferred="qwen2.5-coder:7b", rescue="qwen2.5:1.5b"),
        "reviewer":    LaneRoleLadder(preferred="qwen2.5-coder:7b", rescue="qwen2.5:1.5b"),
        "synthesizer": LaneRoleLadder(preferred="qwen2.5:1.5b",     rescue="qwen2.5:1.5b"),
    }


_DEFAULT_LANE_LADDER_CACHE: dict[str, "LaneRoleLadder"] | None = None


def __getattr__(name: str) -> Any:
    """Lazy module attribute -- materialize ``DEFAULT_LANE_LADDER`` on first access.

    Phase 68 default per-role model ladder (string-keyed by ``Role`` value).

    Defaults match the v4.5 GPU residency policy: ``qwen2.5:1.5b`` for
    structural/summary lanes (planner, synthesizer) and ``qwen2.5-coder:7b``
    for executor/reviewer (which need code-aware reasoning). Rescue is
    always ``qwen2.5:1.5b`` so it stays pinned in unified memory regardless
    of the preferred model. Operators may override per-call via the
    ``ladder=`` kwarg on ``run_swarm_lane`` / ``resume_swarm_lane``
    (plan 68-02). Keys are ``Role`` literal values; consumer call sites
    validate keys against ``ROLE_CHAIN``.

    The lazy build avoids a circular import between this module and
    :mod:`ollarma.swarm.lane.schemas` (see ``_build_default_lane_ladder``).
    """
    global _DEFAULT_LANE_LADDER_CACHE
    if name == "DEFAULT_LANE_LADDER":
        if _DEFAULT_LANE_LADDER_CACHE is None:
            _DEFAULT_LANE_LADDER_CACHE = _build_default_lane_ladder()
        return _DEFAULT_LANE_LADDER_CACHE
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Bundled options dict in the exact shape both engines pass to ``client.generate``.
# Re-exported by ``ollarma.swarm.engine`` as ``DEFAULT_OLLAMA_OPTIONS`` for
# backward compatibility with Phase 70 callers / tests.
DEFAULT_OLLAMA_OPTIONS: Final[dict[str, Any]] = {
    "temperature": DEFAULT_TEMPERATURE,
    "seed": DEFAULT_SEED,
    "num_ctx": DEFAULT_NUM_CTX,
    "num_predict": DEFAULT_NUM_PREDICT,
    "num_gpu": DEFAULT_NUM_GPU,
}

__all__ = [
    "DEFAULT_LANE_LADDER",
    "DEFAULT_LANE_TOKEN_BUDGET",
    "DEFAULT_LEASE_TTL_SECONDS",
    "DEFAULT_NUM_CTX",
    "DEFAULT_NUM_GPU",
    "DEFAULT_NUM_PREDICT",
    "DEFAULT_OLLAMA_OPTIONS",
    "DEFAULT_RESPONSE_FORMAT",
    "DEFAULT_SEED",
    "DEFAULT_TEMPERATURE",
    "SWAP_BLOCKED_PCT_THRESHOLD",
    "SWAP_DEGRADED_PCT_THRESHOLD",
]
