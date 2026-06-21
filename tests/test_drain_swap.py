"""Tests for drain_and_swap() in PipelineController and HTTP/MCP/CLI transport.

Covers:
  34-01: drain_and_swap() logic — drain, timeout fallback, receipt chaining, benchmark block
  34-02: HTTP /pipelines/drain-swap route — 202, 400, 423
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Mock scheduler helpers
# ---------------------------------------------------------------------------

class MockSnapshot:
    def __init__(self, active_job_id=None):
        self.active_job_id = active_job_id


class MockScheduler:
    def __init__(self, active_job_id=None):
        self._active_job_id = active_job_id

    def snapshot(self):
        return MockSnapshot(self._active_job_id)


# ---------------------------------------------------------------------------
# 34-01 Wave 1: PipelineController.drain_and_swap()
# ---------------------------------------------------------------------------

class TestDrainAndSwap:
    """Plan 34-01: drain_and_swap() with timeout + fallback receipt."""

    def test_drain_swap_no_inflight(self, tmp_path):
        """Scheduler has no active job — drains immediately, fallback=False."""
        from ollarma.pipeline_control import PipelineController

        ctrl = PipelineController(tmp_path)
        mock_scheduler = MockScheduler(active_job_id=None)

        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            receipt = ctrl.drain_and_swap("old", "new", scheduler=mock_scheduler)

        assert receipt.payload["drained"] is True
        assert receipt.payload["fallback"] is False

    def test_drain_swap_timeout_hard_swap(self, tmp_path):
        """Scheduler always has active job — times out, fallback=True."""
        from ollarma.pipeline_control import PipelineController

        ctrl = PipelineController(tmp_path)
        mock_scheduler = MockScheduler(active_job_id="job123")

        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            receipt = ctrl.drain_and_swap(
                "old", "new", timeout_s=0.2, scheduler=mock_scheduler
            )

        assert receipt.payload["fallback"] is True
        assert receipt.payload["drained"] is False

    def test_drain_swap_receipt_has_both_models(self, tmp_path):
        """Receipt payload contains old_model and new_model."""
        from ollarma.pipeline_control import PipelineController

        ctrl = PipelineController(tmp_path)
        mock_scheduler = MockScheduler(active_job_id=None)

        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            receipt = ctrl.drain_and_swap("old", "new", scheduler=mock_scheduler)

        assert receipt.payload["old_model"] == "old"
        assert receipt.payload["new_model"] == "new"

    def test_drain_swap_benchmark_flag_blocks(self, tmp_path):
        """Benchmark flag present — raises ValueError('BENCHMARK_ACTIVE')."""
        from ollarma.pipeline_control import PipelineController

        ctrl = PipelineController(tmp_path)
        # Create the benchmark flag
        ctrl._benchmark_flag.touch()

        with pytest.raises(ValueError, match="BENCHMARK_ACTIVE"):
            ctrl.drain_and_swap("old", "new", scheduler=MockScheduler())

    def test_drain_swap_chains_receipt(self, tmp_path):
        """drain_swap receipt.previous_hash == prior pin receipt.receipt_hash."""
        from ollarma.pipeline_control import PipelineController

        ctrl = PipelineController(tmp_path)
        mock_scheduler = MockScheduler(active_job_id=None)

        pin_receipt = ctrl.pin("old")

        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            drain_receipt = ctrl.drain_and_swap(
                "old", "new", scheduler=mock_scheduler
            )

        assert drain_receipt.previous_hash == pin_receipt.receipt_hash


# ---------------------------------------------------------------------------
# 34-02 Wave 2: HTTP /pipelines/drain-swap
# ---------------------------------------------------------------------------

class TestHttpDrainSwap:
    """Plan 34-02: POST /pipelines/drain-swap HTTP route."""

    def _make_receipt(self):
        """Build a minimal fake PipelineReceipt for mocking."""
        from ollarma.pipeline_control import PipelineReceipt
        return PipelineReceipt.create(
            operation="drain_swap",
            model="new-model",
            previous_hash=None,
            payload={
                "old_model": "old-model",
                "new_model": "new-model",
                "drained": True,
                "timeout_s": 30.0,
                "fallback": False,
            },
        )

    def test_http_drain_swap_ok_202(self):
        """POST /pipelines/drain-swap with valid body returns 202 + receipt JSON."""
        from starlette.testclient import TestClient
        from ollarma.http_api import app

        fake_receipt = self._make_receipt()

        with patch("ollarma.service.pipeline_drain_swap", return_value=fake_receipt):
            client = TestClient(app, raise_server_exceptions=True)
            resp = client.post(
                "/pipelines/drain-swap",
                json={"old_model": "old-model", "new_model": "new-model"},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["operation"] == "drain_swap"

    def test_http_drain_swap_missing_models_400(self):
        """POST /pipelines/drain-swap with no models returns 400."""
        from starlette.testclient import TestClient
        from ollarma.http_api import app

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/pipelines/drain-swap", json={})
        assert resp.status_code == 400

    def test_http_drain_swap_benchmark_active_423(self):
        """POST /pipelines/drain-swap when benchmark active returns 423."""
        from starlette.testclient import TestClient
        from ollarma.http_api import app

        with patch(
            "ollarma.service.pipeline_drain_swap",
            side_effect=ValueError("BENCHMARK_ACTIVE"),
        ):
            client = TestClient(app, raise_server_exceptions=True)
            resp = client.post(
                "/pipelines/drain-swap",
                json={"old_model": "old-model", "new_model": "new-model"},
            )

        assert resp.status_code == 423
        assert resp.json()["reason_code"] == "BENCHMARK_ACTIVE"
