"""residency.py — GPU residency policy: rescue pin + swap-aware opportunistic warm.

Phase 52 (GPU-01, GPU-02, GPU-03, GPU-04, OBS-01).

The policy decides which models SHOULD be resident given live telemetry.  It
never calls Ollama directly; all model-lifecycle mutations go through
PipelineController so the receipt chain stays intact.

Public surface:
  RESCUE_MODEL       — always-pinned helper; smallest model in the fleet
  OPTIONAL_STRONGER  — opportunistically warmed when swap budget allows
  ResidencyDecision  — frozen result of decide()
  ResidencyApplyReceipt — append-only record of what apply() did
  decide()           — pure function; no side-effects
  apply()            — executes the decision; respects benchmark-active freeze
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from ollarma.scheduler import SWAP_DEGRADED_THRESHOLD_MB

if TYPE_CHECKING:  # pragma: no cover
    from ollarma.guards import RuntimeTelemetry
    from ollarma.pipeline_control import PipelineController

# ---------------------------------------------------------------------------
# Tunable constants (env-configurable in a future phase; hardcoded for Phase 52)
# ---------------------------------------------------------------------------

RESCUE_MODEL: str = "qwen2.5:1.5b"     # tiny no-cloud bridge; always-available rescue path
OPTIONAL_STRONGER: str = "phi4-mini"   # ~2.3 GB Q4 — opportunistic if swap safe


def _model_names_match(configured: str, runtime_name: str) -> bool:
    """Return True when an Ollama runtime name matches a configured model name."""
    return configured == runtime_name or f"{configured}:latest" == runtime_name


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ResidencyDecision(BaseModel, frozen=True):
    """Frozen result of decide().  Describes WHAT the policy wants, not what happened."""

    model_config = ConfigDict(frozen=True)

    state: Literal["ready", "degraded", "blocked"]
    rescue_target: str
    rescue_resident: bool
    opportunistic_target: str | None
    opportunistic_resident: bool
    swap_used_mb: float | None
    reason_code: str | None
    next_action: Literal["pin_rescue", "warm_opportunistic", "evict_opportunistic", "hold", "blocked"]


class ResidencyApplyReceipt(BaseModel, frozen=True):
    """Append-only record of what apply() executed."""

    model_config = ConfigDict(frozen=True)

    decision_state: str
    next_action: str
    actions_taken: tuple[str, ...]   # e.g. ("pin:qwen2.5:1.5b",)
    errors: tuple[str, ...]          # non-fatal errors encountered during apply
    skipped_benchmark_active: bool


# ---------------------------------------------------------------------------
# decide() — pure function, no side-effects
# ---------------------------------------------------------------------------

def decide(
    *,
    telemetry: "RuntimeTelemetry",
    controller: "PipelineController",
    benchmark_active: bool,
) -> ResidencyDecision:
    """Derive a residency decision from live telemetry and controller state.

    Rules (in priority order):
    1. benchmark_active → blocked; no mutations proposed.
    2. rescue not resident AND swap safe AND not benchmark_active → pin_rescue.
    3. swap > threshold AND opportunistic resident → evict_opportunistic (GPU-03).
    4. swap safe AND rescue resident AND opportunistic not resident → warm_opportunistic.
    5. Otherwise → hold.

    State summary:
      ready    — rescue resident and overall policy satisfied.
      degraded — rescue is loading (not yet resident) or swap forced eviction.
      blocked  — benchmark_active OR rescue unloadable after one attempt.
    """
    swap_mb: float | None = telemetry.swap_used_mb
    loaded: frozenset[str] = frozenset(telemetry.loaded_models)

    # Resolve residency from loaded model names
    rescue_resident: bool = any(_model_names_match(RESCUE_MODEL, name) for name in loaded)
    opportunistic_resident: bool = any(
        _model_names_match(OPTIONAL_STRONGER, name) for name in loaded
    )

    # Pin state is ownership intent, not proof of GPU residency.  A pinned model
    # that is absent from /api/ps is drift/loading and must still be warmed.

    # Rule 1: benchmark freeze
    if benchmark_active:
        return ResidencyDecision(
            state="blocked",
            rescue_target=RESCUE_MODEL,
            rescue_resident=rescue_resident,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=opportunistic_resident,
            swap_used_mb=swap_mb,
            reason_code="BENCHMARK_ACTIVE",
            next_action="blocked",
        )

    swap_over_threshold: bool = (
        swap_mb is not None and swap_mb > SWAP_DEGRADED_THRESHOLD_MB
    )

    # Rule 2: rescue not resident, swap safe → pin_rescue
    if not rescue_resident and not swap_over_threshold:
        return ResidencyDecision(
            state="degraded",
            rescue_target=RESCUE_MODEL,
            rescue_resident=False,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=opportunistic_resident,
            swap_used_mb=swap_mb,
            reason_code="RESCUE_LOADING",
            next_action="pin_rescue",
        )

    # Rule 3: swap over threshold AND opportunistic is loaded → evict (GPU-03)
    if swap_over_threshold and opportunistic_resident:
        return ResidencyDecision(
            state="degraded",
            rescue_target=RESCUE_MODEL,
            rescue_resident=rescue_resident,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=True,
            swap_used_mb=swap_mb,
            reason_code="SWAP_DEGRADED_DROP_OPPORTUNISTIC",
            next_action="evict_opportunistic",
        )

    # Rule 4: swap safe, rescue resident, opportunistic not loaded → warm it
    if not swap_over_threshold and rescue_resident and not opportunistic_resident:
        return ResidencyDecision(
            state="ready",
            rescue_target=RESCUE_MODEL,
            rescue_resident=True,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=False,
            swap_used_mb=swap_mb,
            reason_code=None,
            next_action="warm_opportunistic",
        )

    # Rule 5: hold — everything is already as desired
    # State is ready when rescue is resident (opportunistic may or may not be present)
    if rescue_resident:
        state: Literal["ready", "degraded", "blocked"] = "ready"
    else:
        # rescue not resident but swap is over threshold — degraded, can't load rescue
        state = "degraded"

    return ResidencyDecision(
        state=state,
        rescue_target=RESCUE_MODEL,
        rescue_resident=rescue_resident,
        opportunistic_target=OPTIONAL_STRONGER,
        opportunistic_resident=opportunistic_resident,
        swap_used_mb=swap_mb,
        reason_code=None if rescue_resident else "RESCUE_BLOCKED_SWAP",
        next_action="hold",
    )


# ---------------------------------------------------------------------------
# apply() — executes the decision via PipelineController
# ---------------------------------------------------------------------------

def apply(
    decision: ResidencyDecision,
    controller: "PipelineController",
) -> ResidencyApplyReceipt:
    """Execute the residency decision via controller lifecycle methods.

    Never calls pipeline ops while decision.next_action == "blocked"
    (PIPE-10 / benchmark-active freeze).

    All errors are caught and recorded in the receipt — the policy is
    best-effort; a failure to warm the opportunistic model is non-fatal.
    """
    actions_taken: list[str] = []
    errors: list[str] = []
    skipped = False

    if decision.next_action == "blocked":
        skipped = True
        return ResidencyApplyReceipt(
            decision_state=decision.state,
            next_action=decision.next_action,
            actions_taken=(),
            errors=(),
            skipped_benchmark_active=True,
        )

    def _already_pinned(model: str) -> bool:
        try:
            state = controller._pin_state.get(model)  # noqa: SLF001
        except (RuntimeError, OSError, AttributeError):
            return False
        return bool(state and state.pin_count > 0)

    if decision.next_action == "pin_rescue":
        if decision.opportunistic_resident and decision.opportunistic_target:
            try:
                controller.evict(decision.opportunistic_target)
                actions_taken.append(f"evict:{decision.opportunistic_target}")
            except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
                errors.append(f"evict_failed:{decision.opportunistic_target}:{exc}")
        try:
            controller.warmup(decision.rescue_target, keep_alive=-1)
            actions_taken.append(f"warmup:{decision.rescue_target}")
        except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
            errors.append(f"warmup_failed:{decision.rescue_target}:{exc}")
        else:
            if not _already_pinned(decision.rescue_target):
                try:
                    controller.pin(decision.rescue_target)
                    actions_taken.append(f"pin:{decision.rescue_target}")
                except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
                    errors.append(f"pin_failed:{decision.rescue_target}:{exc}")

    elif decision.next_action == "evict_opportunistic" and decision.opportunistic_target:
        try:
            controller.evict(decision.opportunistic_target)
            actions_taken.append(f"evict:{decision.opportunistic_target}")
        except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
            # If model is pinned by another holder, record but don't raise
            errors.append(f"evict_failed:{decision.opportunistic_target}:{exc}")

    elif decision.next_action == "warm_opportunistic" and decision.opportunistic_target:
        try:
            controller.warmup(decision.opportunistic_target)
            actions_taken.append(f"warmup:{decision.opportunistic_target}")
        except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
            # Non-fatal — opportunistic warm is best-effort
            errors.append(f"warm_opportunistic_failed:{decision.opportunistic_target}:{exc}")

    # "hold" → no-op

    return ResidencyApplyReceipt(
        decision_state=decision.state,
        next_action=decision.next_action,
        actions_taken=tuple(actions_taken),
        errors=tuple(errors),
        skipped_benchmark_active=skipped,
    )
