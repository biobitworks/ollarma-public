"""Tests for MetricsRegistry and /metrics endpoint (Phase 38 HARDEN-02)."""
import pytest
from ollarma.metrics import MetricsRegistry, get_registry


def test_counter_inc_and_expose():
    """counter_inc increments and expose_text renders it."""
    reg = MetricsRegistry()
    reg.counter_inc("test_total", {"endpoint": "/chat", "status": "200"}, help_text="test counter")
    reg.counter_inc("test_total", {"endpoint": "/chat", "status": "200"})
    text = reg.expose_text()
    assert "test_total" in text
    assert 'endpoint="/chat"' in text
    assert "2.0" in text


def test_gauge_set_and_expose():
    """gauge_set sets a gauge and expose_text renders it."""
    reg = MetricsRegistry()
    reg.gauge_set("ollarma_swap_used_mb", 123.5, "Swap used MB")
    text = reg.expose_text()
    assert "ollarma_swap_used_mb 123.5" in text
    assert "# TYPE ollarma_swap_used_mb gauge" in text


def test_metrics_reset():
    """reset() clears all metrics."""
    reg = MetricsRegistry()
    reg.counter_inc("x_total", {})
    reg.gauge_set("y_gauge", 1.0)
    reg.reset()
    text = reg.expose_text()
    assert "x_total" not in text
    assert "y_gauge" not in text


def test_metrics_endpoint_200():
    """GET /metrics returns 200 with Prometheus text content-type."""
    from starlette.testclient import TestClient
    from ollarma.http_api import app
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")


def test_metrics_expose_format():
    """expose_text() output ends with newline and has HELP/TYPE lines."""
    reg = MetricsRegistry()
    reg.gauge_set("ollarma_pinned_models", 2.0, "Pinned models count")
    text = reg.expose_text()
    assert text.endswith("\n")
    assert "# HELP ollarma_pinned_models" in text
    assert "# TYPE ollarma_pinned_models gauge" in text
