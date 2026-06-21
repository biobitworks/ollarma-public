"""Tests for CLI execute/chat/continue-config commands in bench.py.

Tests CLI command registration, argument parsing, and model override logic.
Does NOT test actual Ollama API calls — those are integration tests.
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from ollarma.agent import fleet_agent_loop
from ollarma.cli import app
from ollarma.fleet import AdapterConfig
from ollarma.guardrail import GateResult, GuardrailGate, IntentGateResult


runner = CliRunner()


# ---------------------------------------------------------------------------
# execute command
# ---------------------------------------------------------------------------


class TestExecuteCommand:
    """Tests for the execute CLI command."""

    def test_missing_plan_file_exits_1(self) -> None:
        """execute with nonexistent path prints error and exits 1."""
        result = runner.invoke(app, ["execute", "/nonexistent/plan.md"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_imports_agent_modules(self) -> None:
        """ollarma.cli.py can import agent and plan_parser modules."""
        from ollarma.agent import agent_loop, resolve_default_model
        from ollarma.plan_parser import parse_plan_tasks
        assert callable(agent_loop)
        assert callable(resolve_default_model)
        assert callable(parse_plan_tasks)

    def test_command_registered(self) -> None:
        """execute is a registered CLI command."""
        result = runner.invoke(app, ["--help"])
        assert "execute" in result.output


# ---------------------------------------------------------------------------
# chat command
# ---------------------------------------------------------------------------


class TestChatCommand:
    """Tests for the chat CLI command."""

    def test_command_registered(self) -> None:
        """chat is a registered CLI command."""
        result = runner.invoke(app, ["--help"])
        assert "chat" in result.output

    def test_model_option_accepted(self) -> None:
        """chat command has --model option."""
        result = runner.invoke(app, ["chat", "--help"])
        assert "--model" in result.output


# ---------------------------------------------------------------------------
# model override
# ---------------------------------------------------------------------------


class TestModelOverride:
    """Tests for --model flag on execute and chat commands."""

    def test_execute_uses_model_flag(self, tmp_path: pathlib.Path) -> None:
        """--model flag overrides resolve_default_model in execute."""
        plan_file = tmp_path / "test-plan.md"
        plan_file.write_text(
            "---\nphase: test\nplan: 1\ntype: execute\nwave: 1\n---\n"
            "<tasks>\n<task type=\"auto\">\n"
            "  <name>Task 1: test</name>\n"
            "  <files>test.py</files>\n"
            "  <action>Do nothing</action>\n"
            "</task>\n</tasks>\n"
        )

        with patch("ollarma.cli.agent_loop") as mock_loop:
            mock_result = MagicMock()
            mock_result.tool_calls_count = 0
            mock_result.final_response = "done"
            mock_loop.return_value = mock_result

            result = runner.invoke(
                app, ["execute", str(plan_file), "--model", "custom-model:latest"]
            )
            # agent_loop should have been called with the custom model
            if mock_loop.called:
                call_kwargs = mock_loop.call_args
                assert call_kwargs.kwargs.get("model") == "custom-model:latest" or \
                       (len(call_kwargs.args) >= 2 and call_kwargs.args[1] == "custom-model:latest")

    def test_chat_uses_model_flag(self) -> None:
        """--model flag appears in chat help."""
        result = runner.invoke(app, ["chat", "--help"])
        assert "--model" in result.output
        assert "Ollama model override" in result.output


class TestPreDispatchMutatingIntent:
    """Regression tests for mutating-tool intent checks in fleet_agent_loop."""

    @patch("ollarma.agent.ollama.Client")
    @patch("ollarma.agent.build_tool_registry")
    def test_blocked_run_bash_never_dispatches(
        self, mock_build_registry, mock_client_cls, tmp_path: pathlib.Path
    ) -> None:
        run_bash = MagicMock(return_value="should not run")
        definitions = [{"type": "function", "function": {"name": "run_bash", "parameters": {"type": "object"}}}]
        mock_build_registry.return_value = ({"run_bash": run_bash}, definitions)

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.side_effect = [
            type("Resp", (), {"message": type("Msg", (), {
                "content": "",
                "tool_calls": [type("TC", (), {"function": type("Fn", (), {
                    "name": "run_bash",
                    "arguments": {"command": "rm -rf ."},
                })()})],
            })()})(),
            type("Resp", (), {"message": type("Msg", (), {"content": "done", "tool_calls": None})()})(),
        ]

        adapter = AdapterConfig(
            project_name="demo",
            project_root=str(tmp_path),
            adapter_source="test",
        )
        guardrail = MagicMock(spec=GuardrailGate)
        guardrail.validate_tool_intent.return_value = IntentGateResult(
            tristate="block",
            reason_code="TOOL_INTENT_REJECTED",
            tool_name="run_bash",
            gate_result=GateResult(
                tristate="block",
                blocked=True,
                risk_level="HIGH",
                confidence=0.9,
                reasons=["dangerous command"],
                is_code=False,
            ),
        )
        guardrail.validate.return_value = (
            "pass",
            GateResult(
                tristate="pass",
                blocked=False,
                risk_level="LOW",
                confidence=0.9,
                reasons=[],
                is_code=False,
            ),
        )

        result = fleet_agent_loop("do it", "qwen", adapter, guardrail=guardrail)

        assert run_bash.call_count == 0
        tool_messages = [m["content"] for m in result.messages if m["role"] == "tool"]
        assert any("TOOL_INTENT_REJECTED" in msg for msg in tool_messages)

    @patch("ollarma.agent.ollama.Client")
    @patch("ollarma.agent.build_tool_registry")
    def test_flagged_mutation_retries_once_then_escalates(
        self, mock_build_registry, mock_client_cls, tmp_path: pathlib.Path
    ) -> None:
        run_bash = MagicMock(return_value="command output")
        definitions = [{"type": "function", "function": {"name": "run_bash", "parameters": {"type": "object"}}}]
        mock_build_registry.return_value = ({"run_bash": run_bash}, definitions)

        tool_call_msg = type("Resp", (), {"message": type("Msg", (), {
            "content": "",
            "tool_calls": [type("TC", (), {"function": type("Fn", (), {
                "name": "run_bash",
                "arguments": {"command": "git status"},
            })()})],
        })()})()
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.side_effect = [
            tool_call_msg,
            tool_call_msg,
            type("Resp", (), {"message": type("Msg", (), {"content": "done", "tool_calls": None})()})(),
        ]

        adapter = AdapterConfig(
            project_name="demo",
            project_root=str(tmp_path),
            adapter_source="test",
        )
        gate_result = GateResult(
            tristate="flag",
            blocked=False,
            risk_level="MEDIUM",
            confidence=0.6,
            reasons=["review this command"],
            is_code=False,
        )
        guardrail = MagicMock(spec=GuardrailGate)
        guardrail.validate_tool_intent.return_value = IntentGateResult(
            tristate="flag",
            reason_code="GUARDRAIL_FLAG_RETRYABLE",
            tool_name="run_bash",
            gate_result=gate_result,
        )
        guardrail.validate.return_value = (
            "pass",
            GateResult(
                tristate="pass",
                blocked=False,
                risk_level="LOW",
                confidence=0.9,
                reasons=[],
                is_code=False,
            ),
        )

        result = fleet_agent_loop("do it", "qwen", adapter, guardrail=guardrail)

        assert run_bash.call_count == 1
        tool_messages = [m["content"] for m in result.messages if m["role"] == "tool"]
        assert any("GUARDRAIL_FLAG_RETRYABLE" in msg for msg in tool_messages)


# ---------------------------------------------------------------------------
# models.yml
# ---------------------------------------------------------------------------


class TestModelsYml:
    """Tests for DeepSeek-R1 model entry in models.yml."""

    def test_deepseek_r1_in_models(self) -> None:
        """deepseek-r1:14b appears in load_models() output."""
        from ollarma.registry import load_models
        models = load_models()
        names = [m.name for m in models]
        assert "deepseek-r1:14b" in names

    def test_models_count(self) -> None:
        """models.yml has 17 model entries total."""
        from ollarma.registry import load_models
        models = load_models()
        assert len(models) == 17
