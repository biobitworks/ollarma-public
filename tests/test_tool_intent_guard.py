"""Regression tests for pre-dispatch mutating tool intent validation.

`fleet_agent_loop` should only reach `dispatch_fleet_tool` after the guardrail
allows a mutating proposal. Blocked or repeatedly flagged intents must instead
emit the structured local receipt path built by `build_escalation_receipt`.
"""
from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest

from ollarma.agent import dispatch_fleet_tool, fleet_agent_loop
from ollarma.fleet import AdapterConfig
from ollarma.guardrail import GateResult, GuardrailGate, IntentGateResult


def _tool_response(tool_name: str, arguments: dict) -> object:
    """Build a minimal Ollama tool-call response message."""
    return type("Resp", (), {"message": type("Msg", (), {
        "content": "",
        "tool_calls": [type("TC", (), {"function": type("Fn", (), {
            "name": tool_name,
            "arguments": arguments,
        })()})],
    })()})()


def _done_response() -> object:
    """Build the final no-tool-calls Ollama message."""
    return type("Resp", (), {"message": type("Msg", (), {
        "content": "done",
        "tool_calls": None,
    })()})()


def _adapter(tmp_path: pathlib.Path) -> AdapterConfig:
    return AdapterConfig(
        project_name="demo",
        project_root=str(tmp_path),
        adapter_source="test",
    )


def _pass_result() -> tuple[str, GateResult]:
    return (
        "pass",
        GateResult(
            tristate="pass",
            blocked=False,
            risk_level="LOW",
            confidence=0.8,
            reasons=[],
            is_code=False,
        ),
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("edit_file", {"path": "README.md", "old_text": "a", "new_text": "b"}),
        ("run_bash", {"command": "rm -rf ."}),
        ("git_cmd", {"args": ["commit", "-am", "bad"]}),
    ],
)
@patch("ollarma.agent.ollama.Client")
@patch("ollarma.agent.build_tool_registry")
def test_blocked_mutating_tools_never_dispatch(
    mock_build_registry,
    mock_client_cls,
    tool_name: str,
    arguments: dict,
    tmp_path: pathlib.Path,
) -> None:
    """Blocked edit_file/run_bash/git_cmd proposals never reach dispatch_fleet_tool."""
    blocked_tool = MagicMock(return_value="should not run")
    mock_build_registry.return_value = (
        {tool_name: blocked_tool},
        [{"type": "function", "function": {"name": tool_name, "parameters": {"type": "object"}}}],
    )

    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.chat.side_effect = [_tool_response(tool_name, arguments), _done_response()]

    guardrail = MagicMock(spec=GuardrailGate)
    guardrail.validate_tool_intent.return_value = IntentGateResult(
        tristate="block",
        reason_code="TOOL_INTENT_REJECTED",
        tool_name=tool_name,
        gate_result=GateResult(
            tristate="block",
            blocked=True,
            risk_level="HIGH",
            confidence=0.95,
            reasons=[f"blocked {tool_name}"],
            is_code=False,
        ),
    )
    guardrail.validate.return_value = _pass_result()

    result = fleet_agent_loop("do it", "qwen", _adapter(tmp_path), guardrail=guardrail)

    assert callable(dispatch_fleet_tool)
    assert blocked_tool.call_count == 0
    tool_messages = [m["content"] for m in result.messages if m["role"] == "tool"]
    assert any("TOOL_INTENT_REJECTED" in message for message in tool_messages)


@patch("ollarma.agent.ollama.Client")
@patch("ollarma.agent.build_tool_registry")
def test_repeated_flagged_tool_escalates_before_second_dispatch(
    mock_build_registry,
    mock_client_cls,
    tmp_path: pathlib.Path,
) -> None:
    """A repeated flagged mutating intent dispatches once, then escalates locally."""
    run_bash = MagicMock(return_value="git status output")
    mock_build_registry.return_value = (
        {"run_bash": run_bash},
        [{"type": "function", "function": {"name": "run_bash", "parameters": {"type": "object"}}}],
    )

    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.chat.side_effect = [
        _tool_response("run_bash", {"command": "git status"}),
        _tool_response("run_bash", {"command": "git status"}),
        _done_response(),
    ]

    guardrail = MagicMock(spec=GuardrailGate)
    guardrail.validate_tool_intent.return_value = IntentGateResult(
        tristate="flag",
        reason_code="GUARDRAIL_FLAG_RETRYABLE",
        tool_name="run_bash",
        gate_result=GateResult(
            tristate="flag",
            blocked=False,
            risk_level="MEDIUM",
            confidence=0.7,
            reasons=["review this command"],
            is_code=False,
        ),
    )
    guardrail.validate.return_value = _pass_result()

    result = fleet_agent_loop("do it", "qwen", _adapter(tmp_path), guardrail=guardrail)

    assert run_bash.call_count == 1
    tool_messages = [m["content"] for m in result.messages if m["role"] == "tool"]
    assert any("GUARDRAIL_FLAG_RETRYABLE" in message for message in tool_messages)
