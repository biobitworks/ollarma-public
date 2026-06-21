"""routing_ladder.py — Benchmark-backed local-first routing ladder.

Phase 53 (ROUTE-01, ROUTE-02, ROUTE-03, OBS-02, OBS-03).

Pure decision module — no network I/O, no Ollama calls.  Reads inputs
(a resolved selection winner, a residency decision, swap telemetry) and
returns a deterministic LadderDecision.

Public surface:
  LadderRung        — one candidate in the ordered ladder
  LadderDecision    — frozen result of build_ladder()
  build_ladder()    — pure function; no side-effects
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from ollarma.reserved_models import RESERVED_MODEL_REASON_CODE, is_reserved_model
from ollarma.scheduler import SWAP_DEGRADED_THRESHOLD_MB

if TYPE_CHECKING:  # pragma: no cover
    from ollarma.residency import ResidencyDecision

# ---------------------------------------------------------------------------
# Status and reason-code constants (kept as string literals for grep-ability)
# ---------------------------------------------------------------------------

STATUS_CHOSE_PREFERRED = "chose_preferred"
STATUS_CHOSE_DEGRADED = "chose_degraded"
STATUS_CHOSE_RESCUE = "chose_rescue"
STATUS_BLOCKED_ESCALATE = "blocked_escalate"

RC_LADDER_PREFERRED = "LADDER_PREFERRED"
RC_LADDER_DEGRADED_SWAP = "LADDER_DEGRADED_SWAP"
RC_LADDER_DEGRADED_RESIDENCY = "LADDER_DEGRADED_RESIDENCY"
RC_LADDER_RESCUE_ONLY = "LADDER_RESCUE_ONLY"
RC_LADDER_BLOCKED_NO_LOCAL = "LADDER_BLOCKED_NO_LOCAL"
RC_LADDER_USER_OVERRIDE = "LADDER_USER_OVERRIDE"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class LadderRung(BaseModel, frozen=True):
    """One candidate in the ordered local routing ladder."""

    model_config = ConfigDict(frozen=True)

    rank: int
    model: str
    reason: str  # e.g. "benchmark-ranked #1 for chat workload"


class LadderDecision(BaseModel, frozen=True):
    """Frozen result of build_ladder().

    Describes WHAT the ladder chose and WHY — never executes anything.
    """

    model_config = ConfigDict(frozen=True)

    workload_class: str
    swap_used_mb: float | None
    swap_threshold_mb: float
    resident_models: tuple[str, ...]
    rungs_considered: tuple[LadderRung, ...]
    chosen_model: str | None
    chosen_rank: int | None
    status: Literal[
        "chose_preferred",
        "chose_degraded",
        "chose_rescue",
        "blocked_escalate",
    ]
    reason_code: str
    detail: str
    escalation_hint: str | None  # only non-None when status == "blocked_escalate"


# ---------------------------------------------------------------------------
# build_ladder() — pure function, deterministic
# ---------------------------------------------------------------------------


def build_ladder(
    workload_class: "str",
    *,
    selection_result: str | None = None,
    ranked_alternates: tuple[str, ...] | None = None,
    residency_decision: "ResidencyDecision | None" = None,
    swap_used_mb: float | None,
    swap_threshold_mb: float,
    rescue_model: str,
    resident_models: tuple[str, ...] | None = None,
    override_model: str | None = None,
) -> LadderDecision:
    """Pure function. Returns a deterministic ladder decision.

    Parameters
    ----------
    workload_class:
        String label for the workload being routed (e.g. "route_prompt").
    selection_result:
        Benchmark-approved winner model name from resolve_selection(), or
        None if the artifact was STALE/MISSING.
    ranked_alternates:
        Pareto frontier models from the same artifact, in ranked order.
        May overlap with selection_result.  May be empty or None.
    residency_decision:
        Current ResidencyDecision from residency.decide().  Used to derive
        resident_models when the caller doesn't provide them explicitly.
    swap_used_mb:
        Current swap from telemetry; None = unknown (treated conservatively).
    swap_threshold_mb:
        SWAP_DEGRADED_THRESHOLD_MB constant.
    rescue_model:
        RESCUE_MODEL constant from residency.py (always appended as last rung).
    resident_models:
        Optional explicit set of currently resident models.  If None, derived
        from residency_decision.  If residency_decision is also None, empty.
    override_model:
        If the caller passed an explicit model= parameter, this is it.
        build_ladder() returns a LADDER_USER_OVERRIDE short-circuit immediately.

    Ordering rules
    --------------
    1. User override → LADDER_USER_OVERRIDE short-circuit (no ladder traversal).
    2. If swap_used_mb > threshold → skip largest models; start at smaller tiers.
    3. Models known to be RESIDENT are prioritised within the same tier.
    4. selection_result provides the benchmark-backed #1 candidate.
    5. ranked_alternates provide subsequent benchmark-ranked candidates.
    6. rescue_model is always the last rung (if not already present).
    7. If no model survives the swap/residency filters → blocked_escalate.
    """
    effective_residents: frozenset[str]
    if resident_models is not None:
        effective_residents = frozenset(resident_models)
    elif residency_decision is not None:
        loaded: list[str] = []
        if residency_decision.rescue_resident:
            loaded.append(residency_decision.rescue_target)
        if residency_decision.opportunistic_resident and residency_decision.opportunistic_target:
            loaded.append(residency_decision.opportunistic_target)
        effective_residents = frozenset(loaded)
    else:
        effective_residents = frozenset()

    # --- Short-circuit: explicit user model override ---
    if override_model is not None:
        # Reserved Antigence/Sentinel models are never selectable on the generic
        # routing ladder, even via an explicit override (closes the override leak).
        if is_reserved_model(override_model):
            raise ValueError(
                f"{RESERVED_MODEL_REASON_CODE}: {override_model!r} is reserved for "
                "Antigence/Sentinel and cannot be selected via the routing ladder override."
            )
        return LadderDecision(
            workload_class=workload_class,
            swap_used_mb=swap_used_mb,
            swap_threshold_mb=swap_threshold_mb,
            resident_models=tuple(sorted(effective_residents)),
            rungs_considered=(),
            chosen_model=override_model,
            chosen_rank=None,
            status=STATUS_CHOSE_PREFERRED,
            reason_code=RC_LADDER_USER_OVERRIDE,
            detail=f"Caller-specified model override: {override_model!r}; ladder bypassed.",
            escalation_hint=None,
        )

    swap_over_threshold: bool = (
        swap_used_mb is not None and swap_used_mb > swap_threshold_mb
    )

    # --- Build the ordered candidate list ---
    # Dedupe while preserving order; rescue_model is always appended last.
    seen: set[str] = set()
    candidates: list[str] = []

    def _add(name: str) -> None:
        # Reserved Antigence/Sentinel models are never candidates on the generic
        # ladder — drop them from selection_result, ranked_alternates, AND rescue so
        # auto-selection (resident-first / pareto / rescue) can never emit them.
        if name and name not in seen and not is_reserved_model(name):
            seen.add(name)
            candidates.append(name)

    if selection_result:
        _add(selection_result)
    if ranked_alternates:
        for m in ranked_alternates:
            _add(m)
    _add(rescue_model)  # always last if not already present

    # --- Build rungs with reason labels ---
    rungs: list[LadderRung] = []
    for idx, model_name in enumerate(candidates):
        if model_name == selection_result and idx == 0:
            reason = f"benchmark-ranked #1 for {workload_class} workload"
        elif model_name == rescue_model and model_name not in (
            set(ranked_alternates or []) | ({selection_result} if selection_result else set())
        ):
            reason = "rescue fallback (always-available)"
        elif model_name in (ranked_alternates or ()):
            rank_in_pareto = list(ranked_alternates or ()).index(model_name) + 1
            reason = f"pareto-frontier ranked #{rank_in_pareto} for {workload_class} workload"
        else:
            reason = f"fallback candidate for {workload_class} workload"
        rungs.append(LadderRung(rank=idx + 1, model=model_name, reason=reason))

    # --- Filter to models that survive the current constraints ---
    # Under swap pressure, prefer resident models or rescue over large ones.
    # We do NOT know exact model sizes here, so we use two heuristics:
    #   - resident models are allowed regardless of swap (they're already loaded)
    #   - rescue_model is always allowed (smallest in fleet by policy)
    # Non-resident, non-rescue models are filtered OUT under swap pressure.

    def _model_allowed(model_name: str) -> bool:
        if not swap_over_threshold:
            return True  # all models are fine when swap is safe
        # Under swap pressure: only resident models and rescue are allowed
        return model_name in effective_residents or model_name == rescue_model

    surviving_rungs = [r for r in rungs if _model_allowed(r.model)]

    # --- Choose from surviving rungs ---
    if not surviving_rungs:
        # No local path available
        rungs_hint = ", ".join(r.model for r in rungs[:5])
        return LadderDecision(
            workload_class=workload_class,
            swap_used_mb=swap_used_mb,
            swap_threshold_mb=swap_threshold_mb,
            resident_models=tuple(sorted(effective_residents)),
            rungs_considered=tuple(rungs),
            chosen_model=None,
            chosen_rank=None,
            status=STATUS_BLOCKED_ESCALATE,
            reason_code=RC_LADDER_BLOCKED_NO_LOCAL,
            detail=(
                f"No local model is available for {workload_class!r} workload. "
                f"Swap={swap_used_mb}MB > threshold={swap_threshold_mb}MB. "
                f"Rungs checked: {rungs_hint or 'none'}."
            ),
            escalation_hint=(
                "No local path is viable under current memory pressure. "
                "Operator action: drain swap, evict models, or escalate to a frontier caller."
            ),
        )

    # --- Resident-first tiebreak within same tier ---
    # Among the first surviving rung and rescue, prefer resident if they share
    # the same rank band. The simplest correct rule: sort surviving_rungs so
    # resident models come before non-resident ones at equal rank.
    def _rung_sort_key(rung: LadderRung) -> tuple[int, int]:
        resident_bonus = 0 if rung.model in effective_residents else 1
        return (rung.rank, resident_bonus)

    surviving_rungs_sorted = sorted(surviving_rungs, key=_rung_sort_key)
    chosen_rung = surviving_rungs_sorted[0]

    # --- Determine status ---
    # Preferred: chosen rung is the benchmark winner (#1) and no swap degradation.
    # Degraded: swap pressure limited choices but a non-rescue model was found.
    # Rescue only: only the rescue model survived.
    is_benchmark_winner = (chosen_rung.model == selection_result and selection_result is not None)
    is_rescue = chosen_rung.model == rescue_model

    if is_rescue and len(surviving_rungs_sorted) == 1:
        # Only rescue survived
        if swap_over_threshold:
            status = STATUS_CHOSE_RESCUE
            reason_code = RC_LADDER_RESCUE_ONLY
            detail = (
                f"Swap pressure ({swap_used_mb}MB > {swap_threshold_mb}MB) reduced ladder to "
                f"rescue-only: {rescue_model!r}."
            )
        else:
            # Rescue is the benchmark winner (e.g. qwen2.5:1.5b won the suite)
            status = STATUS_CHOSE_PREFERRED
            reason_code = RC_LADDER_PREFERRED
            detail = (
                f"Rescue model {rescue_model!r} is the benchmark winner for "
                f"{workload_class!r} workload."
            )
    elif swap_over_threshold:
        if is_benchmark_winner:
            # The benchmark winner is resident even under swap — still preferred
            status = STATUS_CHOSE_PREFERRED
            reason_code = RC_LADDER_PREFERRED
            detail = (
                f"Benchmark winner {chosen_rung.model!r} is resident despite swap pressure "
                f"({swap_used_mb}MB). Chose preferred."
            )
        else:
            status = STATUS_CHOSE_DEGRADED
            reason_code = (
                RC_LADDER_DEGRADED_RESIDENCY
                if chosen_rung.model in effective_residents
                else RC_LADDER_DEGRADED_SWAP
            )
            detail = (
                f"Swap pressure ({swap_used_mb}MB > {swap_threshold_mb}MB) forced degraded "
                f"choice: {chosen_rung.model!r} (rank={chosen_rung.rank})."
            )
    elif not is_benchmark_winner and selection_result is not None:
        # We have a selection winner but it wasn't chosen (e.g. not resident and
        # we fell back to a resident alternative). This is degraded.
        status = STATUS_CHOSE_DEGRADED
        reason_code = RC_LADDER_DEGRADED_RESIDENCY
        detail = (
            f"Benchmark winner {selection_result!r} was not preferred; "
            f"chose {chosen_rung.model!r} (rank={chosen_rung.rank}) via residency tiebreak."
        )
    else:
        status = STATUS_CHOSE_PREFERRED
        reason_code = RC_LADDER_PREFERRED
        detail = (
            f"Chose benchmark winner {chosen_rung.model!r} for {workload_class!r} workload "
            f"(rank={chosen_rung.rank})."
        )

    return LadderDecision(
        workload_class=workload_class,
        swap_used_mb=swap_used_mb,
        swap_threshold_mb=swap_threshold_mb,
        resident_models=tuple(sorted(effective_residents)),
        rungs_considered=tuple(rungs),
        chosen_model=chosen_rung.model,
        chosen_rank=chosen_rung.rank,
        status=status,
        reason_code=reason_code,
        detail=detail,
        escalation_hint=None,
    )
