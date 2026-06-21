"""HTTP transport tests for /agents/{name}/run (Phase 35)."""
import pytest
import datetime as dt
from unittest.mock import patch
from starlette.testclient import TestClient
from ollarma.http_api import app
from ollarma.agents import AgentReceipt


def _mock_receipt(name="helper"):
    return AgentReceipt(
        agent_name=name,
        model_selected="qwen3:7b",
        rationale="resolved from selection artifact (chat workload)",
        retry_count=0,
        tool_invocations=[],
        output={"result": "test output", "citations": [], "notes": ""},
        run_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )


class TestAgentsHTTP:
    def test_agents_run_ok_200(self):
        """POST /agents/helper/run with prompt -> 200 with receipt JSON."""
        client = TestClient(app)
        with patch("ollarma.service.run_agent", return_value=_mock_receipt("helper")):
            resp = client.post("/agents/helper/run", json={"prompt": "hello"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_name"] == "helper"
        assert data["model_selected"] == "qwen3:7b"

    def test_agents_run_missing_prompt_400(self):
        """POST /agents/helper/run with no prompt -> 400."""
        client = TestClient(app)
        resp = client.post("/agents/helper/run", json={})
        assert resp.status_code == 400

    def test_agents_run_unknown_agent_404(self):
        """POST /agents/bogus/run with unknown agent name -> 404."""
        client = TestClient(app)
        with patch("ollarma.service.run_agent", side_effect=ValueError("Unknown agent: 'bogus'")):
            resp = client.post("/agents/bogus/run", json={"prompt": "hello"})
        assert resp.status_code == 404

    def test_agents_run_executor_ok_200(self):
        """POST /agents/executor/run -> 200."""
        client = TestClient(app)
        with patch("ollarma.service.run_agent", return_value=_mock_receipt("executor")):
            resp = client.post("/agents/executor/run", json={"prompt": "run workflow"})
        assert resp.status_code == 200
        assert resp.json()["agent_name"] == "executor"

    def test_agents_run_macfind_ok_200(self):
        """POST /agents/macfind/run -> 200."""
        client = TestClient(app)
        with patch("ollarma.service.run_agent", return_value=_mock_receipt("macfind")):
            resp = client.post("/agents/macfind/run", json={"prompt": "find config files"})
        assert resp.status_code == 200
        assert resp.json()["agent_name"] == "macfind"
