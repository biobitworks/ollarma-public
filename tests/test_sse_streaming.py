"""Tests for SSE streaming on /chat, /route, /agents/{name}/run (Phase 38 HARDEN-01)."""
import json
import pytest
from unittest.mock import patch, MagicMock
from starlette.testclient import TestClient
from ollarma.http_api import app, _wants_sse


class TestWantsSSE:
    def test_no_accept_header(self):
        """No Accept header means not SSE."""
        from starlette.requests import Request
        from starlette.datastructures import Headers
        scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
        request = Request(scope)
        assert _wants_sse(request) is False

    def test_accept_event_stream(self):
        """Accept: text/event-stream means SSE."""
        from starlette.requests import Request
        scope = {
            "type": "http", "method": "GET", "path": "/",
            "headers": [(b"accept", b"text/event-stream")],
            "query_string": b"",
        }
        request = Request(scope)
        assert _wants_sse(request) is True

    def test_stream_query_param(self):
        """?stream=true means SSE."""
        from starlette.requests import Request
        scope = {
            "type": "http", "method": "GET", "path": "/",
            "headers": [],
            "query_string": b"stream=true",
        }
        request = Request(scope)
        assert _wants_sse(request) is True


class TestChatSSE:
    def test_chat_non_streaming_unchanged(self):
        """POST /chat without stream returns JSON (bit-identical to before)."""
        client = TestClient(app)
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"response": "hello", "model": "qwen3:7b"}
        with patch("ollarma.service.chat_with_model", return_value=mock_result):
            resp = client.post("/chat", json={"message": "hello"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"

    def test_chat_sse_returns_event_stream(self):
        """POST /chat with Accept: text/event-stream returns SSE stream."""
        client = TestClient(app)
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"response": "hello", "model": "qwen3:7b"}
        with patch("ollarma.service.chat_with_model", return_value=mock_result):
            resp = client.post(
                "/chat",
                json={"message": "hello"},
                headers={"Accept": "text/event-stream"},
            )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        body = resp.text
        assert "data: " in body
        assert "[DONE]" in body


class TestAgentsSSE:
    def test_agents_run_sse(self):
        """POST /agents/helper/run with ?stream=true returns SSE."""
        import datetime as dt
        from ollarma.agents import AgentReceipt
        mock_receipt = AgentReceipt(
            agent_name="helper", model_selected="qwen3:7b",
            rationale="resolved", retry_count=0,
            tool_invocations=[], output={"result": "ok", "citations": [], "notes": ""},
            run_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        client = TestClient(app)
        with patch("ollarma.service.run_agent", return_value=mock_receipt):
            resp = client.post("/agents/helper/run?stream=true", json={"prompt": "hello"})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "[DONE]" in resp.text
