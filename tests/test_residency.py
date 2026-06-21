"""tests/test_residency.py — Unit tests for Phase 52 GPU residency policy.

Requirements covered: GPU-01, GPU-02, GPU-03, GPU-04, OBS-01.

All tests are offline — no Ollama, no network.
PipelineController is always monkeypatched or instantiated with tmp_path.
"""
from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, call

import pytest

from ollarma.residency import (
    OPTIONAL_STRONGER,
    RESCUE_MODEL,
    ResidencyApplyReceipt,
    ResidencyDecision,
    apply,
    decide,
)
from ollarma.scheduler import SWAP_DEGRADED_THRESHOLD_MB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _telemetry(
    loaded: tuple[str, ...] = (),
    swap_mb: float | None = 50.0,
):
    """Build a minimal RuntimeTelemetry-like fake via the real class."""
    from ollarma.guards import RuntimeTelemetry

    return RuntimeTelemetry(
        loaded_models=loaded,
        loaded_model_count=len(loaded),
        size_vram_bytes=0,
        swap_used_mb=swap_mb,
        telemetry_source="fake",
    )


def _controller(
    *,
    pinned: tuple[str, ...] = (),
    benchmark_active: bool = False,
    tmp_path: pathlib.Path | None = None,
):
    """Create a PipelineController with stubbed state (no live Ollama)."""
    from ollarma.pipeline_control import PipelineController, ModelPinState
    import tempfile

    if tmp_path is None:
        tmp_path = pathlib.Path(tempfile.mkdtemp())
    ctrl = PipelineController(state_dir=tmp_path)
    # Inject pin state for any pinned models
    for name in pinned:
        ctrl._pin_state[name] = ModelPinState(pin_count=1)
    if benchmark_active:
        ctrl._benchmark_flag.touch()
    return ctrl


def _decision_kwargs(
    *,
    loaded: tuple[str, ...] = (),
    swap_mb: float | None = 50.0,
    benchmark_active: bool = False,
    pinned: tuple[str, ...] = (),
    tmp_path: pathlib.Path | None = None,
):
    return dict(
        telemetry=_telemetry(loaded=loaded, swap_mb=swap_mb),
        controller=_controller(pinned=pinned, benchmark_active=benchmark_active, tmp_path=tmp_path),
        benchmark_active=benchmark_active,
    )


# ---------------------------------------------------------------------------
# TestResidencyDecision — pure function, no I/O
# ---------------------------------------------------------------------------

class TestResidencyDecision:
    """decide() covers all 5 policy rules without side-effects."""

    def test_rescue_not_resident_safe_swap_pins_rescue(self, tmp_path):
        """Rule 2: rescue not loaded + safe swap → next_action=pin_rescue, state=degraded."""
        d = decide(**_decision_kwargs(loaded=(), swap_mb=50.0, tmp_path=tmp_path))
        assert d.next_action == "pin_rescue"
        assert d.state == "degraded"
        assert d.reason_code == "RESCUE_LOADING"
        assert not d.rescue_resident

    def test_swap_over_threshold_evicts_opportunistic(self, tmp_path):
        """Rule 3: swap > threshold AND opportunistic loaded → evict_opportunistic."""
        high_swap = SWAP_DEGRADED_THRESHOLD_MB + 1.0
        d = decide(**_decision_kwargs(
            loaded=(RESCUE_MODEL, OPTIONAL_STRONGER),
            swap_mb=high_swap,
            tmp_path=tmp_path,
        ))
        assert d.next_action == "evict_opportunistic"
        assert d.state == "degraded"
        assert d.reason_code == "SWAP_DEGRADED_DROP_OPPORTUNISTIC"

    def test_safe_swap_rescue_resident_warms_opportunistic(self, tmp_path):
        """Rule 4: swap safe, rescue loaded, opportunistic NOT loaded → warm_opportunistic."""
        d = decide(**_decision_kwargs(
            loaded=(RESCUE_MODEL,),
            swap_mb=50.0,
            tmp_path=tmp_path,
        ))
        assert d.next_action == "warm_opportunistic"
        assert d.state == "ready"
        assert d.rescue_resident

    def test_benchmark_active_freezes_all_mutations(self, tmp_path):
        """Rule 1: benchmark_active → blocked; next_action=blocked."""
        d = decide(**_decision_kwargs(
            loaded=(),
            swap_mb=50.0,
            benchmark_active=True,
            tmp_path=tmp_path,
        ))
        assert d.next_action == "blocked"
        assert d.state == "blocked"
        assert d.reason_code == "BENCHMARK_ACTIVE"

    def test_hold_when_both_models_resident(self, tmp_path):
        """Rule 5: rescue and opportunistic both loaded, safe swap → hold, ready."""
        d = decide(**_decision_kwargs(
            loaded=(RESCUE_MODEL, OPTIONAL_STRONGER),
            swap_mb=50.0,
            tmp_path=tmp_path,
        ))
        assert d.next_action == "hold"
        assert d.state == "ready"
        assert d.rescue_resident
        assert d.opportunistic_resident

    def test_swap_unknown_none_holds(self, tmp_path):
        """swap_mb=None (unknown) → rescue not resident triggers pin_rescue (swap unknown = safe side)."""
        # When swap is None, swap_over_threshold = False (None > threshold is False)
        # so if rescue not resident we go to pin_rescue
        d = decide(**_decision_kwargs(loaded=(), swap_mb=None, tmp_path=tmp_path))
        assert d.next_action == "pin_rescue"
        assert d.swap_used_mb is None

    def test_swap_unknown_none_with_rescue_resident_holds(self, tmp_path):
        """swap_mb=None, rescue resident, opportunistic not loaded → warm_opportunistic."""
        d = decide(**_decision_kwargs(loaded=(RESCUE_MODEL,), swap_mb=None, tmp_path=tmp_path))
        # None is not > threshold, so opportunistic warm is proposed
        assert d.next_action == "warm_opportunistic"

    def test_rescue_pinned_but_not_loaded_is_not_resident(self, tmp_path):
        """Pin state is not residency; rescue absent from telemetry must be warmed."""
        d = decide(**_decision_kwargs(
            loaded=(),          # not in loaded_models
            swap_mb=50.0,
            pinned=(RESCUE_MODEL,),   # pinned in controller state
            tmp_path=tmp_path,
        ))
        assert d.rescue_resident is False
        assert d.next_action == "pin_rescue"
        assert d.reason_code == "RESCUE_LOADING"

    def test_decision_is_frozen(self, tmp_path):
        """ResidencyDecision is immutable (frozen Pydantic model)."""
        d = decide(**_decision_kwargs(tmp_path=tmp_path))
        with pytest.raises(Exception):
            d.state = "ready"  # type: ignore[misc]

    def test_swap_high_rescue_not_resident_holds_degraded(self, tmp_path):
        """Swap > threshold, rescue not loaded: cannot safely pin → hold, degraded."""
        high_swap = SWAP_DEGRADED_THRESHOLD_MB + 1.0
        # opportunistic not loaded so Rule 3 doesn't fire
        d = decide(**_decision_kwargs(loaded=(), swap_mb=high_swap, tmp_path=tmp_path))
        # Rule 2 won't fire (swap_over_threshold), Rule 3 won't fire (opportunistic not resident)
        # Rule 4 won't fire (rescue not resident), falls through to Rule 5
        assert d.state == "degraded"
        assert d.next_action == "hold"
        assert d.reason_code == "RESCUE_BLOCKED_SWAP"

    def test_opportunistic_latest_runtime_name_is_detected(self, tmp_path):
        """Configured phi4-mini matches Ollama's phi4-mini:latest runtime name."""
        d = decide(**_decision_kwargs(
            loaded=("phi4-mini:latest",),
            swap_mb=50.0,
            tmp_path=tmp_path,
        ))

        assert d.opportunistic_resident is True
        assert d.next_action == "pin_rescue"


# ---------------------------------------------------------------------------
# TestResidencyApply — monkeypatched controller
# ---------------------------------------------------------------------------

class TestResidencyApply:
    """apply() calls exactly the right controller methods for each action."""

    def _mock_ctrl(self):
        ctrl = MagicMock()
        from ollarma.pipeline_control import PipelineReceipt
        receipt = PipelineReceipt.create("warmup", RESCUE_MODEL, None)
        ctrl._pin_state = {}
        ctrl.pin.return_value = receipt
        ctrl.warmup.return_value = receipt
        ctrl.evict.return_value = receipt
        return ctrl

    def test_pin_rescue_calls_warmup_then_pin(self):
        """pin_rescue action warms first, then pins only after the model is loaded."""
        decision = ResidencyDecision(
            state="degraded",
            rescue_target=RESCUE_MODEL,
            rescue_resident=False,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=False,
            swap_used_mb=50.0,
            reason_code="RESCUE_LOADING",
            next_action="pin_rescue",
        )
        ctrl = self._mock_ctrl()
        receipt = apply(decision, ctrl)
        assert ctrl.mock_calls[:2] == [call.warmup(RESCUE_MODEL, keep_alive=-1), call.pin(RESCUE_MODEL)]
        ctrl.pin.assert_called_once_with(RESCUE_MODEL)
        ctrl.warmup.assert_called_once_with(RESCUE_MODEL, keep_alive=-1)
        assert f"pin:{RESCUE_MODEL}" in receipt.actions_taken
        assert f"warmup:{RESCUE_MODEL}" in receipt.actions_taken
        assert not receipt.skipped_benchmark_active

    def test_pin_rescue_evicts_opportunistic_before_warmup(self):
        """If opportunistic is resident, free it before reloading the pinned rescue."""
        decision = ResidencyDecision(
            state="degraded",
            rescue_target=RESCUE_MODEL,
            rescue_resident=False,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=True,
            swap_used_mb=50.0,
            reason_code="RESCUE_LOADING",
            next_action="pin_rescue",
        )
        ctrl = self._mock_ctrl()
        receipt = apply(decision, ctrl)
        assert ctrl.mock_calls[:3] == [
            call.evict(OPTIONAL_STRONGER),
            call.warmup(RESCUE_MODEL, keep_alive=-1),
            call.pin(RESCUE_MODEL),
        ]
        assert receipt.actions_taken == (
            f"evict:{OPTIONAL_STRONGER}",
            f"warmup:{RESCUE_MODEL}",
            f"pin:{RESCUE_MODEL}",
        )

    def test_pin_rescue_warmup_failure_does_not_pin(self):
        """If rescue warmup fails, apply() records the error and does not create a stale pin."""
        decision = ResidencyDecision(
            state="degraded",
            rescue_target=RESCUE_MODEL,
            rescue_resident=False,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=False,
            swap_used_mb=50.0,
            reason_code="RESCUE_LOADING",
            next_action="pin_rescue",
        )
        ctrl = self._mock_ctrl()
        ctrl.warmup.side_effect = ValueError("SWAP_DEGRADED")
        receipt = apply(decision, ctrl)
        ctrl.warmup.assert_called_once_with(RESCUE_MODEL, keep_alive=-1)
        ctrl.pin.assert_not_called()
        assert receipt.actions_taken == ()
        assert len(receipt.errors) == 1
        assert "warmup_failed" in receipt.errors[0]

    def test_pin_rescue_does_not_increment_existing_pin(self):
        """A stale existing pin is preserved after successful warmup instead of incremented."""
        from ollarma.pipeline_control import ModelPinState

        decision = ResidencyDecision(
            state="degraded",
            rescue_target=RESCUE_MODEL,
            rescue_resident=False,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=False,
            swap_used_mb=50.0,
            reason_code="RESCUE_LOADING",
            next_action="pin_rescue",
        )
        ctrl = self._mock_ctrl()
        ctrl._pin_state = {RESCUE_MODEL: ModelPinState(pin_count=7)}
        receipt = apply(decision, ctrl)
        ctrl.warmup.assert_called_once_with(RESCUE_MODEL, keep_alive=-1)
        ctrl.pin.assert_not_called()
        assert receipt.actions_taken == (f"warmup:{RESCUE_MODEL}",)
        assert receipt.errors == ()

    def test_evict_opportunistic_calls_evict(self):
        """evict_opportunistic action → controller.evict() for opportunistic model."""
        decision = ResidencyDecision(
            state="degraded",
            rescue_target=RESCUE_MODEL,
            rescue_resident=True,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=True,
            swap_used_mb=600.0,
            reason_code="SWAP_DEGRADED_DROP_OPPORTUNISTIC",
            next_action="evict_opportunistic",
        )
        ctrl = self._mock_ctrl()
        receipt = apply(decision, ctrl)
        ctrl.evict.assert_called_once_with(OPTIONAL_STRONGER)
        ctrl.pin.assert_not_called()
        ctrl.warmup.assert_not_called()
        assert f"evict:{OPTIONAL_STRONGER}" in receipt.actions_taken

    def test_warm_opportunistic_calls_warmup(self):
        """warm_opportunistic action → controller.warmup() for opportunistic model only."""
        decision = ResidencyDecision(
            state="ready",
            rescue_target=RESCUE_MODEL,
            rescue_resident=True,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=False,
            swap_used_mb=50.0,
            reason_code=None,
            next_action="warm_opportunistic",
        )
        ctrl = self._mock_ctrl()
        receipt = apply(decision, ctrl)
        ctrl.warmup.assert_called_once_with(OPTIONAL_STRONGER)
        ctrl.pin.assert_not_called()
        ctrl.evict.assert_not_called()
        assert f"warmup:{OPTIONAL_STRONGER}" in receipt.actions_taken

    def test_hold_calls_no_controller_methods(self):
        """hold action → no pipeline mutations."""
        decision = ResidencyDecision(
            state="ready",
            rescue_target=RESCUE_MODEL,
            rescue_resident=True,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=True,
            swap_used_mb=50.0,
            reason_code=None,
            next_action="hold",
        )
        ctrl = self._mock_ctrl()
        receipt = apply(decision, ctrl)
        ctrl.pin.assert_not_called()
        ctrl.warmup.assert_not_called()
        ctrl.evict.assert_not_called()
        assert receipt.actions_taken == ()

    def test_blocked_skips_all_mutations(self):
        """blocked action (benchmark_active) → skipped_benchmark_active=True, no calls."""
        decision = ResidencyDecision(
            state="blocked",
            rescue_target=RESCUE_MODEL,
            rescue_resident=False,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=False,
            swap_used_mb=50.0,
            reason_code="BENCHMARK_ACTIVE",
            next_action="blocked",
        )
        ctrl = self._mock_ctrl()
        receipt = apply(decision, ctrl)
        ctrl.pin.assert_not_called()
        ctrl.warmup.assert_not_called()
        ctrl.evict.assert_not_called()
        assert receipt.skipped_benchmark_active is True

    def test_warmup_failure_recorded_not_raised(self):
        """If warmup fails, the error is recorded in receipt.errors (non-fatal)."""
        decision = ResidencyDecision(
            state="ready",
            rescue_target=RESCUE_MODEL,
            rescue_resident=True,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=False,
            swap_used_mb=50.0,
            reason_code=None,
            next_action="warm_opportunistic",
        )
        ctrl = self._mock_ctrl()
        ctrl.warmup.side_effect = ValueError("WARMUP_FAILED: connection error")
        receipt = apply(decision, ctrl)
        assert len(receipt.errors) >= 1
        assert "warm_opportunistic_failed" in receipt.errors[0]
        assert receipt.actions_taken == ()

    def test_evict_failure_recorded_not_raised(self):
        """If evict fails (e.g. model pinned), error is recorded in receipt.errors."""
        decision = ResidencyDecision(
            state="degraded",
            rescue_target=RESCUE_MODEL,
            rescue_resident=True,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=True,
            swap_used_mb=600.0,
            reason_code="SWAP_DEGRADED_DROP_OPPORTUNISTIC",
            next_action="evict_opportunistic",
        )
        ctrl = self._mock_ctrl()
        ctrl.evict.side_effect = ValueError("PIPELINE_MODEL_PINNED")
        receipt = apply(decision, ctrl)
        assert len(receipt.errors) >= 1
        assert "evict_failed" in receipt.errors[0]

    def test_receipt_is_frozen(self):
        """ResidencyApplyReceipt is immutable."""
        receipt = ResidencyApplyReceipt(
            decision_state="ready",
            next_action="hold",
            actions_taken=(),
            errors=(),
            skipped_benchmark_active=False,
        )
        with pytest.raises(Exception):
            receipt.decision_state = "blocked"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestResidencyServiceIntegration
# ---------------------------------------------------------------------------

class TestResidencyServiceIntegration:
    """service.get_residency_decision() returns a valid decision via monkeypatched probes."""

    def test_get_residency_decision_returns_decision(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        """get_residency_decision() returns a ResidencyDecision with expected fields."""
        from ollarma.guards import RuntimeTelemetry
        from ollarma import service

        fake_telemetry = RuntimeTelemetry(
            loaded_models=(RESCUE_MODEL,),
            loaded_model_count=1,
            size_vram_bytes=0,
            swap_used_mb=50.0,
            telemetry_source="fake",
        )
        monkeypatch.setattr(
            "ollarma.guards.collect_runtime_telemetry",
            lambda *a, **kw: fake_telemetry,
        )
        # Also patch the controller to avoid filesystem side-effects
        ctrl = _controller(pinned=(RESCUE_MODEL,), tmp_path=tmp_path)
        monkeypatch.setattr(
            "ollarma.pipeline_control.get_pipeline_controller",
            lambda *a, **kw: ctrl,
        )

        decision = service.get_residency_decision()

        assert isinstance(decision, ResidencyDecision)
        assert decision.rescue_target == RESCUE_MODEL
        assert decision.state in ("ready", "degraded", "blocked")
        assert decision.next_action in (
            "pin_rescue", "warm_opportunistic", "evict_opportunistic", "hold", "blocked"
        )

    def test_get_residency_decision_absorbs_telemetry_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        """If collect_runtime_telemetry raises, decision falls back to degraded hold."""
        from ollarma import service

        monkeypatch.setattr(
            "ollarma.guards.collect_runtime_telemetry",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("telemetry unavailable")),
        )
        ctrl = _controller(tmp_path=tmp_path)
        monkeypatch.setattr(
            "ollarma.pipeline_control.get_pipeline_controller",
            lambda *a, **kw: ctrl,
        )

        decision = service.get_residency_decision()
        # Should not raise; should return a valid decision
        assert isinstance(decision, ResidencyDecision)

    def test_apply_residency_policy_once_returns_receipt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        """apply_residency_policy_once() returns a ResidencyApplyReceipt."""
        from ollarma.guards import RuntimeTelemetry
        from ollarma import service

        fake_telemetry = RuntimeTelemetry(
            loaded_models=(RESCUE_MODEL, OPTIONAL_STRONGER),
            loaded_model_count=2,
            size_vram_bytes=0,
            swap_used_mb=50.0,
            telemetry_source="fake",
        )
        monkeypatch.setattr(
            "ollarma.guards.collect_runtime_telemetry",
            lambda *a, **kw: fake_telemetry,
        )
        ctrl = _controller(pinned=(RESCUE_MODEL,), tmp_path=tmp_path)
        monkeypatch.setattr(
            "ollarma.pipeline_control.get_pipeline_controller",
            lambda *a, **kw: ctrl,
        )

        receipt = service.apply_residency_policy_once()
        assert isinstance(receipt, ResidencyApplyReceipt)
        assert receipt.decision_state in ("ready", "degraded", "blocked")


# ---------------------------------------------------------------------------
# TestLifespanApplyOnce — lifespan calls apply_residency_policy_once once
# ---------------------------------------------------------------------------

class TestLifespanApplyOnce:
    """Starlette lifespan calls service.apply_residency_policy_once exactly once."""

    def test_lifespan_calls_apply_once(self, monkeypatch: pytest.MonkeyPatch):
        """Startup applies residency first, then refreshes readiness once."""
        from starlette.testclient import TestClient
        from ollarma import service

        call_log: list[str] = []

        def fake_apply():
            call_log.append("apply")
            return ResidencyApplyReceipt(
                decision_state="ready",
                next_action="hold",
                actions_taken=(),
                errors=(),
                skipped_benchmark_active=False,
            )

        def fake_refresh():
            call_log.append("refresh")

        monkeypatch.setattr(service, "refresh_startup_readiness", fake_refresh)
        monkeypatch.setattr(service, "apply_residency_policy_once", fake_apply)

        from ollarma.http_api import app
        with TestClient(app, raise_server_exceptions=False):
            pass  # lifespan runs on enter

        assert call_log == ["apply", "refresh"]

    def test_lifespan_apply_error_does_not_crash_app(self, monkeypatch: pytest.MonkeyPatch):
        """If apply_residency_policy_once raises, the app still starts."""
        from starlette.testclient import TestClient
        from ollarma import service

        def boom():
            raise RuntimeError("residency broken")

        monkeypatch.setattr(service, "refresh_startup_readiness", lambda: None)
        monkeypatch.setattr(service, "apply_residency_policy_once", boom)

        from ollarma.http_api import app
        # Should not raise — lifespan swallows the error
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestStartupReadinessResidencyField — OBS-01 schema coverage
# ---------------------------------------------------------------------------

class TestStartupReadinessResidencyField:
    """StartupReadinessPayload includes residency posture (OBS-01)."""

    def test_residency_field_in_payload(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        """build_startup_readiness() includes a residency field with expected keys."""
        from ollarma.guards import RuntimeTelemetry
        from ollarma import service

        fake_telemetry = RuntimeTelemetry(
            loaded_models=(RESCUE_MODEL,),
            loaded_model_count=1,
            size_vram_bytes=0,
            swap_used_mb=50.0,
            telemetry_source="fake",
        )
        monkeypatch.setattr(
            "ollarma.guards.collect_runtime_telemetry",
            lambda *a, **kw: fake_telemetry,
        )
        ctrl = _controller(pinned=(RESCUE_MODEL,), tmp_path=tmp_path)
        monkeypatch.setattr(
            "ollarma.pipeline_control.get_pipeline_controller",
            lambda *a, **kw: ctrl,
        )
        monkeypatch.setattr(
            service, "startup_readiness_path", lambda root=None: tmp_path / "readiness.json"
        )

        payload = service.build_startup_readiness()
        assert payload.residency is not None
        assert payload.residency.rescue_target == RESCUE_MODEL
        assert payload.residency.state in ("ready", "degraded", "blocked", "unknown")

    def test_residency_field_defaults_none_in_payload(self):
        """StartupReadinessPayload can be constructed without residency (backwards compat)."""
        from ollarma.service import (
            StartupReadinessPayload,
            StartupModelAvailability,
            StartupSwapPosture,
            StartupAdmissionPosture,
            StartupPipelinePosture,
        )
        import datetime

        payload = StartupReadinessPayload(
            schema_version=1,
            service_label="com.byron.ollarma",
            generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            status="ready",
            model_availability=StartupModelAvailability(status="ready"),
            swap=StartupSwapPosture(status="ready", threshold_mb=512.0),
            admission=StartupAdmissionPosture(enabled=True),
            pipeline=StartupPipelinePosture(),
            # residency omitted
        )
        assert payload.residency is None
