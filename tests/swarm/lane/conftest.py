"""conftest.py -- shared fixtures for tests/swarm/lane/.

Provides:

* ``tmp_lane_store`` -- a pytest-tmp-path-backed :class:`LaneStore` so each
  test gets a fresh on-disk SQLite + runs/ tree with no real-filesystem
  dependence on ``.ollarma/swarm/``.
* ``tmp_lease_manager`` -- a :class:`LeaseManager` wired to ``tmp_lane_store``.
* ``canned_invoker`` -- a stub ``on_role_invoke(role, lane_run)`` that emits
  typed canned outputs for each of the four roles. No Ollama calls.
* ``raising_invoker`` -- a stub that raises on the executor role; used to
  exercise the 1-retry-then-quarantine path in ``runtime.py``.

Design notes
------------
* The lease-expiry test uses a brief real-time ``time.sleep(1.1)`` (rather
  than monkeypatching ``datetime.now``) because the production code reads
  the wall clock from ``datetime.now(timezone.utc)`` in BOTH ``lease.py``
  and ``store.py.upsert_lease``; monkeypatching one without the other
  produces an inconsistent virtual clock, and the marginal 1.1s wall buys
  honesty for ~ 5 LOC of complication. Total ``tests/swarm/lane/`` runtime
  budget per the plan is ~ 5 seconds; the 2 tests that sleep contribute
  ~ 2.2s, the rest run instantly. Documented choice: option A from plan.
* The concurrency test uses TWO separate :class:`LaneStore` instances
  pointing at the SAME ``base_dir`` so the atomicity boundary is the
  SQLite filesystem-level lock (not an in-process ``threading.Lock``).
  This more honestly probes UPSERT atomicity than reusing one store.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ollarma.swarm.lane import (
    LaneStore,
    LeaseManager,
    TokenExhaustedError,
    run_swarm_lane,  # noqa: F401 -- re-exported for tests
)
from ollarma.swarm.lane.schemas import (
    ExecutorOutput,
    LaneRun,
    PlannerOutput,
    ReviewerOutput,
    Role,
    SynthesizerOutput,
)


# ---------------------------------------------------------------------------
# Store + lease-manager fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_lane_store(tmp_path: Path) -> LaneStore:
    """Fresh ``LaneStore`` rooted at ``tmp_path / "swarm"``."""
    return LaneStore(base_dir=tmp_path / "swarm")


@pytest.fixture
def tmp_lease_manager(tmp_lane_store: LaneStore) -> LeaseManager:
    """``LeaseManager`` wired to the per-test ``tmp_lane_store``."""
    return LeaseManager(tmp_lane_store)


# ---------------------------------------------------------------------------
# Canned invoker -- happy-path stub for run_swarm_lane
# ---------------------------------------------------------------------------

@pytest.fixture
def canned_invoker() -> Any:
    """Return a stub ``on_role_invoke`` that emits typed canned outputs.

    The returned callable also exposes ``calls`` -- a list of dicts capturing
    ``(role, upstream_count)`` for each invocation so tests can assert the
    full-upstream-visibility contract (planner sees 0; executor 1; reviewer
    2; synthesizer 3).
    """

    calls: list[dict[str, Any]] = []

    def _invoke(role: Role, lane_run: LaneRun) -> Any:
        calls.append(
            {
                "role": role,
                "upstream_count": len(lane_run.upstream_outputs),
                "lane_id": lane_run.lane_id,
            }
        )
        if role == "planner":
            return PlannerOutput(
                run_id=lane_run.run_id,
                lane_id=lane_run.lane_id,
                plan_steps=["analyze", "draft", "review"],
                rationale="canned planner rationale",
            )
        if role == "executor":
            return ExecutorOutput(
                run_id=lane_run.run_id,
                lane_id=lane_run.lane_id,
                actions_taken=["wrote draft.md"],
                artifacts_written=["draft.md"],
                success=True,
            )
        if role == "reviewer":
            return ReviewerOutput(
                run_id=lane_run.run_id,
                lane_id=lane_run.lane_id,
                findings=["typo in section 2"],
                severity="warn",
                summary="minor issues",
            )
        if role == "synthesizer":
            return SynthesizerOutput(
                run_id=lane_run.run_id,
                lane_id=lane_run.lane_id,
                final_summary="canned synthesis",
                decisions=["ship the draft"],
                next_actions=["fix typo and re-review"],
            )
        raise ValueError(f"unknown role: {role!r}")

    # Attach call log to the function for test inspection.
    _invoke.calls = calls  # type: ignore[attr-defined]
    return _invoke


# ---------------------------------------------------------------------------
# Phase 68: swap-provider factory for parametrize-friendly stubbing
# ---------------------------------------------------------------------------

@pytest.fixture
def swap_provider_factory() -> Callable[[float], Callable[[], float]]:
    """Build a zero-arg swap_percent_provider that returns a fixed percent.

    Phase 68 routing-integration tests pass a swap percent through to
    ``select_lane_model``; this factory keeps the test parametrize shape
    minimal (just pass a float) while preserving the
    ``Callable[[], float]`` shape the runtime expects.

    Example::

        def test_routing_rescue_path(swap_provider_factory):
            provider = swap_provider_factory(60.0)
            run = run_swarm_lane(..., swap_percent_provider=provider)
    """

    def _build(percent: float) -> Callable[[], float]:
        return lambda: percent

    return _build


# ---------------------------------------------------------------------------
# Token-exhausting invoker -- factory exercising Phase 67 token-loss path
# ---------------------------------------------------------------------------

@pytest.fixture
def token_exhausting_invoker() -> Any:
    """Factory: build an invoker that raises ``TokenExhaustedError`` on a
    configurable role for a configurable number of attempts.

    Usage::

        invoker = token_exhausting_invoker(raise_on_role="executor", raise_count=1)

    The returned callable mirrors :func:`canned_invoker`: it emits typed
    canned outputs for every role except ``raise_on_role``, where it raises
    ``TokenExhaustedError`` for the first ``raise_count`` invocations and
    then falls through to a canned output on subsequent calls. Per-role
    attempt counts and the full call log are exposed on the invoker as
    ``.attempts`` and ``.calls`` for test inspection.
    """

    def _build(*, raise_on_role: Role = "executor", raise_count: int = 1) -> Any:
        attempts: dict[str, int] = {
            "planner": 0,
            "executor": 0,
            "reviewer": 0,
            "synthesizer": 0,
        }
        calls: list[dict[str, Any]] = []

        def _invoke(role: Role, lane_run: LaneRun) -> Any:
            attempts[role] += 1
            calls.append(
                {
                    "role": role,
                    "lane_id": lane_run.lane_id,
                    "attempt": attempts[role],
                }
            )
            if role == raise_on_role and attempts[role] <= raise_count:
                raise TokenExhaustedError(
                    f"budget exhausted on {role} attempt {attempts[role]}",
                    tokens_consumed=999,
                )
            if role == "planner":
                return PlannerOutput(
                    run_id=lane_run.run_id,
                    lane_id=lane_run.lane_id,
                    plan_steps=["x"],
                    rationale="ok",
                )
            if role == "executor":
                return ExecutorOutput(
                    run_id=lane_run.run_id,
                    lane_id=lane_run.lane_id,
                    actions_taken=["did the thing"],
                    artifacts_written=["out.txt"],
                    success=True,
                )
            if role == "reviewer":
                return ReviewerOutput(
                    run_id=lane_run.run_id,
                    lane_id=lane_run.lane_id,
                    findings=[],
                    severity="pass",
                    summary="ok",
                )
            if role == "synthesizer":
                return SynthesizerOutput(
                    run_id=lane_run.run_id,
                    lane_id=lane_run.lane_id,
                    final_summary="done",
                    decisions=[],
                    next_actions=[],
                )
            raise ValueError(f"unknown role: {role!r}")

        _invoke.attempts = attempts  # type: ignore[attr-defined]
        _invoke.calls = calls  # type: ignore[attr-defined]
        return _invoke

    return _build


# ---------------------------------------------------------------------------
# Raising invoker -- exercises 1-retry-then-quarantine on executor
# ---------------------------------------------------------------------------

@pytest.fixture
def raising_invoker() -> Any:
    """Return a stub that raises on every ``executor`` call.

    Planner returns a valid output; executor raises. The runtime should
    retry executor once, then quarantine it. Synth/reviewer never reached.
    """

    calls: list[dict[str, Any]] = []

    def _invoke(role: Role, lane_run: LaneRun) -> Any:
        calls.append({"role": role, "lane_id": lane_run.lane_id})
        if role == "executor":
            raise RuntimeError("simulated executor failure")
        if role == "planner":
            return PlannerOutput(
                run_id=lane_run.run_id,
                lane_id=lane_run.lane_id,
                plan_steps=["x"],
                rationale="ok",
            )
        # Defensive -- runtime should never reach reviewer/synthesizer
        # when executor quarantines. If it does, fail loudly.
        raise AssertionError(
            f"raising_invoker unexpectedly called for role {role!r}"
        )

    _invoke.calls = calls  # type: ignore[attr-defined]
    return _invoke
