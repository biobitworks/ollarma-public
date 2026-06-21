"""Tests for harness/agent.py — Agent conversation loop with tool calling.

TDD RED: These tests import from ollarma.agent which does not exist yet.
All tests will fail with ImportError until harness/agent.py is created.
"""
from __future__ import annotations

import datetime as dt
import pathlib
from unittest.mock import MagicMock, patch

import orjson
import pytest

from ollarma.agent import (
    AgentResult,
    agent_loop,
    dispatch_tool,
    dispatch_fleet_tool,
    fleet_agent_loop,
    resolve_default_model,
)
from ollarma.evidence import canonical_hash
from ollarma.execution_policy import SelectionResolutionError, WorkloadClass
from ollarma.fleet import AdapterConfig
from ollarma.guardrail import GuardrailGate, GateResult


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _mock_chat_response(content: str = "done", tool_calls: list | None = None):
    """Build a fake Ollama ChatResponse matching the SDK shape.

    Args:
        content: The assistant message text.
        tool_calls: List of dicts with "name" and "args" keys.
    """
    fake_tool_calls = None
    if tool_calls:
        fake_tool_calls = []
        for tc in tool_calls:
            fn = type("FakeFunction", (), {
                "name": tc["name"],
                "arguments": tc["args"],
            })()
            fake_tool_calls.append(
                type("FakeToolCall", (), {"function": fn})()
            )

    msg = type("FakeMessage", (), {
        "role": "assistant",
        "content": content,
        "tool_calls": fake_tool_calls,
    })()

    return type("FakeResponse", (), {
        "message": msg,
        "done": True,
    })()


def _write_selection_artifact(
    results_dir: pathlib.Path,
    *,
    run_id: str,
    winners: dict[str, str] | None = None,
) -> pathlib.Path:
    """Create a validated selection artifact for policy-resolution tests."""
    per_suite_winners = winners if winners is not None else {
        "code": "qwen3-coder:7b",
        "science": "qwen3:8b",
    }
    artifact_body = {
        "run_id": run_id,
        "evidence_root": "deadbeef" * 8,
        "quality_weight_default": 0.7,
        "speed_weight_default": 0.3,
        "quality_weight_routing": 0.4,
        "speed_weight_routing": 0.6,
        "per_suite_winners": per_suite_winners,
        "pareto_frontier": sorted(set(per_suite_winners.values())),
    }
    artifact = {
        **artifact_body,
        "stable_decision_hash": canonical_hash(artifact_body),
    }
    path = results_dir / f"run-{run_id}.artifact.json"
    path.write_bytes(orjson.dumps(artifact))
    return path


# ---------------------------------------------------------------------------
# AgentResult model
# ---------------------------------------------------------------------------


class TestAgentResult:
    """Tests for AgentResult Pydantic model."""

    def test_is_frozen_pydantic(self) -> None:
        """AgentResult has frozen=True config."""
        result = AgentResult(
            final_response="done",
            messages=[],
            tool_calls_count=0,
            model="test",
        )
        with pytest.raises(Exception):
            result.final_response = "changed"  # type: ignore[misc]

    def test_required_fields(self) -> None:
        """AgentResult requires final_response, messages, tool_calls_count, model."""
        result = AgentResult(
            final_response="hello",
            messages=[{"role": "user", "content": "hi"}],
            tool_calls_count=3,
            model="qwen2.5-coder:7b",
        )
        assert result.final_response == "hello"
        assert result.tool_calls_count == 3
        assert result.model == "qwen2.5-coder:7b"


# ---------------------------------------------------------------------------
# dispatch_tool
# ---------------------------------------------------------------------------


class TestDispatchTool:
    """Tests for dispatch_tool function."""

    def test_dispatches_known_tool(self, tmp_path: pathlib.Path) -> None:
        """dispatch_tool routes read_file to TOOL_REGISTRY."""
        f = tmp_path / "test.txt"
        f.write_text("hello")
        result = dispatch_tool("read_file", {"path": str(f)}, str(tmp_path))
        assert "hello" in result

    def test_unknown_tool_returns_error(self) -> None:
        """dispatch_tool with unknown tool name returns error string."""
        result = dispatch_tool("unknown_tool", {}, "/tmp")
        assert "error" in result.lower() or "unknown" in result.lower()

    def test_tool_exception_returns_error(self, tmp_path: pathlib.Path) -> None:
        """dispatch_tool wraps exceptions in error string."""
        # edit_file with missing required args should return error, not crash
        result = dispatch_tool("edit_file", {"path": str(tmp_path / "nope.txt"), "old_text": "a", "new_text": "b"}, str(tmp_path))
        assert isinstance(result, str)
        # Should contain error info (file not found)
        assert "error" in result.lower() or "not found" in result.lower()


# ---------------------------------------------------------------------------
# agent_loop
# ---------------------------------------------------------------------------


class TestAgentLoop:
    """Tests for agent_loop function with mocked Ollama client."""

    @patch("ollarma.agent.ollama.Client")
    def test_no_tool_calls_returns_immediately(self, mock_client_cls) -> None:
        """Response with no tool_calls returns immediately."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.return_value = _mock_chat_response(content="Final answer")

        result = agent_loop(
            prompt="Hello",
            model="test-model",
            project_root="/tmp",
        )
        assert result.final_response == "Final answer"
        assert result.tool_calls_count == 0

    @patch("ollarma.agent.ollama.Client")
    def test_single_tool_call_round_trip(self, mock_client_cls, tmp_path) -> None:
        """One tool call followed by final response."""
        f = tmp_path / "test.txt"
        f.write_text("file contents")

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # First response: tool call, second response: final
        mock_client.chat.side_effect = [
            _mock_chat_response(
                content="",
                tool_calls=[{"name": "read_file", "args": {"path": str(f)}}],
            ),
            _mock_chat_response(content="I read the file"),
        ]

        result = agent_loop(
            prompt="Read test.txt",
            model="test-model",
            project_root=str(tmp_path),
        )
        assert result.final_response == "I read the file"
        assert result.tool_calls_count == 1

    @patch("ollarma.agent.ollama.Client")
    def test_multiple_tool_calls_in_sequence(self, mock_client_cls, tmp_path) -> None:
        """Two rounds of tool calls then final response."""
        f = tmp_path / "a.txt"
        f.write_text("aaa")

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_client.chat.side_effect = [
            _mock_chat_response(
                content="",
                tool_calls=[{"name": "read_file", "args": {"path": str(f)}}],
            ),
            _mock_chat_response(
                content="",
                tool_calls=[{"name": "run_bash", "args": {"command": "echo hi"}}],
            ),
            _mock_chat_response(content="All done"),
        ]

        result = agent_loop(
            prompt="Do things",
            model="test-model",
            project_root=str(tmp_path),
        )
        assert result.final_response == "All done"
        assert result.tool_calls_count == 2

    @patch("ollarma.agent.ollama.Client")
    def test_max_turns_stops_loop(self, mock_client_cls, tmp_path) -> None:
        """Infinite tool calls stop at max_turns."""
        f = tmp_path / "x.txt"
        f.write_text("x")

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # Always return tool calls — never a final response
        mock_client.chat.return_value = _mock_chat_response(
            content="still going",
            tool_calls=[{"name": "read_file", "args": {"path": str(f)}}],
        )

        result = agent_loop(
            prompt="Loop forever",
            model="test-model",
            project_root=str(tmp_path),
            max_turns=3,
        )
        # Should stop after max_turns
        assert result.tool_calls_count == 3
        assert result.model == "test-model"

    @patch("ollarma.agent.ollama.Client")
    def test_messages_grow_correctly(self, mock_client_cls, tmp_path) -> None:
        """After loop, messages list contains user + assistant + tool entries."""
        f = tmp_path / "t.txt"
        f.write_text("t")

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_client.chat.side_effect = [
            _mock_chat_response(
                content="",
                tool_calls=[{"name": "read_file", "args": {"path": str(f)}}],
            ),
            _mock_chat_response(content="Final"),
        ]

        result = agent_loop(
            prompt="Read",
            model="test-model",
            project_root=str(tmp_path),
        )
        # Messages should have: user, assistant (tool call), tool result, assistant (final)
        roles = [m["role"] for m in result.messages]
        assert "user" in roles
        assert "assistant" in roles
        assert "tool" in roles

    @patch("ollarma.agent.ollama.Client")
    def test_system_message_prepended(self, mock_client_cls) -> None:
        """agent_loop with system_prompt adds system message at index 0."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.return_value = _mock_chat_response(content="done")

        result = agent_loop(
            prompt="Hello",
            model="test-model",
            project_root="/tmp",
            system_prompt="You are helpful.",
        )
        assert result.messages[0]["role"] == "system"
        assert result.messages[0]["content"] == "You are helpful."


# ---------------------------------------------------------------------------
# resolve_default_model
# ---------------------------------------------------------------------------


class TestResolveDefaultModel:
    """Tests for resolve_default_model function."""

    def test_reads_code_winner_from_artifact(self, tmp_path: pathlib.Path) -> None:
        """Resolves a chat workload from the latest validated artifact."""
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_selection_artifact(
            results_dir,
            run_id=run_id,
            winners={"code": "qwen3-coder:7b", "science": "qwen3:8b"},
        )

        result = resolve_default_model(
            str(results_dir),
            workload_class=WorkloadClass.CHAT,
        )
        assert result == "qwen3-coder:7b"

    def test_missing_artifact_raises_selection_missing(self, tmp_path: pathlib.Path) -> None:
        """Missing selection artifacts hard-fail with SELECTION_MISSING."""
        with pytest.raises(SelectionResolutionError) as exc_info:
            resolve_default_model(
                str(tmp_path / "nonexistent"),
                workload_class=WorkloadClass.CHAT,
            )

        assert exc_info.value.reason_code == "SELECTION_MISSING"
        assert "SELECTION_MISSING" in str(exc_info.value)

    def test_stale_artifact_raises_selection_stale(self, tmp_path: pathlib.Path) -> None:
        """Old artifacts hard-fail with SELECTION_STALE."""
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_selection_artifact(
            results_dir,
            run_id="2024-01-01T00:00:00Z",
        )

        with pytest.raises(SelectionResolutionError) as exc_info:
            resolve_default_model(
                str(results_dir),
                workload_class=WorkloadClass.CHAT,
            )

        assert exc_info.value.reason_code == "SELECTION_STALE"
        assert "SELECTION_STALE" in str(exc_info.value)

    def test_prefers_newest_run_id_over_artifact_mtime(self, tmp_path: pathlib.Path) -> None:
        """Newest validated run_id wins even if an older artifact has a newer mtime."""
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        newer = _write_selection_artifact(
            results_dir,
            run_id="2026-04-10T15:53:23Z",
            winners={"code": "qwen3-coder:7b"},
        )
        older = _write_selection_artifact(
            results_dir,
            run_id="2026-04-08T02:33:10Z",
            winners={"code": "qwen3:1.7b"},
        )

        older.touch()
        assert older.stat().st_mtime >= newer.stat().st_mtime

        from ollarma.execution_policy import resolve_selection

        result = resolve_selection(
            WorkloadClass.CHAT,
            results_dir=str(results_dir),
            now=dt.datetime(2026, 4, 10, 16, 0, tzinfo=dt.timezone.utc),
        )

        assert result == "qwen3-coder:7b"

    def test_ignores_newer_artifact_without_requested_suite(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Partial artifacts must not shadow the newest usable workload winner."""
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        _write_selection_artifact(
            results_dir,
            run_id="2026-05-31T02:16:35Z",
            winners={"code": "phi4-mini", "science": "phi4-mini"},
        )
        _write_selection_artifact(
            results_dir,
            run_id="2026-05-31T03:29:14Z",
            winners={},
        )

        from ollarma.execution_policy import resolve_selection

        result = resolve_selection(
            WorkloadClass.CHAT,
            results_dir=str(results_dir),
            now=dt.datetime(2026, 5, 31, 4, 0, tzinfo=dt.timezone.utc),
        )

        assert result == "phi4-mini"


# ---------------------------------------------------------------------------
# dispatch_fleet_tool
# ---------------------------------------------------------------------------


def _make_adapter(project_root: str = "/tmp/test-project") -> AdapterConfig:
    """Helper to build a minimal AdapterConfig for testing."""
    return AdapterConfig(
        project_name="test-project",
        project_root=project_root,
        tools=["gh", "git_cmd"],
        antibodies=["prompt_injection"],
        adapter_source="test",
    )


class TestDispatchFleetTool:
    """Tests for dispatch_fleet_tool function."""

    def test_routes_gh(self) -> None:
        """dispatch_fleet_tool routes 'gh' to the registry function."""
        called_with: dict = {}

        def fake_gh(args):
            called_with["args"] = args
            return "gh output"

        registry = {"gh": fake_gh}
        adapter = _make_adapter()
        result = dispatch_fleet_tool(
            "gh", {"args": "pr list"}, "/tmp/test-project", registry, adapter
        )
        assert result == "gh output"
        assert called_with["args"] == "pr list"

    def test_routes_db_query(self) -> None:
        """dispatch_fleet_tool routes 'db_query' with adapter db config."""
        adapter = AdapterConfig(
            project_name="db-proj",
            project_root="/tmp/db-proj",
            databases=[{"host": "localhost", "port": 8531, "db": "test"}],
            adapter_source="test",
        )

        called_with: dict = {}

        def fake_db(query):
            called_with["query"] = query
            return '["result"]'

        registry = {"db_query": fake_db}
        result = dispatch_fleet_tool(
            "db_query", {"query": "FOR d IN col RETURN d"}, "/tmp/db-proj", registry, adapter
        )
        assert result == '["result"]'
        assert called_with["query"] == "FOR d IN col RETURN d"

    def test_routes_base_tools(self, tmp_path: pathlib.Path) -> None:
        """dispatch_fleet_tool routes base tools (read_file) correctly."""
        f = tmp_path / "test.txt"
        f.write_text("hello fleet")

        from ollarma.tools import TOOL_REGISTRY
        registry = dict(TOOL_REGISTRY)
        adapter = _make_adapter(str(tmp_path))

        result = dispatch_fleet_tool(
            "read_file", {"path": str(f)}, str(tmp_path), registry, adapter
        )
        assert "hello fleet" in result

    def test_unknown_tool_returns_error(self) -> None:
        """dispatch_fleet_tool with unknown tool returns error string."""
        registry = {"gh": lambda **kw: "ok"}
        adapter = _make_adapter()
        result = dispatch_fleet_tool(
            "nonexistent", {}, "/tmp", registry, adapter
        )
        assert "error" in result.lower() or "unknown" in result.lower()

    def test_exception_returns_error(self) -> None:
        """dispatch_fleet_tool wraps exceptions in error string."""

        def bad_tool(args):
            raise RuntimeError("boom")

        registry = {"gh": bad_tool}
        adapter = _make_adapter()
        result = dispatch_fleet_tool(
            "gh", {"args": "test"}, "/tmp", registry, adapter
        )
        assert "error" in result.lower()
        assert "boom" in result.lower()


# ---------------------------------------------------------------------------
# fleet_agent_loop
# ---------------------------------------------------------------------------


class TestFleetAgentLoop:
    """Tests for fleet_agent_loop with mocked Ollama client and guardrail."""

    @patch("ollarma.agent.ollama.Client")
    @patch("ollarma.agent.build_tool_registry")
    def test_calls_guardrail_on_tool_result(
        self, mock_build_registry, mock_client_cls, tmp_path
    ) -> None:
        """fleet_agent_loop validates tool results through guardrail."""
        f = tmp_path / "test.txt"
        f.write_text("content")

        from ollarma.tools import TOOL_REGISTRY, TOOL_DEFINITIONS
        mock_build_registry.return_value = (dict(TOOL_REGISTRY), list(TOOL_DEFINITIONS))

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.side_effect = [
            _mock_chat_response(
                content="",
                tool_calls=[{"name": "read_file", "args": {"path": str(f)}}],
            ),
            _mock_chat_response(content="Done reading"),
        ]

        adapter = _make_adapter(str(tmp_path))
        guardrail = MagicMock(spec=GuardrailGate)
        # Return "pass" for tool result validation, "pass" for final response
        guardrail.validate.return_value = ("pass", GateResult(
            tristate="pass", blocked=False, risk_level="LOW",
            confidence=0.9, reasons=[], is_code=False,
        ))

        result = fleet_agent_loop(
            prompt="Read test.txt",
            model="test-model",
            adapter=adapter,
            guardrail=guardrail,
        )
        assert result.final_response == "Done reading"
        # guardrail.validate called at least once (tool result + final response)
        assert guardrail.validate.call_count >= 1

    @patch("ollarma.agent.ollama.Client")
    @patch("ollarma.agent.build_tool_registry")
    def test_blocks_on_guardrail_block(
        self, mock_build_registry, mock_client_cls, tmp_path
    ) -> None:
        """fleet_agent_loop replaces tool result when guardrail blocks."""
        f = tmp_path / "test.txt"
        f.write_text("sensitive data")

        from ollarma.tools import TOOL_REGISTRY, TOOL_DEFINITIONS
        mock_build_registry.return_value = (dict(TOOL_REGISTRY), list(TOOL_DEFINITIONS))

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.side_effect = [
            _mock_chat_response(
                content="",
                tool_calls=[{"name": "read_file", "args": {"path": str(f)}}],
            ),
            _mock_chat_response(content="Final response"),
        ]

        adapter = _make_adapter(str(tmp_path))
        guardrail = MagicMock(spec=GuardrailGate)
        # Block on tool result, pass on final response
        guardrail.validate.side_effect = [
            ("block", GateResult(
                tristate="block", blocked=True, risk_level="HIGH",
                confidence=0.95, reasons=["dangerous content"], is_code=False,
            )),
            ("pass", GateResult(
                tristate="pass", blocked=False, risk_level="LOW",
                confidence=0.9, reasons=[], is_code=False,
            )),
        ]

        result = fleet_agent_loop(
            prompt="Read file",
            model="test-model",
            adapter=adapter,
            guardrail=guardrail,
        )
        # The tool message should contain BLOCKED
        tool_msgs = [m for m in result.messages if m["role"] == "tool"]
        assert any("BLOCKED" in m["content"] for m in tool_msgs)

    @patch("ollarma.agent.ollama.Client")
    @patch("ollarma.agent.build_tool_registry")
    def test_passes_when_no_guardrail(
        self, mock_build_registry, mock_client_cls, tmp_path
    ) -> None:
        """fleet_agent_loop works without guardrail (None)."""
        from ollarma.tools import TOOL_REGISTRY, TOOL_DEFINITIONS
        mock_build_registry.return_value = (dict(TOOL_REGISTRY), list(TOOL_DEFINITIONS))

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.return_value = _mock_chat_response(content="No guardrail here")

        adapter = _make_adapter(str(tmp_path))
        result = fleet_agent_loop(
            prompt="Hello",
            model="test-model",
            adapter=adapter,
            guardrail=None,
        )
        assert result.final_response == "No guardrail here"
        assert result.tool_calls_count == 0

    @patch("ollarma.agent.ollama.Client")
    @patch("ollarma.agent.build_tool_registry")
    def test_flags_final_response(
        self, mock_build_registry, mock_client_cls, tmp_path
    ) -> None:
        """fleet_agent_loop prepends warning when guardrail flags final response."""
        from ollarma.tools import TOOL_REGISTRY, TOOL_DEFINITIONS
        mock_build_registry.return_value = (dict(TOOL_REGISTRY), list(TOOL_DEFINITIONS))

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.return_value = _mock_chat_response(content="Some suspicious output")

        adapter = _make_adapter(str(tmp_path))
        guardrail = MagicMock(spec=GuardrailGate)
        guardrail.validate.return_value = ("flag", GateResult(
            tristate="flag", blocked=False, risk_level="MEDIUM",
            confidence=0.7, reasons=["suspicious pattern"], is_code=False,
        ))

        result = fleet_agent_loop(
            prompt="Hello",
            model="test-model",
            adapter=adapter,
            guardrail=guardrail,
        )
        assert "WARNING" in result.final_response
        assert "suspicious pattern" in result.final_response
        assert "Some suspicious output" in result.final_response

    @patch("ollarma.agent.ollama.Client")
    @patch("ollarma.agent.build_tool_registry")
    def test_blocks_final_response(
        self, mock_build_registry, mock_client_cls, tmp_path
    ) -> None:
        """fleet_agent_loop replaces final response when guardrail blocks it."""
        from ollarma.tools import TOOL_REGISTRY, TOOL_DEFINITIONS
        mock_build_registry.return_value = (dict(TOOL_REGISTRY), list(TOOL_DEFINITIONS))

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.return_value = _mock_chat_response(content="Dangerous output")

        adapter = _make_adapter(str(tmp_path))
        guardrail = MagicMock(spec=GuardrailGate)
        guardrail.validate.return_value = ("block", GateResult(
            tristate="block", blocked=True, risk_level="HIGH",
            confidence=0.99, reasons=["malicious content"], is_code=False,
        ))

        result = fleet_agent_loop(
            prompt="Hello",
            model="test-model",
            adapter=adapter,
            guardrail=guardrail,
        )
        assert "BLOCKED" in result.final_response
        assert "malicious content" in result.final_response
        assert "Dangerous output" not in result.final_response
