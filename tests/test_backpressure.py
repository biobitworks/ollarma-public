"""test_backpressure.py -- Tests for SWAP_DEGRADED reason code and 429 Retry-After.

Covers:
  - NS-06: SWAP_DEGRADED emitted when swap_used_mb > 512MB threshold
  - NS-03: HTTP 429 with Retry-After header for backpressure codes
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest
from starlette.testclient import TestClient

from ollarma.guards import RuntimeTelemetry
from ollarma.scheduler import (
    AdmissionDecision,
    JobRequest,
    Lane,
    Scheduler,
    SchedulerAdmissionError,
    SWAP_DEGRADED,
    SWAP_DEGRADED_THRESHOLD_MB,
    RESOURCE_BUDGET_EXCEEDED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scheduler(telemetry_provider) -> Scheduler:
    """Create a Scheduler with an isolated temp state directory."""
    state_dir = pathlib.Path(tempfile.mkdtemp(prefix="ollarma-test-bp-"))
    return Scheduler(telemetry_provider=telemetry_provider, state_dir=state_dir)


def _degraded_telemetry(swap_used_mb: float | None, degraded_mode: bool = True) -> RuntimeTelemetry:
    return RuntimeTelemetry(
        degraded_mode=degraded_mode,
        swap_used_mb=swap_used_mb,
        degraded_reason="test degraded reason",
        telemetry_source="test",
    )


# ---------------------------------------------------------------------------
# Task 1: Scheduler reason code tests
# ---------------------------------------------------------------------------


class TestSwapDegradedReasonCode:
    def test_swap_degraded_emitted_above_threshold(self) -> None:
        """swap above the RAM-relative threshold → SWAP_DEGRADED reason code."""
        scheduler = _make_scheduler(
            lambda: _degraded_telemetry(
                swap_used_mb=SWAP_DEGRADED_THRESHOLD_MB + 100.0, degraded_mode=True
            )
        )
        req = JobRequest(lane=Lane.WORKFLOW_EXECUTION_QUEUE, project="proj-a")
        decision = scheduler.preview(req)
        assert decision.status == "rejected", f"Expected rejected, got {decision.status}"
        assert decision.reason_code == SWAP_DEGRADED, (
            f"Expected SWAP_DEGRADED, got {decision.reason_code}"
        )

    def test_resource_budget_exceeded_emitted_below_threshold(self) -> None:
        """swap_used_mb=100 < 512 threshold → RESOURCE_BUDGET_EXCEEDED reason code."""
        scheduler = _make_scheduler(
            lambda: _degraded_telemetry(swap_used_mb=100.0, degraded_mode=True)
        )
        req = JobRequest(lane=Lane.WORKFLOW_EXECUTION_QUEUE, project="proj-a")
        decision = scheduler.preview(req)
        assert decision.status == "rejected", f"Expected rejected, got {decision.status}"
        assert decision.reason_code == RESOURCE_BUDGET_EXCEEDED, (
            f"Expected RESOURCE_BUDGET_EXCEEDED, got {decision.reason_code}"
        )

    def test_resource_budget_exceeded_when_swap_is_none(self) -> None:
        """swap_used_mb=None (unknown) → RESOURCE_BUDGET_EXCEEDED (not SWAP_DEGRADED)."""
        scheduler = _make_scheduler(
            lambda: _degraded_telemetry(swap_used_mb=None, degraded_mode=True)
        )
        req = JobRequest(lane=Lane.WORKFLOW_EXECUTION_QUEUE, project="proj-a")
        decision = scheduler.preview(req)
        assert decision.status == "rejected"
        assert decision.reason_code == RESOURCE_BUDGET_EXCEEDED, (
            f"Expected RESOURCE_BUDGET_EXCEEDED when swap is None, got {decision.reason_code}"
        )

    def test_no_error_when_healthy(self) -> None:
        """degraded_mode=False → preview status is accepted or queued (not rejected)."""
        scheduler = _make_scheduler(
            lambda: _degraded_telemetry(swap_used_mb=600.0, degraded_mode=False)
        )
        req = JobRequest(lane=Lane.WORKFLOW_EXECUTION_QUEUE, project="proj-a")
        decision = scheduler.preview(req)
        assert decision.status in ("accepted", "queued"), (
            f"Expected accepted or queued when healthy, got {decision.status}"
        )

    def test_swap_degraded_threshold_is_ram_relative(self) -> None:
        """Threshold is RAM-relative with a 512 MB floor (GPU-08), not a fixed 512."""
        from ollarma.scheduler import (
            _SWAP_DEGRADED_FLOOR_MB,
            _physical_ram_mb,
            SWAP_DEGRADED_FRACTION_OF_RAM,
        )

        assert SWAP_DEGRADED_THRESHOLD_MB >= _SWAP_DEGRADED_FLOOR_MB
        ram_mb = _physical_ram_mb()
        if ram_mb is not None:
            expected = max(_SWAP_DEGRADED_FLOOR_MB, ram_mb * SWAP_DEGRADED_FRACTION_OF_RAM)
            assert SWAP_DEGRADED_THRESHOLD_MB == expected
        else:
            assert SWAP_DEGRADED_THRESHOLD_MB == _SWAP_DEGRADED_FLOOR_MB

    def test_acquire_raises_swap_degraded_above_threshold(self) -> None:
        """acquire() raises SchedulerAdmissionError with SWAP_DEGRADED when swap is high."""
        scheduler = _make_scheduler(
            lambda: _degraded_telemetry(
                swap_used_mb=SWAP_DEGRADED_THRESHOLD_MB + 100.0, degraded_mode=True
            )
        )
        req = JobRequest(lane=Lane.WORKFLOW_EXECUTION_QUEUE, project="proj-a")
        with pytest.raises(SchedulerAdmissionError) as exc_info:
            scheduler.acquire(req)
        assert exc_info.value.reason_code == SWAP_DEGRADED


# ---------------------------------------------------------------------------
# Task 2: HTTP 429 Retry-After tests
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """Fresh TestClient per test (no state bleed)."""
    from ollarma.http_api import app
    return TestClient(app, raise_server_exceptions=False)


class TestHttp429BackpressureResponses:
    def test_http_429_on_swap_degraded(self, monkeypatch, client) -> None:
        """route_prompt raises SWAP_DEGRADED → HTTP 429."""
        from ollarma import service
        from ollarma.scheduler import RuntimeSnapshot, Lane

        snapshot = RuntimeSnapshot(
            active_job_id=None,
            active_lane=None,
            active_model=None,
            active_project=None,
            queue_depth=0,
            queue_depth_by_lane={l.value: 0 for l in Lane},
            active_read_only=0,
            degraded_mode=True,
            resource_reason="swap pressure",
            swap_used_mb=600.0,
            loaded_models=(),
            telemetry_source="test",
        )

        def _raise_swap(*args, **kwargs):
            raise SchedulerAdmissionError(SWAP_DEGRADED, snapshot, "swap exceeded 512MB")

        monkeypatch.setattr(service, "route_prompt", _raise_swap)

        resp = client.post(
            "/route",
            json={"prompt": "hello", "project": "test-project"},
        )
        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"
        assert resp.headers.get("Retry-After") == "30", (
            f"Expected Retry-After: 30, got {resp.headers.get('Retry-After')}"
        )

    def test_http_429_body_has_reason_code_and_retry_after(self, monkeypatch, client) -> None:
        """429 response body contains reason_code and retry_after keys."""
        from ollarma import service
        from ollarma.scheduler import RuntimeSnapshot, Lane

        snapshot = RuntimeSnapshot(
            active_job_id=None,
            active_lane=None,
            active_model=None,
            active_project=None,
            queue_depth=0,
            queue_depth_by_lane={l.value: 0 for l in Lane},
            active_read_only=0,
            degraded_mode=True,
            resource_reason="swap",
            swap_used_mb=600.0,
            loaded_models=(),
            telemetry_source="test",
        )

        def _raise_swap(*args, **kwargs):
            raise SchedulerAdmissionError(SWAP_DEGRADED, snapshot, "swap exceeded 512MB")

        monkeypatch.setattr(service, "route_prompt", _raise_swap)

        resp = client.post(
            "/route",
            json={"prompt": "hello", "project": "test-project"},
        )
        assert resp.status_code == 429
        body = resp.json()
        assert "reason_code" in body, f"Body missing reason_code: {body}"
        assert "retry_after" in body, f"Body missing retry_after: {body}"
        assert body["reason_code"] == SWAP_DEGRADED
        assert body["retry_after"] == 30

    def test_http_429_on_resource_budget_exceeded(self, monkeypatch, client) -> None:
        """route_prompt raises RESOURCE_BUDGET_EXCEEDED → HTTP 429."""
        from ollarma import service
        from ollarma.scheduler import RuntimeSnapshot, Lane

        snapshot = RuntimeSnapshot(
            active_job_id=None,
            active_lane=None,
            active_model=None,
            active_project=None,
            queue_depth=0,
            queue_depth_by_lane={l.value: 0 for l in Lane},
            active_read_only=0,
            degraded_mode=True,
            resource_reason="budget exceeded",
            swap_used_mb=100.0,
            loaded_models=(),
            telemetry_source="test",
        )

        def _raise_budget(*args, **kwargs):
            raise SchedulerAdmissionError(
                RESOURCE_BUDGET_EXCEEDED, snapshot, "resource budget exceeded"
            )

        monkeypatch.setattr(service, "route_prompt", _raise_budget)

        resp = client.post(
            "/route",
            json={"prompt": "hello", "project": "test-project"},
        )
        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"
        assert resp.headers.get("Retry-After") == "30"

    def test_http_non_backpressure_admission_error_not_429(self, monkeypatch, client) -> None:
        """SchedulerAdmissionError with non-backpressure code → not 429 (502)."""
        from ollarma import service
        from ollarma.scheduler import RuntimeSnapshot, Lane, QUEUE_TIMEOUT

        snapshot = RuntimeSnapshot(
            active_job_id=None,
            active_lane=None,
            active_model=None,
            active_project=None,
            queue_depth=0,
            queue_depth_by_lane={l.value: 0 for l in Lane},
            active_read_only=0,
            degraded_mode=False,
            resource_reason=None,
            swap_used_mb=None,
            loaded_models=(),
            telemetry_source="test",
        )

        def _raise_timeout(*args, **kwargs):
            raise SchedulerAdmissionError(QUEUE_TIMEOUT, snapshot, "queue timed out")

        monkeypatch.setattr(service, "route_prompt", _raise_timeout)

        resp = client.post(
            "/route",
            json={"prompt": "hello", "project": "test-project"},
        )
        assert resp.status_code != 429, (
            f"QUEUE_TIMEOUT should not produce 429, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Task 3: /chat and /workflow 429+Retry-After parity (Phase 55 DEBT-06)
# ---------------------------------------------------------------------------

def _make_swap_snapshot():
    """Helper: build a RuntimeSnapshot representing high-swap state."""
    from ollarma.scheduler import RuntimeSnapshot, Lane
    return RuntimeSnapshot(
        active_job_id=None,
        active_lane=None,
        active_model=None,
        active_project=None,
        queue_depth=0,
        queue_depth_by_lane={l.value: 0 for l in Lane},
        active_read_only=0,
        degraded_mode=True,
        resource_reason="swap pressure",
        swap_used_mb=600.0,
        loaded_models=(),
        telemetry_source="test",
    )


class TestHttp429ChatWorkflowParity:
    """Phase 55 DEBT-06: /chat and /workflow return 429+Retry-After on SWAP_DEGRADED.

    Previously only /route had this handling; /chat and /workflow silently
    returned 502.  The _backpressure_response helper now covers all three.
    """

    def test_chat_429_on_swap_degraded(self, monkeypatch, client) -> None:
        """/chat: SWAP_DEGRADED from chat_with_model → HTTP 429 with Retry-After."""
        from ollarma import service

        snapshot = _make_swap_snapshot()

        def _raise_swap(*args, **kwargs):
            raise SchedulerAdmissionError(SWAP_DEGRADED, snapshot, "swap exceeded 512MB")

        monkeypatch.setattr(service, "chat_with_model", _raise_swap)

        resp = client.post("/chat", json={"message": "hello"})
        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"
        assert resp.headers.get("Retry-After") == "30", (
            f"Expected Retry-After: 30, got {resp.headers.get('Retry-After')}"
        )
        body = resp.json()
        assert body.get("reason_code") == SWAP_DEGRADED
        assert body.get("retry_after") == 30

    def test_workflow_429_on_swap_degraded(self, monkeypatch, client) -> None:
        """/workflow: SWAP_DEGRADED from submit_workflow → HTTP 429 with Retry-After."""
        from ollarma import service

        snapshot = _make_swap_snapshot()

        def _raise_swap(*args, **kwargs):
            raise SchedulerAdmissionError(SWAP_DEGRADED, snapshot, "swap exceeded 512MB")

        monkeypatch.setattr(service, "submit_workflow", _raise_swap)

        resp = client.post(
            "/workflow",
            json={"project": "proj", "manifest_ref": "m1", "step_id": "s1"},
        )
        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"
        assert resp.headers.get("Retry-After") == "30", (
            f"Expected Retry-After: 30, got {resp.headers.get('Retry-After')}"
        )
        body = resp.json()
        assert body.get("reason_code") == SWAP_DEGRADED

    def test_chat_502_on_non_backpressure_error(self, monkeypatch, client) -> None:
        """/chat: non-backpressure exception (e.g. RuntimeError) → 502, not 429."""
        from ollarma import service

        def _raise_runtime(*args, **kwargs):
            raise RuntimeError("model exploded")

        monkeypatch.setattr(service, "chat_with_model", _raise_runtime)

        resp = client.post("/chat", json={"message": "hello"})
        assert resp.status_code == 502, f"Expected 502, got {resp.status_code}: {resp.text}"
        assert resp.status_code != 429

    def test_workflow_400_on_missing_fields_not_affected(self, client) -> None:
        """/workflow: missing required fields still returns 400, not 429."""
        resp = client.post("/workflow", json={"project": "proj"})
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
