"""Tests for macfind transport wiring — HTTP routes and MCP tools.

Covers plan 32-03 requirements:
  - POST /macfind/query: 400 on empty query, 412 on not-configured, 200 on success
  - POST /macfind/reindex: 412 on not-configured, 200 on success
  - MCP tools: find_on_mac and reindex_macfind registered
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """Fresh TestClient per test (no state bleed)."""
    from ollarma.http_api import app

    return TestClient(app, raise_server_exceptions=False)


def _mock_macfind_receipt():
    """Return a minimal MacFindReceipt with .model_dump()."""
    from ollarma.macfind_searcher import MacFindHit, MacFindReceipt

    return MacFindReceipt(
        query="protein folding mechanisms",
        namespace="__unscoped__",
        status="ok",
        no_match_reason=None,
        hits=(
            MacFindHit(
                path="<local-path>",
                snippet="Protein folding is mediated by chaperones...",
                bm25_score=0.8,
                cosine_score=0.7,
                hybrid_score=0.75,
            ),
        ),
        redaction_events=(),
        query_at="2026-04-15T10:00:00Z",
        latency_ms=42.5,
    )


# ---------------------------------------------------------------------------
# HTTP: POST /macfind/query
# ---------------------------------------------------------------------------


class TestHttpMacfindQuery:
    """HTTP /macfind/query endpoint tests."""

    def test_http_macfind_query_empty_query_400(self, client):
        """POST /macfind/query with empty query returns 400."""
        resp = client.post("/macfind/query", json={"query": ""})
        assert resp.status_code == 400
        assert "query is required" in resp.json().get("error", "")

    def test_http_macfind_query_missing_query_400(self, client):
        """POST /macfind/query with no query key returns 400."""
        resp = client.post("/macfind/query", json={})
        assert resp.status_code == 400
        assert "query is required" in resp.json().get("error", "")

    def test_http_macfind_query_not_configured_412(self, client):
        """POST /macfind/query returns 412 when macfind is not configured."""
        with patch("ollarma.service.find_on_mac", side_effect=ValueError("MACFIND_NOT_CONFIGURED")):
            resp = client.post("/macfind/query", json={"query": "protein folding"})
        assert resp.status_code == 412
        data = resp.json()
        assert data.get("reason_code") == "MACFIND_NOT_CONFIGURED"

    def test_http_macfind_query_ok_200(self, client):
        """POST /macfind/query returns 200 with receipt fields on success."""
        receipt = _mock_macfind_receipt()
        with patch("ollarma.service.find_on_mac", return_value=receipt):
            resp = client.post(
                "/macfind/query",
                json={"query": "protein folding mechanisms", "max_results": 5},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "protein folding mechanisms"
        assert data["status"] == "ok"
        assert len(data["hits"]) == 1
        assert data["hits"][0]["path"] == "<local-path>"
        assert data["hits"][0]["hybrid_score"] == pytest.approx(0.75)

    def test_http_macfind_query_namespace_header(self, client):
        """POST /macfind/query forwards X-Namespace-Prefix header to service."""
        receipt = _mock_macfind_receipt()
        with patch("ollarma.service.find_on_mac", return_value=receipt) as mock_fn:
            client.post(
                "/macfind/query",
                json={"query": "test query"},
                headers={"X-Namespace-Prefix": "papers"},
            )
        _call_kwargs = mock_fn.call_args
        # namespace_prefix argument (positional or keyword)
        args, kwargs = _call_kwargs
        # args[1] is namespace_prefix positional, or it may come via kwargs
        assert "papers" in args or kwargs.get("namespace_prefix") == "papers" or args[1] == "papers"

    def test_http_macfind_query_value_error_400(self, client):
        """POST /macfind/query returns 400 on non-MACFIND_NOT_CONFIGURED ValueError."""
        with patch("ollarma.service.find_on_mac", side_effect=ValueError("invalid query")):
            resp = client.post("/macfind/query", json={"query": "bad query"})
        assert resp.status_code == 400
        assert "invalid query" in resp.json().get("error", "")


# ---------------------------------------------------------------------------
# HTTP: POST /macfind/reindex
# ---------------------------------------------------------------------------


class TestHttpMacfindReindex:
    """HTTP /macfind/reindex endpoint tests."""

    def test_http_macfind_reindex_not_configured_412(self, client):
        """POST /macfind/reindex returns 412 when macfind is not configured."""
        with patch("ollarma.service.reindex_macfind", side_effect=ValueError("MACFIND_NOT_CONFIGURED")):
            resp = client.post("/macfind/reindex", json={})
        assert resp.status_code == 412
        data = resp.json()
        assert data.get("reason_code") == "MACFIND_NOT_CONFIGURED"

    def test_http_macfind_reindex_ok_200(self, client):
        """POST /macfind/reindex returns 200 with stats on success."""
        mock_stats = {"files": 42, "chunks": 380, "errors": []}
        with patch("ollarma.service.reindex_macfind", return_value=mock_stats):
            resp = client.post("/macfind/reindex", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["stats"]["files"] == 42
        assert data["stats"]["chunks"] == 380

    def test_http_macfind_reindex_value_error_400(self, client):
        """POST /macfind/reindex returns 400 on non-MACFIND_NOT_CONFIGURED ValueError."""
        with patch("ollarma.service.reindex_macfind", side_effect=ValueError("index build failed")):
            resp = client.post("/macfind/reindex", json={})
        assert resp.status_code == 400
        assert "index build failed" in resp.json().get("error", "")


# ---------------------------------------------------------------------------
# MCP: tool registration
# ---------------------------------------------------------------------------


class TestMcpMacfindTools:
    """MCP tool registration tests for find_on_mac and reindex_macfind."""

    def test_mcp_find_on_mac_registered(self):
        """find_on_mac and reindex_macfind are registered in the MCP tool manager."""
        from ollarma.mcp_server import mcp

        registered = list(mcp._tool_manager._tools.keys())
        assert "find_on_mac" in registered, f"find_on_mac missing from {registered}"
        assert "reindex_macfind" in registered, f"reindex_macfind missing from {registered}"

    def test_mcp_find_on_mac_returns_receipt(self):
        """find_on_mac MCP tool returns a dict with receipt fields."""
        from ollarma.mcp_server import find_on_mac as mcp_find_on_mac

        receipt = _mock_macfind_receipt()
        with patch("ollarma.service.find_on_mac", return_value=receipt):
            result = mcp_find_on_mac(query="protein folding", namespace_prefix=None)
        assert isinstance(result, dict)
        assert result["query"] == "protein folding mechanisms"
        assert result["status"] == "ok"
        assert len(result["hits"]) == 1

    def test_mcp_reindex_macfind_returns_stats(self):
        """reindex_macfind MCP tool returns stats dict."""
        from ollarma.mcp_server import reindex_macfind as mcp_reindex

        mock_stats = {"files": 10, "chunks": 88, "errors": []}
        with patch("ollarma.service.reindex_macfind", return_value=mock_stats):
            result = mcp_reindex()
        assert isinstance(result, dict)
        assert result["files"] == 10
        assert result["chunks"] == 88

    def test_mcp_find_on_mac_not_configured_returns_error(self):
        """find_on_mac MCP tool returns error dict when not configured."""
        from ollarma.mcp_server import find_on_mac as mcp_find_on_mac

        with patch("ollarma.service.find_on_mac", side_effect=ValueError("MACFIND_NOT_CONFIGURED")):
            result = mcp_find_on_mac(query="test", namespace_prefix=None)
        assert "error" in result
        assert "MACFIND_NOT_CONFIGURED" in result["error"]

    def test_mcp_reindex_macfind_not_configured_returns_error(self):
        """reindex_macfind MCP tool returns error dict when not configured."""
        from ollarma.mcp_server import reindex_macfind as mcp_reindex

        with patch("ollarma.service.reindex_macfind", side_effect=ValueError("MACFIND_NOT_CONFIGURED")):
            result = mcp_reindex()
        assert "error" in result
        assert "MACFIND_NOT_CONFIGURED" in result["error"]
