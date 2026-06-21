"""Tests for pipeline control transport wiring (Plan 33-02).

Covers HTTP routes and MCP tool registration for pipeline control:
  PIPE-01: GET /models/status
  PIPE-02: POST /pipelines/warmup (with error codes)
  PIPE-03: POST /pipelines/pin
  PIPE-04: POST /pipelines/evict (with pin protection)
  MCP: all 4 pipeline tools registered
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """Fresh TestClient per test (no state bleed)."""
    from ollarma.http_api import app

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_status_snapshot():
    """Return a minimal ModelStatusSnapshot."""
    from ollarma.pipeline_control import ModelStatusSnapshot

    return ModelStatusSnapshot(
        models=(),
        swap_used_mb=256.0,
        state="ok",
        cached_at="2026-04-15T00:00:00Z",
    )


def _mock_pipeline_receipt(operation: str = "warmup", model: str = "qwen3:4b"):
    """Return a minimal PipelineReceipt."""
    from ollarma.pipeline_control import PipelineReceipt

    return PipelineReceipt.create(operation, model, previous_hash=None)


# ---------------------------------------------------------------------------
# HTTP tests
# ---------------------------------------------------------------------------


class TestModelsStatusHTTP:
    """PIPE-01: GET /models/status returns 200 with snapshot fields."""

    def test_http_models_status_ok(self, client, monkeypatch):
        """GET /models/status returns 200 with ModelStatusSnapshot JSON."""
        from ollarma import service

        snapshot = _mock_status_snapshot()
        monkeypatch.setattr(service, "get_pipeline_status", lambda: snapshot)

        resp = client.get("/models/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "ok"
        assert data["swap_used_mb"] == 256.0
        assert "models" in data
        assert "cached_at" in data


class TestPipelinesWarmupHTTP:
    """PIPE-02: POST /pipelines/warmup error code mapping."""

    def test_http_warmup_missing_model_400(self, client):
        """POST /pipelines/warmup without model body returns 400."""
        resp = client.post("/pipelines/warmup", json={})
        assert resp.status_code == 400
        assert "model is required" in resp.json()["error"]

    def test_http_warmup_swap_degraded_503(self, client, monkeypatch):
        """POST /pipelines/warmup → ValueError(SWAP_DEGRADED) returns 503."""
        from ollarma import service

        monkeypatch.setattr(
            service, "pipeline_warmup", lambda model: (_ for _ in ()).throw(ValueError("SWAP_DEGRADED"))
        )

        resp = client.post("/pipelines/warmup", json={"model": "qwen3:4b"})
        assert resp.status_code == 503
        assert resp.json()["reason_code"] == "SWAP_DEGRADED"

    def test_http_warmup_benchmark_active_423(self, client, monkeypatch):
        """POST /pipelines/warmup → ValueError(BENCHMARK_ACTIVE) returns 423."""
        from ollarma import service

        monkeypatch.setattr(
            service, "pipeline_warmup", lambda model: (_ for _ in ()).throw(ValueError("BENCHMARK_ACTIVE"))
        )

        resp = client.post("/pipelines/warmup", json={"model": "qwen3:4b"})
        assert resp.status_code == 423
        assert resp.json()["reason_code"] == "BENCHMARK_ACTIVE"


class TestPipelinesPinHTTP:
    """PIPE-03: POST /pipelines/pin success path."""

    def test_http_pin_ok_200(self, client, monkeypatch):
        """POST /pipelines/pin returns 200 with PipelineReceipt JSON."""
        from ollarma import service

        receipt = _mock_pipeline_receipt("pin", "qwen3:4b")
        monkeypatch.setattr(service, "pipeline_pin", lambda model: receipt)

        resp = client.post("/pipelines/pin", json={"model": "qwen3:4b"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["operation"] == "pin"
        assert data["model"] == "qwen3:4b"
        assert "receipt_hash" in data


class TestPipelinesEvictHTTP:
    """PIPE-04: POST /pipelines/evict with pin protection."""

    def test_http_evict_pinned_409(self, client, monkeypatch):
        """POST /pipelines/evict → ValueError(PIPELINE_MODEL_PINNED) returns 409."""
        from ollarma import service

        monkeypatch.setattr(
            service, "pipeline_evict", lambda model: (_ for _ in ()).throw(ValueError("PIPELINE_MODEL_PINNED"))
        )

        resp = client.post("/pipelines/evict", json={"model": "qwen3:4b"})
        assert resp.status_code == 409
        assert resp.json()["reason_code"] == "PIPELINE_MODEL_PINNED"

    def test_http_evict_ok_200(self, client, monkeypatch):
        """POST /pipelines/evict returns 200 with PipelineReceipt JSON."""
        from ollarma import service

        receipt = _mock_pipeline_receipt("evict", "qwen3:4b")
        monkeypatch.setattr(service, "pipeline_evict", lambda model: receipt)

        resp = client.post("/pipelines/evict", json={"model": "qwen3:4b"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["operation"] == "evict"
        assert data["model"] == "qwen3:4b"
        assert "receipt_hash" in data


# ---------------------------------------------------------------------------
# MCP tool registration test (added in Task 2)
# ---------------------------------------------------------------------------


class TestMCPPipelineTools:
    """Verify all 4 pipeline tools are registered in the MCP server."""

    def test_mcp_pipeline_tools_registered(self):
        """get_pipeline_status, pipeline_warmup, pipeline_pin, pipeline_evict are in MCP tool list."""
        from ollarma.mcp_server import mcp

        registered = set(mcp._tool_manager._tools.keys())
        expected_pipeline_tools = {
            "get_pipeline_status",
            "pipeline_warmup",
            "pipeline_pin",
            "pipeline_evict",
        }
        missing = expected_pipeline_tools - registered
        assert not missing, f"Pipeline tools missing from MCP: {missing}"
