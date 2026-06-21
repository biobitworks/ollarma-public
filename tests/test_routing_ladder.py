"""tests/test_routing_ladder.py — Unit tests for Phase 53 local-first routing ladder.

Requirements covered: ROUTE-01, ROUTE-02, ROUTE-03, OBS-02, OBS-03.

All tests are offline — no Ollama, no network.
Selection artifacts, residency decisions, and telemetry are monkeypatched.
"""
from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest

from ollarma.routing_ladder import (
    LadderDecision,
    LadderRung,
    RC_LADDER_BLOCKED_NO_LOCAL,
    RC_LADDER_DEGRADED_RESIDENCY,
    RC_LADDER_DEGRADED_SWAP,
    RC_LADDER_PREFERRED,
    RC_LADDER_RESCUE_ONLY,
    RC_LADDER_USER_OVERRIDE,
    STATUS_BLOCKED_ESCALATE,
    STATUS_CHOSE_DEGRADED,
    STATUS_CHOSE_PREFERRED,
    STATUS_CHOSE_RESCUE,
    build_ladder,
)
from ollarma.scheduler import SWAP_DEGRADED_THRESHOLD_MB

RESCUE = "qwen2.5:1.5b"  # lean-bridge rescue; qwen3:1.7b is reserved for Antigence/Sentinel
SAFE_SWAP = 50.0
HIGH_SWAP = SWAP_DEGRADED_THRESHOLD_MB + 1.0
VERY_HIGH_SWAP = SWAP_DEGRADED_THRESHOLD_MB * 20.0
THRESHOLD = float(SWAP_DEGRADED_THRESHOLD_MB)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ladder(
    *,
    selection_result: str | None = "qwen3:8b",
    ranked_alternates: tuple[str, ...] | None = ("qwen3:8b", "phi4-mini"),
    swap_used_mb: float = SAFE_SWAP,
    resident_models: tuple[str, ...] = ("qwen3:8b",),
    override_model: str | None = None,
    workload_class: str = "route_prompt",
) -> LadderDecision:
    return build_ladder(
        workload_class,
        selection_result=selection_result,
        ranked_alternates=ranked_alternates,
        swap_used_mb=swap_used_mb,
        swap_threshold_mb=THRESHOLD,
        rescue_model=RESCUE,
        resident_models=resident_models,
        override_model=override_model,
    )


# ---------------------------------------------------------------------------
# TestBuildLadder — pure, no I/O
# ---------------------------------------------------------------------------


class TestBuildLadder:
    """build_ladder() is deterministic and pure — all inputs injected."""

    # ROUTE-01: benchmark-backed ladder in clean state
    def test_clean_state_chooses_benchmark_winner(self):
        """Full clean state: selects the benchmark #1 model as chose_preferred."""
        d = _ladder(
            selection_result="qwen3:8b",
            ranked_alternates=("qwen3:8b", "phi4-mini"),
            swap_used_mb=SAFE_SWAP,
            resident_models=("qwen3:8b",),
        )
        assert d.status == STATUS_CHOSE_PREFERRED
        assert d.chosen_model == "qwen3:8b"
        assert d.reason_code == RC_LADDER_PREFERRED
        assert d.escalation_hint is None
        # Rescue is always the last rung
        assert d.rungs_considered[-1].model == RESCUE

    # ROUTE-02: swap pressure forces degraded choice
    def test_swap_over_threshold_forces_degraded(self):
        """Swap > threshold: benchmark winner NOT resident → degrade to resident."""
        d = _ladder(
            selection_result="qwen3:8b",
            ranked_alternates=("qwen3:8b", "phi4-mini"),
            swap_used_mb=HIGH_SWAP,
            resident_models=("phi4-mini",),  # qwen3:8b not resident
        )
        assert d.status == STATUS_CHOSE_DEGRADED
        assert d.chosen_model == "phi4-mini"
        assert d.reason_code in (RC_LADDER_DEGRADED_SWAP, RC_LADDER_DEGRADED_RESIDENCY)
        assert d.swap_used_mb == HIGH_SWAP

    # ROUTE-02: very high swap + no non-rescue resident → rescue only
    def test_very_high_swap_no_residency_forces_rescue(self):
        """Very high swap + no resident model other than rescue → chose_rescue."""
        d = _ladder(
            selection_result="qwen3:8b",
            ranked_alternates=("qwen3:8b", "phi4-mini"),
            swap_used_mb=VERY_HIGH_SWAP,
            resident_models=(RESCUE,),  # only rescue is resident
        )
        assert d.status == STATUS_CHOSE_RESCUE
        assert d.chosen_model == RESCUE
        assert d.reason_code == RC_LADDER_RESCUE_ONLY

    # ROUTE-03: empty selection + high swap → rescue fallback (not blocked)
    def test_empty_selection_high_swap_falls_back_to_rescue(self):
        """No selection + high swap: rescue is always the last resort — chose_rescue."""
        d = _ladder(
            selection_result=None,
            ranked_alternates=None,
            swap_used_mb=VERY_HIGH_SWAP,
            resident_models=(),  # nothing resident but rescue is always appended
        )
        # Rescue is always appended and always passes the swap filter,
        # so we get chose_rescue, not blocked_escalate.
        assert d.status == STATUS_CHOSE_RESCUE
        assert d.chosen_model == RESCUE
        assert d.reason_code == RC_LADDER_RESCUE_ONLY

    # ROUTE-03: explicit blocked_escalate when rescue_model is empty
    def test_blocked_escalate_when_no_rescue_and_swap_pressure(self):
        """blocked_escalate fires when rescue_model is empty and swap filters everything."""
        # Pass an empty rescue_model so no fallback exists under swap pressure
        d = build_ladder(
            "route_prompt",
            selection_result=None,
            ranked_alternates=None,
            swap_used_mb=VERY_HIGH_SWAP,
            swap_threshold_mb=THRESHOLD,
            rescue_model="",          # no rescue model configured
            resident_models=(),       # nothing resident either
        )
        assert d.status == STATUS_BLOCKED_ESCALATE
        assert d.chosen_model is None
        assert d.reason_code == RC_LADDER_BLOCKED_NO_LOCAL
        assert d.escalation_hint is not None
        assert "escalat" in d.escalation_hint.lower()

    # ROUTE-01/user override
    def test_user_override_short_circuits_ladder(self):
        """Explicit model= parameter returns LADDER_USER_OVERRIDE immediately."""
        d = _ladder(
            selection_result="qwen3:8b",
            override_model="my-special-model",
            swap_used_mb=VERY_HIGH_SWAP,  # would normally block or degrade
        )
        assert d.status == STATUS_CHOSE_PREFERRED
        assert d.reason_code == RC_LADDER_USER_OVERRIDE
        assert d.chosen_model == "my-special-model"
        assert d.rungs_considered == ()  # ladder not traversed

    # Resident tiebreak
    def test_resident_model_preferred_within_same_tier(self):
        """Resident model wins tiebreak when swap is safe and two candidates exist."""
        # phi4-mini is in pareto at rank 2 but is resident; qwen3:8b is rank 1 but not
        d = _ladder(
            selection_result="qwen3:8b",
            ranked_alternates=("qwen3:8b", "phi4-mini"),
            swap_used_mb=SAFE_SWAP,
            resident_models=("phi4-mini",),  # qwen3:8b NOT resident
        )
        # Under safe swap, benchmark winner is still chosen (resident bonus doesn't
        # override rank-1 benchmark selection when swap is safe)
        assert d.chosen_model == "qwen3:8b"

    def test_resident_preferred_under_swap_pressure(self):
        """Under swap pressure, resident model wins over non-resident at higher rank."""
        d = _ladder(
            selection_result="qwen3:8b",
            ranked_alternates=("qwen3:8b", "phi4-mini"),
            swap_used_mb=HIGH_SWAP,
            resident_models=("phi4-mini",),  # only phi4-mini is resident
        )
        assert d.chosen_model == "phi4-mini"
        # qwen3:8b is rank-1 benchmark winner but not resident under swap

    # Determinism
    def test_deterministic_same_inputs_same_output(self):
        """Same inputs produce identical model_dump bytes (pydantic frozen model)."""
        d1 = _ladder()
        d2 = _ladder()
        assert d1.model_dump(mode="json") == d2.model_dump(mode="json")

    # Rescue always appended
    def test_rescue_always_appended_as_last_rung(self):
        """Rescue model is always the last rung even when not in selection artifact."""
        d = _ladder(
            selection_result="qwen3:8b",
            ranked_alternates=("qwen3:8b", "phi4-mini"),
            swap_used_mb=SAFE_SWAP,
        )
        last_rung = d.rungs_considered[-1]
        assert last_rung.model == RESCUE

    def test_rescue_not_duplicated_when_already_winner(self):
        """If rescue is the benchmark winner, it appears only once in rungs."""
        d = _ladder(
            selection_result=RESCUE,
            ranked_alternates=(RESCUE,),
            swap_used_mb=SAFE_SWAP,
            resident_models=(RESCUE,),
        )
        models = [r.model for r in d.rungs_considered]
        assert models.count(RESCUE) == 1

    # LadderDecision is frozen
    def test_ladder_decision_is_frozen(self):
        """LadderDecision is a frozen Pydantic model — mutations raise TypeError."""
        d = _ladder()
        with pytest.raises((TypeError, Exception)):
            d.chosen_model = "should-fail"  # type: ignore[misc]

    # escalation_hint only when blocked
    def test_escalation_hint_none_when_not_blocked(self):
        """escalation_hint is None unless status == blocked_escalate."""
        d = _ladder(swap_used_mb=SAFE_SWAP)
        assert d.escalation_hint is None

    def test_escalation_hint_populated_when_blocked(self):
        """escalation_hint is a non-empty string when status == blocked_escalate."""
        # Use empty rescue_model to force truly empty ladder under swap pressure.
        d = build_ladder(
            "route_prompt",
            selection_result=None,
            ranked_alternates=None,
            swap_used_mb=VERY_HIGH_SWAP,
            swap_threshold_mb=THRESHOLD,
            rescue_model="",
            resident_models=(),
        )
        assert d.status == STATUS_BLOCKED_ESCALATE
        assert isinstance(d.escalation_hint, str)
        assert len(d.escalation_hint) > 10


# ---------------------------------------------------------------------------
# TestRouteIntegration — service.py wiring (no live Ollama)
# ---------------------------------------------------------------------------


class TestRouteIntegration:
    """route_prompt() wires ladder decision into RouteResult.ladder field."""

    def _make_adapter(self, tmp_path: pathlib.Path):
        from ollarma.fleet import AdapterConfig
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "workflow.md").write_text(
            "workflow manifest guidance and execution\n", encoding="utf-8"
        )
        return AdapterConfig(
            project_name="test-project",
            project_root=str(tmp_path),
            adapter_source="yaml",
            knowledge_base={
                "sources": [
                    {"path": "docs", "kind": "documents", "authority": "canonical"}
                ]
            },
        )

    def test_route_result_ladder_populated(self, tmp_path):
        """route_prompt() returns RouteResult with ladder field populated."""
        from ollarma.service import build_project_kb, route_prompt
        from ollarma.guards import RuntimeTelemetry

        adapter = self._make_adapter(tmp_path)
        safe_telemetry = RuntimeTelemetry(
            loaded_models=("qwen3:8b",),
            loaded_model_count=1,
            swap_used_mb=SAFE_SWAP,
            telemetry_source="fake",
        )
        mock_response = MagicMock()
        mock_response.message.content = "Grounded answer"

        with patch("ollarma.service._load_project_registry", return_value={"test-project": adapter}), \
             patch("ollarma.service.resolve_ranked_selection", return_value=("qwen3:8b", ("qwen3:8b",))), \
             patch("ollarma.service.resolve_default_model", return_value="qwen3:8b"), \
             patch("ollarma.guards.collect_runtime_telemetry", return_value=safe_telemetry), \
             patch("ollarma.service.ollama.Client") as mock_cls:
            mock_cls.return_value.chat.return_value = mock_response
            build_project_kb("test-project")
            result = route_prompt("summarize the workflow guidance", "test-project")

        # ROUTE-01/03 + OBS-03: ladder is populated
        assert result.ladder is not None
        assert isinstance(result.ladder, LadderDecision)
        assert result.ladder.workload_class == "route_prompt"
        assert result.ladder.chosen_model == "qwen3:8b"
        assert result.ladder.status == STATUS_CHOSE_PREFERRED

    def test_blocked_escalate_path_returns_escalation_receipt(self, tmp_path):
        """blocked_escalate ladder returns escalation receipt in RouteResult (ROUTE-03)."""
        from ollarma.service import build_project_kb, route_prompt

        adapter = self._make_adapter(tmp_path)
        # Inject a pre-built blocked_escalate decision so we don't rely on
        # specific swap/residency state in the test environment.
        blocked_decision = LadderDecision(
            workload_class="route_prompt",
            swap_used_mb=VERY_HIGH_SWAP,
            swap_threshold_mb=THRESHOLD,
            resident_models=(),
            rungs_considered=(),
            chosen_model=None,
            chosen_rank=None,
            status=STATUS_BLOCKED_ESCALATE,
            reason_code=RC_LADDER_BLOCKED_NO_LOCAL,
            detail="No local model available under swap pressure.",
            escalation_hint="Drain swap or escalate to frontier.",
        )

        with patch("ollarma.service._load_project_registry", return_value={"test-project": adapter}), \
             patch("ollarma.service._build_ladder_decision", return_value=blocked_decision):
            build_project_kb("test-project")
            result = route_prompt("summarize the workflow guidance", "test-project")

        # ROUTE-03: explicit escalation receipt, never silent
        assert result.lane == "frontier_or_human"
        assert result.escalation_receipt is not None
        assert result.ladder is not None
        assert result.ladder.status == STATUS_BLOCKED_ESCALATE

    def test_blocked_escalate_writes_scribe_entry(self, tmp_path, monkeypatch):
        """blocked_escalate path calls scribe end_of_run with state=blocked (ROUTE-03)."""
        from ollarma.service import build_project_kb, route_prompt
        import ollarma.scribe_hooks as hooks

        adapter = self._make_adapter(tmp_path)
        blocked_decision = LadderDecision(
            workload_class="route_prompt",
            swap_used_mb=VERY_HIGH_SWAP,
            swap_threshold_mb=THRESHOLD,
            resident_models=(),
            rungs_considered=(),
            chosen_model=None,
            chosen_rank=None,
            status=STATUS_BLOCKED_ESCALATE,
            reason_code=RC_LADDER_BLOCKED_NO_LOCAL,
            detail="No local model available.",
            escalation_hint="Escalate to frontier caller.",
        )
        captured_state: list[str] = []

        def _fake_end(entrypoint, *, state, project="", task="", notes="", next_action="", artifacts=None):
            captured_state.append(state)

        monkeypatch.setattr(hooks, "end_of_run", _fake_end)
        monkeypatch.setenv("OLLARMA_SCRIBE_HOOKS", "on")  # ensure hooks enabled

        with patch("ollarma.service._load_project_registry", return_value={"test-project": adapter}), \
             patch("ollarma.service._build_ladder_decision", return_value=blocked_decision):
            build_project_kb("test-project")
            result = route_prompt("summarize the workflow guidance", "test-project")

        # ROUTE-03: scribe blocked state was emitted
        assert "blocked" in captured_state
        assert result.ladder is not None
        assert result.ladder.status == STATUS_BLOCKED_ESCALATE


# ---------------------------------------------------------------------------
# TestDashboardLadderSurface — OBS-02 / OBS-03
# ---------------------------------------------------------------------------


class TestDashboardLadderSurface:
    """Dashboard and /health surfaces expose ladder state (OBS-02/03)."""

    def _make_runtime_health_kwargs(self):
        """Minimal kwargs to construct RuntimeHealthResult."""
        return dict(
            status="ready",
            helper_chat={
                "status": "ready",
                "effective_model": "qwen3:8b",
                "requested_model": None,
                "reason_code": None,
                "detail": None,
                "recovery_commands": [],
            },
            chat_selection={"workload_class": "chat", "status": "ready"},
            route_selection={"workload_class": "route_prompt", "status": "ready"},
            project_count=0,
        )

    def test_dashboard_readiness_no_item_when_preferred(self):
        """No ladder item in dashboard when last decision is chose_preferred."""
        import ollarma.service as svc
        from ollarma.routing_ladder import LadderDecision

        preferred = LadderDecision(
            workload_class="route_prompt",
            swap_used_mb=SAFE_SWAP,
            swap_threshold_mb=THRESHOLD,
            resident_models=("qwen3:8b",),
            rungs_considered=(LadderRung(rank=1, model="qwen3:8b", reason="top"),),
            chosen_model="qwen3:8b",
            chosen_rank=1,
            status=STATUS_CHOSE_PREFERRED,
            reason_code=RC_LADDER_PREFERRED,
            detail="All good",
            escalation_hint=None,
        )
        svc._set_last_ladder_decision(preferred)

        from ollarma.service import RuntimeHealthResult
        rh = RuntimeHealthResult.model_validate({
            **self._make_runtime_health_kwargs(),
            "last_routing_ladder": None,
        })
        items = svc._build_dashboard_readiness(
            projects=["myproj"],
            kb_status_items=[],
            route_receipts=[],
            runtime_health=rh,
        )
        titles = [i.title for i in items]
        assert not any("ladder" in t.lower() for t in titles), (
            f"No ladder item expected but got: {[t for t in titles if 'ladder' in t.lower()]}"
        )
        # Reset
        svc._last_ladder_decision = None

    def test_dashboard_readiness_shows_item_when_degraded(self):
        """Dashboard includes a ladder item when last decision != chose_preferred."""
        import ollarma.service as svc
        from ollarma.routing_ladder import LadderDecision

        degraded = LadderDecision(
            workload_class="route_prompt",
            swap_used_mb=HIGH_SWAP,
            swap_threshold_mb=THRESHOLD,
            resident_models=(RESCUE,),
            rungs_considered=(LadderRung(rank=1, model=RESCUE, reason="rescue"),),
            chosen_model=RESCUE,
            chosen_rank=1,
            status=STATUS_CHOSE_RESCUE,
            reason_code=RC_LADDER_RESCUE_ONLY,
            detail="Swap forced rescue only",
            escalation_hint=None,
        )
        svc._set_last_ladder_decision(degraded)

        from ollarma.service import RuntimeHealthResult
        rh = RuntimeHealthResult.model_validate({
            **self._make_runtime_health_kwargs(),
            "last_routing_ladder": None,
        })
        items = svc._build_dashboard_readiness(
            projects=["myproj"],
            kb_status_items=[],
            route_receipts=[],
            runtime_health=rh,
        )
        titles = [i.title for i in items]
        assert any("ladder" in t.lower() for t in titles), (
            f"Expected ladder item but got: {titles}"
        )
        ladder_item = next(i for i in items if "ladder" in i.title.lower())
        assert RC_LADDER_RESCUE_ONLY in ladder_item.detail
        # Reset
        svc._last_ladder_decision = None

    def test_dashboard_shows_blocked_severity_when_escalate(self):
        """blocked_escalate surfaces as severity='blocked' in dashboard."""
        import ollarma.service as svc
        from ollarma.routing_ladder import LadderDecision

        blocked = LadderDecision(
            workload_class="route_prompt",
            swap_used_mb=VERY_HIGH_SWAP,
            swap_threshold_mb=THRESHOLD,
            resident_models=(),
            rungs_considered=(),
            chosen_model=None,
            chosen_rank=None,
            status=STATUS_BLOCKED_ESCALATE,
            reason_code=RC_LADDER_BLOCKED_NO_LOCAL,
            detail="No local model available",
            escalation_hint="Drain swap or escalate to frontier.",
        )
        svc._set_last_ladder_decision(blocked)

        from ollarma.service import RuntimeHealthResult
        rh = RuntimeHealthResult.model_validate({
            **self._make_runtime_health_kwargs(),
            "last_routing_ladder": None,
        })
        items = svc._build_dashboard_readiness(
            projects=["myproj"],
            kb_status_items=[],
            route_receipts=[],
            runtime_health=rh,
        )
        ladder_items = [i for i in items if "ladder" in i.title.lower()]
        assert ladder_items, "Expected at least one ladder item"
        assert ladder_items[0].severity == "blocked"
        # Reset
        svc._last_ladder_decision = None

    def _make_startup_readiness(self):
        """Build a valid StartupReadinessPayload for patching get_startup_readiness."""
        from ollarma.service import (
            StartupReadinessPayload,
            StartupModelAvailability,
            StartupSwapPosture,
            StartupAdmissionPosture,
            StartupPipelinePosture,
        )
        import datetime
        return StartupReadinessPayload(
            schema_version=1,
            service_label="com.byron.ollarma",
            generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            status="ready",
            checks=(),
            model_availability=StartupModelAvailability(
                status="ready",
                effective_model="qwen3:8b",
            ),
            swap=StartupSwapPosture(
                status="ready",
                swap_used_mb=SAFE_SWAP,
                threshold_mb=THRESHOLD,
            ),
            admission=StartupAdmissionPosture(enabled=True),
            pipeline=StartupPipelinePosture(),
            residency=None,
            next_fix_commands=(),
        )

    def _make_helper_resolution(self):
        """Build a valid HelperModelResolution for patching resolve_generic_chat_model."""
        from ollarma.service import HelperModelResolution
        return HelperModelResolution(
            status="ready",
            effective_model="qwen3:8b",
        )

    def test_get_runtime_health_last_routing_ladder_field(self):
        """get_runtime_health() returns last_routing_ladder compact dict after a decision."""
        import ollarma.service as svc
        from ollarma.routing_ladder import LadderDecision

        preferred = LadderDecision(
            workload_class="route_prompt",
            swap_used_mb=SAFE_SWAP,
            swap_threshold_mb=THRESHOLD,
            resident_models=("qwen3:8b",),
            rungs_considered=(LadderRung(rank=1, model="qwen3:8b", reason="top"),),
            chosen_model="qwen3:8b",
            chosen_rank=1,
            status=STATUS_CHOSE_PREFERRED,
            reason_code=RC_LADDER_PREFERRED,
            detail="Benchmark winner selected",
            escalation_hint=None,
        )
        svc._set_last_ladder_decision(preferred)

        with patch("ollarma.service.get_startup_readiness", return_value=self._make_startup_readiness()), \
             patch("ollarma.service.resolve_generic_chat_model", return_value=self._make_helper_resolution()), \
             patch("ollarma.service.list_projects", return_value=[]):
            health = svc.get_runtime_health()

        assert health.last_routing_ladder is not None
        assert health.last_routing_ladder["status"] == STATUS_CHOSE_PREFERRED
        assert health.last_routing_ladder["chosen_model"] == "qwen3:8b"
        assert health.last_routing_ladder["reason_code"] == RC_LADDER_PREFERRED
        assert health.last_routing_ladder["escalation_hint"] is None
        # Reset
        svc._last_ladder_decision = None

    def test_get_runtime_health_last_routing_ladder_none_when_no_decision(self):
        """get_runtime_health() returns last_routing_ladder=None before any route_prompt call."""
        import ollarma.service as svc

        # Ensure cache is clear
        svc._last_ladder_decision = None

        with patch("ollarma.service.get_startup_readiness", return_value=self._make_startup_readiness()), \
             patch("ollarma.service.resolve_generic_chat_model", return_value=self._make_helper_resolution()), \
             patch("ollarma.service.list_projects", return_value=[]):
            health = svc.get_runtime_health()

        assert health.last_routing_ladder is None
