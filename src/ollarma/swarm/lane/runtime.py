"""runtime.py -- ``run_swarm_lane()`` orchestrator (Phase 66 plan 02 + Phase 67 plan 02).

Drives a single :class:`SwarmRun` through the hardcoded role chain:

    planner -> executor -> reviewer -> synthesizer

Sequential, single-GPU honest. Each role gets full upstream visibility (all
prior outputs) via :class:`LaneRun.upstream_outputs`. Each lane handoff
emits a :class:`LaneTransition` receipt that ``LaneStore`` hash-chains.

Provider-agnostic
-----------------
The orchestrator does NOT call Ollama. It calls a user-supplied
``on_role_invoke(role, lane_run) -> LaneOutputBase`` callback. This keeps
``runtime.py`` testable with stubs and decouples it from any specific
provider. When ``on_role_invoke`` is ``None`` (default) the orchestrator
emits a typed empty output for each role -- useful for receipt-chain tests
that don't need real LLM content.

Failure modes
-------------
* Lease not acquired                 -> ``status="blocked"``, ``reason_code="LEASE_HELD_BY_OTHER"``,
                                        single BLOCKED transition emitted, return.
* Lease expired or stolen mid-run    -> ``status="failed"``, ``reason_code="LEASE_EXPIRED"``,
                                        BLOCKED transition emitted, return.
* Role invoker raises (after 1 retry) -> the lane is quarantined to
                                         ``<run_dir>/quarantine.jsonl``;
                                         ``status="failed"``, ``reason_code="LANE_FAILED"``;
                                         BLOCKED transition emitted, return.
* Role invoker raises :class:`TokenExhaustedError` (Phase 67) -> the partial
                                         is quarantined with ``cause="token_exhausted"``;
                                         the lane is restarted ONCE with a fresh
                                         ``lane_id`` (same role, full upstream visibility).
                                         Second TE in the same role -> ``status="failed"``,
                                         ``reason_code="TOKEN_BUDGET_EXCEEDED"``,
                                         BLOCKED transition emitted, return.

After every successful lane completion the orchestrator persists a
``Checkpoint`` to ``<run_dir>/checkpoint.json`` (Phase 67) so a crashed /
expired run always has a freshest "where did we stop" pointer for
:func:`resume_swarm_lane` to consume.

The lease is ALWAYS released in the ``finally`` block (best-effort; release
returns False if we already lost it, which is fine).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import orjson

from ollarma.evidence import canonical_hash
from ollarma.swarm._runtime_contract import (
    DEFAULT_LEASE_TTL_SECONDS,
    SWAP_DEGRADED_PCT_THRESHOLD,
)
from ollarma.swarm.lane.lease import LeaseManager
from ollarma.swarm.lane.routing import _merge_ladder, select_lane_model
from ollarma.swarm.lane.schemas import (
    Checkpoint,
    ExecutorOutput,
    LaneOutcome,
    LaneOutputBase,
    LaneRoleLadder,
    LaneRun,
    LaneTransition,
    PlannerOutput,
    ROLE_CHAIN,
    ReviewerOutput,
    Role,
    SwarmRun,
    SynthesizerOutput,
)
from ollarma.swarm.lane.store import LaneStore, LaneStoreError


__all__ = [
    "ROLE_OUTPUT_MAP",
    "RoleInvoker",
    "TokenExhaustedError",
    "resume_swarm_lane",
    "run_swarm_lane",
]


# Default role-output mapping; on_role_invoke must return one of these.
ROLE_OUTPUT_MAP: dict[Role, type[LaneOutputBase]] = {
    "planner": PlannerOutput,
    "executor": ExecutorOutput,
    "reviewer": ReviewerOutput,
    "synthesizer": SynthesizerOutput,
}

# Type alias for the role-invoker callback.
RoleInvoker = Callable[[Role, LaneRun], LaneOutputBase]

# Sentinel UUID used in BLOCKED transition receipts (no real lane ran).
_BLOCKED_LANE_ID = UUID("00000000-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------
# Phase 67: token-loss recovery
# ---------------------------------------------------------------------------

class TokenExhaustedError(Exception):
    """Raised by ``on_role_invoke`` when the per-lane token budget is
    exhausted mid-call.

    The orchestrator catches this SPECIFICALLY (BEFORE the generic
    ``Exception`` quarantine path) and routes through the token-loss
    restart protocol:

    1. Quarantine the partial response (``cause="token_exhausted"``) to
       ``<run_dir>/quarantine.jsonl``.
    2. Restart the lane ONCE with a fresh ``lane_id`` (same role, same
       upstream visibility).
    3. If the restart also raises :class:`TokenExhaustedError`, the
       SwarmRun ends with ``status="failed"``,
       ``reason_code="TOKEN_BUDGET_EXCEEDED"``, and a BLOCKED transition
       receipt is emitted.

    The optional ``tokens_consumed`` field carries the caller's own token
    counter for diagnostics; it is not used by the runtime (the runtime
    does not count tokens itself -- the caller's invoker is the
    authoritative budget enforcer).
    """

    def __init__(
        self,
        message: str = "",
        *,
        tokens_consumed: int | None = None,
    ) -> None:
        super().__init__(message)
        self.tokens_consumed = tokens_consumed


def run_swarm_lane(
    scenario: str,
    store: LaneStore,
    lease_manager: LeaseManager,
    *,
    run_id: UUID | None = None,
    holder_id: str | None = None,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    on_role_invoke: RoleInvoker | None = None,
    metadata: dict[str, str] | None = None,
    ladder: dict[Role, LaneRoleLadder] | None = None,
    swap_percent_provider: Callable[[], float] | None = None,
) -> SwarmRun:
    """Drive a single :class:`SwarmRun` through the hardcoded role chain.

    Parameters
    ----------
    scenario
        Free-text scenario description. Stored on the ``SwarmRun.metadata``
        as ``"scenario": <str>`` so the receipt chain captures what was run.
    store
        The shared :class:`LaneStore` (SQLite + JSONL backing).
    lease_manager
        :class:`LeaseManager` wired to the same ``store``.
    run_id
        Override the auto-generated run id (useful for tests / resume).
    holder_id
        Override the auto-generated holder id. Defaults to ``"runtime-<rid8>"``.
    ttl_seconds
        Lease TTL. Defaults to :data:`DEFAULT_LEASE_TTL_SECONDS` (60s).
    on_role_invoke
        Callback ``(role, lane_run) -> LaneOutputBase``. If ``None``, the
        orchestrator emits a typed empty output for each role.
    metadata
        gsd v2.3 control-plane contract slot. Merged with the scenario tag.
    ladder
        Phase 68 per-call routing override. ``None`` -> use
        :data:`DEFAULT_LANE_LADDER`. A partial dict overrides the default
        role-by-role (e.g., supply only ``{"executor": ...}`` to retarget
        the executor lane while keeping the default planner/reviewer/synth
        ladders).
    swap_percent_provider
        Phase 68 zero-arg callable returning current swap usage percent.
        ``None`` -> ``lambda: 0.0`` (treat host as healthy; matches the
        Phase 66/67 baseline so existing callers see identical behavior).
        Operators wire :func:`ollarma.swarm.lane.swap.live_swap_percent_provider`
        explicitly when they want live measurement.

    Returns
    -------
    SwarmRun
        Final :class:`SwarmRun` record (also persisted via ``store.upsert_run``).
        On the happy path: ``status="completed"``, ``reason_code=None``.
        On failure: ``status="failed"`` or ``"blocked"`` with a populated
        ``reason_code`` from the v5.1 lane-runtime extension set.
    """
    # 1. Generate identifiers + per-run dir + run-level metadata.
    run_id = run_id or uuid4()
    holder_id = holder_id or f"runtime-{run_id.hex[:8]}"
    requested_at = datetime.now(timezone.utc)
    merged_metadata: dict[str, str] = {"scenario": scenario}
    if metadata:
        merged_metadata.update(metadata)
    run_dir = store.run_dir_for(run_id)

    # 2. Try to acquire the lease.
    decision = lease_manager.acquire(run_id, holder_id, ttl_seconds)
    if not decision.acquired:
        run = SwarmRun(
            run_id=run_id,
            requested_at=requested_at,
            status="blocked",
            reason_code="LEASE_HELD_BY_OTHER",
            metadata=merged_metadata,
        )
        store.upsert_run(run)
        _emit_blocked_transition(
            store, run_dir, run_id, "LEASE_HELD_BY_OTHER", parent_hash=None,
        )
        return run

    # 3. Mark in_progress.
    run = SwarmRun(
        run_id=run_id,
        requested_at=requested_at,
        status="in_progress",
        metadata=merged_metadata,
    )
    store.upsert_run(run)

    # 4. Drive the chain (Phase 66 + Phase 67 checkpoint emission +
    #    Phase 68 per-lane routing decision).
    effective_ladder = _merge_ladder(ladder)
    swap_provider = swap_percent_provider or (lambda: 0.0)
    try:
        return _drive_chain_from(
            run=run,
            start_index=0,
            upstream=[],
            parent_hash=None,
            store=store,
            run_dir=run_dir,
            holder_id=holder_id,
            on_role_invoke=on_role_invoke,
            ladder=effective_ladder,
            swap_percent_provider=swap_provider,
        )
    finally:
        # Always release lease (best-effort; returns False if we lost it).
        lease_manager.release(run_id, holder_id)


# ---------------------------------------------------------------------------
# Phase 67: resume orchestrator
# ---------------------------------------------------------------------------

def resume_swarm_lane(
    run_id: UUID,
    store: LaneStore,
    lease_manager: LeaseManager,
    *,
    holder_id: str | None = None,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    on_role_invoke: RoleInvoker | None = None,
    swap_percent_provider: Callable[[], float] | None = None,
    ladder: dict[Role, LaneRoleLadder] | None = None,
) -> SwarmRun:
    """Resume a previously interrupted :class:`SwarmRun`.

    Resume detection criteria are caller-side: the typical caller has just
    selected this ``run_id`` from :meth:`LaneStore.list_resumable_runs`.

    Behavior
    --------
    1. **Idempotency:** if the SwarmRun row's ``status`` is already
       ``"completed"`` we return the existing run unchanged. If the row is
       missing OR its status is not in ``("in_progress", "failed")`` we
       raise :class:`ValueError` carrying the ``RESUME_NO_CANDIDATE`` reason
       code.
    2. **Swap-pressure check:** if the injected ``swap_percent_provider``
       returns >= :data:`SWAP_DEGRADED_PCT_THRESHOLD` (default 50) we
       refuse to resume -- emit BLOCKED with ``reason_code="SWAP_DEGRADED"``
       and return. (Phase 68 will add a routing-ladder rescue path.)
    3. **Re-acquire lease:** via :class:`LeaseManager`. If a different
       holder owns an unexpired lease we refuse -- emit BLOCKED with
       ``reason_code="LEASE_HELD_BY_OTHER"`` and return.
    4. **Determine resume point:** read ``<run_dir>/checkpoint.json`` for
       ``last_completed_role``. If the file is missing, walk
       ``lane_outputs/`` to infer the latest role with a persisted output.
    5. **Drive remaining chain:** from the next role after ``last_completed_role``,
       reusing the per-role loop logic from :func:`run_swarm_lane`. Each new
       lane gets a fresh ``lane_id``; transitions chain to the last
       pre-interruption transition's ``transition_hash`` via ``parent_hash``.
    6. **Always release lease** in the ``finally`` block.

    Parameters
    ----------
    run_id
        The run to resume. Resume preserves this id; only ``lane_id``\\s
        for new attempts are fresh.
    store
        Shared :class:`LaneStore`.
    lease_manager
        :class:`LeaseManager` wired to the same store.
    holder_id
        Override the auto-generated holder id. Defaults to
        ``"resume-<rid8>"``.
    ttl_seconds
        Lease TTL for the resume holder.
    on_role_invoke
        Same callback contract as :func:`run_swarm_lane`. ``None`` falls
        back to typed-empty outputs.
    swap_percent_provider
        Zero-argument callable returning the current swap-usage percent.
        Default ``None`` -> stub returning ``0.0`` (Phase 67 ships without
        psutil; Phase 68 wires the real provider). Used by the resume-time
        SWAP_DEGRADED guard (refuses resume entirely on degraded host) AND
        by the Phase 68 per-lane routing decision inside the chain driver.
    ladder
        Phase 68 per-call routing override (same shape / semantics as the
        ``ladder`` param on :func:`run_swarm_lane`). ``None`` -> use
        :data:`DEFAULT_LANE_LADDER`.

    Returns
    -------
    SwarmRun
        Final state of the run after the resume attempt. ``status`` is
        one of ``"completed"``, ``"failed"``, or ``"blocked"`` -- callers
        inspect ``reason_code`` for the failure mode.

    Raises
    ------
    ValueError
        If ``run_id`` has no SwarmRun row, OR if the existing row's status
        is not one of ``("in_progress", "failed", "completed")``. The
        message contains the literal token ``"RESUME_NO_CANDIDATE"`` for
        easy grepping.
    """
    holder_id = holder_id or f"resume-{run_id.hex[:8]}"
    effective_ladder = _merge_ladder(ladder)
    swap_provider = swap_percent_provider or (lambda: 0.0)

    # 1. Idempotency / candidate check.
    existing = store.get_run(run_id)
    if existing is None:
        raise ValueError(
            f"RESUME_NO_CANDIDATE: no SwarmRun row for run_id={run_id}"
        )
    if existing.status == "completed":
        return existing
    if existing.status not in ("in_progress", "failed"):
        raise ValueError(
            f"RESUME_NO_CANDIDATE: status={existing.status!r} for run_id={run_id}"
        )

    run_dir = store.run_dir_for(run_id)

    # 2. Swap-pressure check (BEFORE lease acquire -- don't take the lease
    #    just to immediately drop it).
    swap_pct = swap_provider()
    if swap_pct >= SWAP_DEGRADED_PCT_THRESHOLD:
        run = existing.model_copy(
            update={"status": "blocked", "reason_code": "SWAP_DEGRADED"},
        )
        store.upsert_run(run)
        _emit_blocked_transition(
            store, run_dir, run_id, "SWAP_DEGRADED",
            parent_hash=_last_parent_hash(store, run_dir),
        )
        return run

    # 3. Re-acquire lease.
    decision = lease_manager.acquire(run_id, holder_id, ttl_seconds)
    if not decision.acquired:
        run = existing.model_copy(
            update={"status": "blocked", "reason_code": "LEASE_HELD_BY_OTHER"},
        )
        store.upsert_run(run)
        _emit_blocked_transition(
            store, run_dir, run_id, "LEASE_HELD_BY_OTHER",
            parent_hash=_last_parent_hash(store, run_dir),
        )
        return run

    try:
        # 4. Determine resume point.
        try:
            checkpoint = store.load_checkpoint(run_dir)
        except LaneStoreError:
            # Corrupted checkpoint -- fall back to inferring from
            # lane_outputs/. (Mirrors list_resumable_runs' best-effort
            # tolerance.)
            checkpoint = None
        if checkpoint is not None:
            last_completed = checkpoint.last_completed_role
        else:
            last_completed = _infer_last_completed_role(run_dir)

        if last_completed is None:
            start_index = 0
        else:
            start_index = ROLE_CHAIN.index(last_completed) + 1

        # 5. Already-complete short-circuit (chain ran to synthesizer
        #    pre-interruption but the SwarmRun row never got marked).
        if start_index >= len(ROLE_CHAIN):
            run = existing.model_copy(
                update={"status": "completed", "reason_code": None},
            )
            store.upsert_run(run)
            return run

        # 6. Re-load upstream outputs from disk.
        upstream = _load_upstream_outputs(
            store, run_dir, ROLE_CHAIN[:start_index],
        )
        parent_hash = _last_parent_hash(store, run_dir)

        # 7. Mark in_progress (was 'failed' or already 'in_progress').
        run = existing.model_copy(
            update={"status": "in_progress", "reason_code": None},
        )
        store.upsert_run(run)

        # 8. Drive the remaining chain.
        return _drive_chain_from(
            run=run,
            start_index=start_index,
            upstream=upstream,
            parent_hash=parent_hash,
            store=store,
            run_dir=run_dir,
            holder_id=holder_id,
            on_role_invoke=on_role_invoke,
            ladder=effective_ladder,
            swap_percent_provider=swap_provider,
        )
    finally:
        lease_manager.release(run_id, holder_id)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _drive_chain_from(
    *,
    run: SwarmRun,
    start_index: int,
    upstream: list[LaneOutputBase],
    parent_hash: str | None,
    store: LaneStore,
    run_dir: Path,
    holder_id: str,
    on_role_invoke: RoleInvoker | None,
    ladder: dict[Role, LaneRoleLadder],
    swap_percent_provider: Callable[[], float],
) -> SwarmRun:
    """Per-role loop shared by :func:`run_swarm_lane` and :func:`resume_swarm_lane`.

    The caller owns lease acquisition and lease release; this function just
    runs the chain from ``ROLE_CHAIN[start_index:]`` with the supplied
    upstream-output and parent-hash continuation, persists each lane output,
    appends each transition, and emits a Checkpoint (Phase 67) after every
    completed lane.

    Phase 68: BEFORE each lane invocation, :func:`select_lane_model` is
    consulted with the per-role ladder + a fresh ``swap_percent_provider()``
    reading. The chosen model name + outcome + measured swap percent are
    recorded on the per-lane :attr:`LaneRun.routing_decision` so the
    invoker can read them; the outcome is also propagated onto the
    persisted :class:`LaneOutputBase` and the emitted
    :class:`LaneTransition`. ``outcome == "blocked_escalate"`` short-circuits
    the chain (refuse to invoke; emit BLOCKED transition with
    ``outcome="blocked_escalate"``; return ``status="blocked"``,
    ``reason_code="ROUTING_BLOCKED_ESCALATE"``).
    """
    run_id = run.run_id
    upstream = list(upstream)  # local mutable copy

    for role in ROLE_CHAIN[start_index:]:
        # Lease-check before every lane (Phase 66 deviation 5).
        current_lease = store.get_lease(run_id)
        if current_lease is None or current_lease[1] != holder_id:
            run = run.model_copy(
                update={"status": "failed", "reason_code": "LEASE_EXPIRED"},
            )
            store.upsert_run(run)
            _emit_blocked_transition(
                store, run_dir, run_id, "LEASE_EXPIRED",
                parent_hash=parent_hash,
            )
            return run

        # Lease expiry check (TTL fired even though row is still ours).
        _, _, current_expires = current_lease
        current_expires_aware = (
            current_expires
            if current_expires.tzinfo is not None
            else current_expires.replace(tzinfo=timezone.utc)
        )
        if current_expires_aware < datetime.now(timezone.utc):
            run = run.model_copy(
                update={"status": "failed", "reason_code": "LEASE_EXPIRED"},
            )
            store.upsert_run(run)
            _emit_blocked_transition(
                store, run_dir, run_id, "LEASE_EXPIRED",
                parent_hash=parent_hash,
            )
            return run

        # Phase 68: per-lane routing decision. Pure-function policy --
        # any error inside the (operator-supplied) provider is the
        # provider's responsibility (live_swap_percent_provider fails OPEN
        # to 0.0 by contract).
        swap_pct = swap_percent_provider()
        ladder_for_role = ladder[role]
        model_name, outcome = select_lane_model(role, ladder_for_role, swap_pct)

        if outcome == "blocked_escalate":
            run = run.model_copy(
                update={
                    "status": "blocked",
                    "reason_code": "ROUTING_BLOCKED_ESCALATE",
                },
            )
            store.upsert_run(run)
            _emit_blocked_transition(
                store, run_dir, run_id, "ROUTING_BLOCKED_ESCALATE",
                parent_hash=parent_hash,
                outcome="blocked_escalate",
            )
            return run

        lane_run = LaneRun(
            run_id=run_id,
            role=role,
            upstream_outputs=list(upstream),
            started_at=datetime.now(timezone.utc),
            routing_decision={
                "model": model_name,
                "outcome": outcome,
                "swap_pct_at_decision": f"{swap_pct:.2f}",
            },
        )

        # Phase 67: token-loss-aware invocation. On TokenExhaustedError the
        # wrapper restarts the lane ONCE with a fresh lane_id; second TE ->
        # TOKEN_BUDGET_EXCEEDED. On non-TE exception the wrapper delegates
        # to _invoke_with_retry (1-retry-then-quarantine -> LANE_FAILED).
        output, failure_reason = _invoke_with_token_loss_handling(
            on_role_invoke, role, lane_run, run_dir,
        )
        if output is None:
            assert failure_reason is not None
            run = run.model_copy(
                update={"status": "failed", "reason_code": failure_reason},
            )
            store.upsert_run(run)
            # outcome stays "preferred" (Phase 67 default): the lane never
            # got past the routing decision into a real invocation, so the
            # routing-blocked outcome would be a misleading label here.
            _emit_blocked_transition(
                store, run_dir, run_id, failure_reason,
                parent_hash=parent_hash,
            )
            return run

        # Phase 68: stamp the routing outcome onto the persisted output so
        # the receipt chain + the output JSON both record the same decision.
        output = output.model_copy(update={"outcome": outcome})

        # Persist the lane output (atomic JSON write).
        store.write_lane_output(output, run_dir)

        # Build + append the transition receipt. The store computes
        # transition_hash itself (validates parent_hash against the
        # on-disk tail) and returns the materialized receipt; we use
        # its transition_hash as the next parent. ``outcome`` mirrors
        # the LaneOutputBase.outcome so the receipt chain alone tells the
        # routing history.
        output_content_hash = canonical_hash(output.model_dump(mode="json"))
        transition_in = LaneTransition(
            run_id=run_id,
            lane_id=output.lane_id,
            from_role=upstream[-1].role if upstream else None,
            to_role=role,
            output_content_hash=output_content_hash,
            parent_hash=parent_hash,
            transition_hash="",
            timestamp=datetime.now(timezone.utc),
            outcome=outcome,
        )
        materialized = store.append_transition(transition_in, run_dir)
        parent_hash = materialized.transition_hash
        upstream.append(output)

        # Phase 67: emit checkpoint AFTER the transition + output are durable.
        # Order matters -- if we crash between write_lane_output and
        # write_checkpoint, the next resume will fall back to walking
        # lane_outputs/ and still see this lane as completed.
        checkpoint = Checkpoint(
            run_id=run_id,
            last_completed_role=role,
            holder_id=holder_id,
            last_checkpoint_at=datetime.now(timezone.utc),
        )
        store.write_checkpoint(checkpoint, run_dir)

    # Mark completed.
    run = run.model_copy(update={"status": "completed", "reason_code": None})
    store.upsert_run(run)
    return run


def _invoke_with_token_loss_handling(
    invoker: RoleInvoker | None,
    role: Role,
    lane_run: LaneRun,
    run_dir: Path,
) -> tuple[LaneOutputBase | None, str | None]:
    """Phase 67: token-loss-aware wrapper around :func:`_invoke_with_retry`.

    Returns
    -------
    (output, failure_reason)
        ``(output, None)`` on success. ``(None, "TOKEN_BUDGET_EXCEEDED")``
        when two successive :class:`TokenExhaustedError`\\s are raised
        (1 restart with fresh ``lane_id`` then give up). ``(None, "LANE_FAILED")``
        when the underlying :func:`_invoke_with_retry` quarantines (non-TE
        exception path). ``(None, "LANE_FAILED")`` is also returned when
        ``invoker`` is ``None`` and somehow returns ``None`` (defensive --
        shouldn't happen since the no-invoker fallback emits typed-empty
        outputs).
    """
    if invoker is None:
        # No-invoker fallback path: defer entirely to _invoke_with_retry,
        # which emits a typed-empty output. No TokenExhaustedError surface.
        output = _invoke_with_retry(invoker, role, lane_run, run_dir)
        return (output, None) if output is not None else (None, "LANE_FAILED")

    current_lane_run = lane_run
    for attempt in (1, 2):
        try:
            output = _invoke_with_retry(invoker, role, current_lane_run, run_dir)
        except TokenExhaustedError as exc:
            # Quarantine the partial with explicit cause -- distinct from
            # the generic LANE_FAILED quarantine path.
            _quarantine(
                run_dir, current_lane_run, role, exc,
                cause="token_exhausted",
            )
            if attempt == 1:
                # Restart the lane: fresh lane_id, same upstream visibility.
                current_lane_run = current_lane_run.model_copy(
                    update={"lane_id": uuid4()},
                )
                continue
            # Second token-exhaustion in the same role -> token budget
            # genuinely exceeded. Caller turns this into a SwarmRun.failed
            # with reason_code=TOKEN_BUDGET_EXCEEDED.
            return (None, "TOKEN_BUDGET_EXCEEDED")

        # _invoke_with_retry returns None when its own 1-retry-then-quarantine
        # path was exhausted (already wrote the quarantine record).
        if output is None:
            return (None, "LANE_FAILED")
        return (output, None)

    # Defensive -- the loop returns on every attempt-2 path.
    return (None, "LANE_FAILED")


def _invoke_with_retry(
    invoker: RoleInvoker | None,
    role: Role,
    lane_run: LaneRun,
    run_dir: Path,
) -> LaneOutputBase | None:
    """Call ``invoker(role, lane_run)``; on exception, retry once; on second
    failure, quarantine + return ``None``.

    When ``invoker`` is ``None``, return a typed empty output (the
    no-invoker fallback used by the smoke tests in plan 66-03).

    Phase 67: :class:`TokenExhaustedError` is RE-RAISED unchanged so the
    surrounding :func:`_invoke_with_token_loss_handling` wrapper can apply
    the special restart-with-fresh-lane-id semantics. All other exceptions
    follow the original 1-retry-then-quarantine path.
    """
    if invoker is None:
        cls = ROLE_OUTPUT_MAP[role]
        return cls(  # type: ignore[arg-type]
            run_id=lane_run.run_id,
            lane_id=lane_run.lane_id,
            **_default_role_fields(role),
        )

    expected = ROLE_OUTPUT_MAP[role]
    last_exc: BaseException | None = None
    for attempt in (1, 2):
        try:
            output = invoker(role, lane_run)
        except TokenExhaustedError:
            # Phase 67: surface to the token-loss wrapper unchanged.
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == 1:
                continue
            _quarantine(run_dir, lane_run, role, exc)
            return None

        # Type guard: must be the expected pydantic model for this role.
        if not isinstance(output, expected):
            err = TypeError(
                f"Role {role!r} invoker returned {type(output).__name__}, "
                f"expected {expected.__name__}"
            )
            last_exc = err
            if attempt == 1:
                continue
            _quarantine(run_dir, lane_run, role, err)
            return None

        return output

    # Defensive -- the loop returns on every path; only reachable if logic
    # is changed without updating this comment.
    if last_exc is not None:
        _quarantine(run_dir, lane_run, role, last_exc)
    return None


def _quarantine(
    run_dir: Path,
    lane_run: LaneRun,
    role: Role,
    exc: BaseException,
    *,
    cause: str = "lane_failed",
) -> None:
    """Append a quarantine record for a lane that failed.

    ``cause`` distinguishes the two Phase 66/67 failure modes:
    ``"lane_failed"`` (default; 1-retry-then-quarantine on generic
    exception) vs. ``"token_exhausted"`` (Phase 67 token-loss path). The
    field appears verbatim in ``quarantine.jsonl`` so post-mortem readers
    can split the two without re-running the chain.
    """
    quarantine_path = run_dir / "quarantine.jsonl"
    record = {
        "run_id": str(lane_run.run_id),
        "lane_id": str(lane_run.lane_id),
        "role": role,
        "cause": cause,
        "exception_type": type(exc).__name__,
        "exception_repr": repr(exc),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    line = orjson.dumps(record, option=orjson.OPT_SORT_KEYS)
    with quarantine_path.open("ab") as fh:
        fh.write(line)
        fh.write(b"\n")


def _default_role_fields(role: Role) -> dict[str, Any]:
    """Default pydantic field values for the no-invoker fallback."""
    if role == "planner":
        return {"plan_steps": [], "rationale": ""}
    if role == "executor":
        return {
            "actions_taken": [],
            "artifacts_written": [],
            "success": True,
            "error_message": None,
        }
    if role == "reviewer":
        return {"findings": [], "severity": "pass", "summary": ""}
    if role == "synthesizer":
        return {"final_summary": "", "decisions": [], "next_actions": []}
    raise ValueError(f"unknown role {role!r}")


def _emit_blocked_transition(
    store: LaneStore,
    run_dir: Path,
    run_id: UUID,
    reason_code: str,
    *,
    parent_hash: str | None,
    outcome: LaneOutcome = "preferred",
) -> None:
    """Emit a single BLOCKED transition for receipt-chain completeness on
    early-return paths.

    The transition uses a sentinel zero ``lane_id`` (no real lane ran)
    and ``output_content_hash`` derived from the reason_code so post-mortem
    readers can distinguish blocked-on-lease from blocked-on-lane-failure
    without consulting the SwarmRun row.

    The ``outcome`` kwarg defaults to ``"preferred"`` to preserve the
    Phase 66/67 receipt semantics: lease-blocked / lane-failed / token-
    exhausted / resume-time-SWAP_DEGRADED transitions never made it past
    the routing decision into a real invocation, so labeling them
    ``"blocked_escalate"`` would misrepresent why the lane stopped. Only
    the Phase 68 routing-blocked path passes
    ``outcome="blocked_escalate"`` explicitly.
    """
    blocked_payload = {"blocked_reason": reason_code, "run_id": str(run_id)}
    output_content_hash = canonical_hash(blocked_payload)
    transition_in = LaneTransition(
        run_id=run_id,
        lane_id=_BLOCKED_LANE_ID,
        from_role=None,
        to_role=None,
        output_content_hash=output_content_hash,
        parent_hash=parent_hash,
        transition_hash="",
        timestamp=datetime.now(timezone.utc),
        outcome=outcome,
    )
    store.append_transition(transition_in, run_dir)


# ---------------------------------------------------------------------------
# Phase 67: resume helpers
# ---------------------------------------------------------------------------

def _last_parent_hash(store: LaneStore, run_dir: Path) -> str | None:
    """Return the ``transition_hash`` of the most recent transition on disk.

    Used by :func:`resume_swarm_lane` so the first transition emitted in
    the resumed chain links cleanly to the pre-interruption tail. Returns
    ``None`` for an empty / missing transitions file (the chain starts
    fresh).
    """
    transitions = store.load_transitions(run_dir)
    if not transitions:
        return None
    return transitions[-1].transition_hash


def _infer_last_completed_role(run_dir: Path) -> Role | None:
    """Walk ``lane_outputs/`` to determine the latest role with a persisted output.

    Fallback for the case where ``checkpoint.json`` is missing or
    corrupted. We scan each output JSON, read its ``role`` field, and
    return the role with the highest index in :data:`ROLE_CHAIN` that has
    at least one output on disk.

    Returns ``None`` when no outputs exist (the chain hasn't started or
    crashed before the first lane completed).
    """
    outputs_dir = run_dir / "lane_outputs"
    if not outputs_dir.exists():
        return None
    seen_roles: set[Role] = set()
    for path in outputs_dir.glob("*.json"):
        try:
            payload = orjson.loads(path.read_bytes())
        except (OSError, orjson.JSONDecodeError):
            continue
        role = payload.get("role")
        if role in ROLE_CHAIN:
            seen_roles.add(role)  # type: ignore[arg-type]
    if not seen_roles:
        return None
    # Return the highest-index role we observed.
    for role in reversed(ROLE_CHAIN):
        if role in seen_roles:
            return role
    return None


def _load_upstream_outputs(
    store: LaneStore,
    run_dir: Path,
    completed_roles: tuple[Role, ...],
) -> list[LaneOutputBase]:
    """Re-materialize the typed lane outputs for the already-completed roles.

    Walks ``lane_transitions.jsonl`` to discover the ``lane_id`` for each
    already-completed role (the transitions carry the canonical ordering;
    ``lane_outputs/*.json`` filenames don't), then loads each output JSON
    and validates it against the role's pydantic class via
    :data:`ROLE_OUTPUT_MAP`.

    Returns an empty list when ``completed_roles`` is empty (resuming from
    the planner). Raises :class:`LaneStoreError` if a referenced output
    file is missing or its payload doesn't match the expected role schema --
    a corrupted run dir is not silently papered over.
    """
    if not completed_roles:
        return []
    transitions = store.load_transitions(run_dir)
    # Build role -> lane_id map by walking transitions in order. Skip the
    # BLOCKED sentinel lane_id (zero UUID) -- those receipts don't have an
    # output on disk.
    role_to_lane: dict[Role, UUID] = {}
    for tr in transitions:
        if tr.lane_id == _BLOCKED_LANE_ID:
            continue
        if tr.to_role is not None and tr.to_role in completed_roles:
            role_to_lane[tr.to_role] = tr.lane_id

    outputs: list[LaneOutputBase] = []
    for role in completed_roles:
        lane_id = role_to_lane.get(role)
        if lane_id is None:
            raise LaneStoreError(
                f"resume: no transition for completed role {role!r} "
                f"in {run_dir / 'lane_transitions.jsonl'}"
            )
        payload = store.load_lane_output(run_dir, lane_id)
        if payload is None:
            raise LaneStoreError(
                f"resume: lane output for role {role!r} (lane_id={lane_id}) "
                f"missing from {run_dir / 'lane_outputs'}"
            )
        cls = ROLE_OUTPUT_MAP[role]
        try:
            outputs.append(cls.model_validate(payload))
        except Exception as exc:  # pydantic.ValidationError or similar
            raise LaneStoreError(
                f"resume: lane output for role {role!r} (lane_id={lane_id}) "
                f"failed schema validation: {exc}"
            ) from exc
    return outputs
