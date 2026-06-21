"""Tests for typed agent core (Phase 35)."""
import pytest
from unittest.mock import patch, MagicMock
from ollarma.agents import (
    AgentInput, AgentOutput, AgentReceipt,
    HelperAgent, ExecutorAgent, MacfindAgent,
    get_agent, AGENT_RETRY_CAP,
    AgentToolRejectedError, AgentOutputBlockedError,
    HELPER_AGENT_TOOLS, EXECUTOR_AGENT_TOOLS, MACFIND_AGENT_TOOLS,
)
from ollarma.guardrail import GateResult, IntentGateResult


def _make_gate(tristate="pass"):
    gate = MagicMock()
    gate_result = GateResult(
        tristate=tristate,
        blocked=(tristate == "block"),
        risk_level="LOW",
        confidence=0.9,
        reasons=[],
        is_code=False,
    )
    gate.validate.return_value = (tristate, gate_result)
    reason_code = "TOOL_INTENT_REJECTED" if tristate == "block" else None
    intent_result = IntentGateResult(
        tristate=tristate,
        reason_code=reason_code,
        tool_name="test",
        gate_result=gate_result,
    )
    gate.validate_tool_intent.return_value = intent_result
    return gate


def test_helper_agent_run_ok(tmp_path):
    """HelperAgent resolves model and returns AgentReceipt with correct fields."""
    gate = _make_gate("pass")
    agent = HelperAgent(gate, results_dir=str(tmp_path))
    with patch("ollarma.agents.resolve_selection", return_value="qwen3:7b"), \
         patch.object(agent, "_call_model", return_value="Test answer"):
        receipt = agent.run(AgentInput(prompt="hello"))
    assert receipt.model_selected == "qwen3:7b"
    assert receipt.agent_name == "helper"
    assert receipt.retry_count == 0
    assert receipt.tool_invocations == []
    assert receipt.output["result"] == "Test answer"
    assert receipt.run_at.endswith("+00:00") or receipt.run_at.endswith("Z")


def test_helper_agent_tool_rejected_by_gate():
    """Gate blocking a tool raises AgentToolRejectedError (fail-closed)."""
    gate = _make_gate("block")
    agent = HelperAgent(gate)
    with patch("ollarma.agents.resolve_selection", return_value="qwen3:7b"), \
         pytest.raises(AgentToolRejectedError):
        agent.run(AgentInput(prompt="find it", tool_name="find_on_mac", tool_args={"query": "x", "namespace": "ns"}))


def test_helper_agent_unknown_tool_raises():
    """Tool not in HelperAgent registry raises AgentToolRejectedError before gate."""
    gate = _make_gate("pass")
    agent = HelperAgent(gate)
    with patch("ollarma.agents.resolve_selection", return_value="qwen3:7b"), \
         pytest.raises(AgentToolRejectedError, match="not in helper tool registry"):
        agent.run(AgentInput(prompt="run it", tool_name="submit_workflow", tool_args={}))


def test_agent_output_blocked_raises():
    """Blocked output raises AgentOutputBlockedError (no fail-open)."""
    gate = _make_gate("block")
    gate.validate_tool_intent.return_value = IntentGateResult(
        tristate="pass", reason_code=None, tool_name="test",
        gate_result=GateResult(tristate="pass", blocked=False, risk_level="LOW", confidence=0.9, reasons=[], is_code=False)
    )
    agent = HelperAgent(gate)
    with patch("ollarma.agents.resolve_selection", return_value="qwen3:7b"), \
         patch.object(agent, "_call_model", return_value="bad output"), \
         pytest.raises(AgentOutputBlockedError):
        agent.run(AgentInput(prompt="hello"))


def test_agent_retry_on_flag():
    """Flagged output triggers one retry; retry_count==1 in receipt."""
    gate = MagicMock()
    gate_result_flag = GateResult(tristate="flag", blocked=False, risk_level="MEDIUM", confidence=0.8, reasons=[], is_code=False)
    gate_result_pass = GateResult(tristate="pass", blocked=False, risk_level="LOW", confidence=0.9, reasons=[], is_code=False)
    gate.validate.side_effect = [("flag", gate_result_flag), ("pass", gate_result_pass)]
    agent = HelperAgent(gate)
    with patch("ollarma.agents.resolve_selection", return_value="qwen3:7b"), \
         patch.object(agent, "_call_model", return_value="ok"):
        receipt = agent.run(AgentInput(prompt="hello"))
    assert receipt.retry_count == 1


def test_executor_agent_allowed_tools():
    """ExecutorAgent only allows submit_workflow."""
    assert EXECUTOR_AGENT_TOOLS == frozenset({"submit_workflow"})
    gate = _make_gate("pass")
    agent = ExecutorAgent(gate)
    with patch("ollarma.agents.resolve_selection", return_value="qwen3:7b"), \
         pytest.raises(AgentToolRejectedError, match="not in executor tool registry"):
        agent.run(AgentInput(prompt="find it", tool_name="find_on_mac", tool_args={}))


def test_macfind_agent_allowed_tools():
    """MacfindAgent only allows find_on_mac."""
    assert MACFIND_AGENT_TOOLS == frozenset({"find_on_mac"})


def test_get_agent_unknown_raises():
    """get_agent with unknown name raises ValueError."""
    gate = _make_gate("pass")
    with pytest.raises(ValueError, match="Unknown agent"):
        get_agent("nonexistent", gate)


def test_agent_receipt_is_frozen():
    """AgentReceipt is immutable (frozen Pydantic model)."""
    from pydantic import ValidationError
    gate = _make_gate("pass")
    agent = HelperAgent(gate)
    with patch("ollarma.agents.resolve_selection", return_value="qwen3:7b"), \
         patch.object(agent, "_call_model", return_value="ok"):
        receipt = agent.run(AgentInput(prompt="hello"))
    with pytest.raises((ValidationError, TypeError)):
        receipt.retry_count = 99  # type: ignore[misc]


def test_helper_agent_tool_invocation_recorded():
    """Successful tool call appears in tool_invocations in receipt."""
    gate = _make_gate("pass")
    agent = HelperAgent(gate)
    with patch("ollarma.agents.resolve_selection", return_value="qwen3:7b"), \
         patch.object(agent, "_call_model", return_value="found it"), \
         patch.object(agent, "_dispatch_tool", return_value={"hits": []}) as mock_dispatch:
        receipt = agent.run(AgentInput(prompt="find x", tool_name="find_on_mac", tool_args={"query": "x", "namespace": "ns"}))
    assert "find_on_mac" in receipt.tool_invocations
    mock_dispatch.assert_called_once_with("find_on_mac", {"query": "x", "namespace": "ns"})
