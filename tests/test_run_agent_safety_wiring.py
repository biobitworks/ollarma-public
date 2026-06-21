"""test_run_agent_safety_wiring.py -- Verify run_agent wires OverwatchAdapter, GovernanceStore, RunReceipt."""
from unittest.mock import MagicMock, patch
import pytest


def _make_mock_receipt():
    """Create a mock AgentReceipt with model_dump support."""
    receipt = MagicMock()
    receipt.model_dump.return_value = {
        "agent_name": "helper",
        "model_selected": "qwen3:1.7b",
        "rationale": "test",
        "retry_count": 0,
        "tool_invocations": [],
        "output": {"result": "ok"},
        "run_at": "2026-01-01T00:00:00Z",
    }
    receipt.run_at = "2026-01-01T00:00:00Z"
    return receipt


def _make_mock_receipt_with_overwatch_state():
    """Create a mock AgentReceipt payload that already includes overwatch_state."""
    receipt = _make_mock_receipt()
    payload = receipt.model_dump.return_value
    payload["overwatch_state"] = "BASE_DEFAULT"
    return receipt


class TestRunAgentOverwatchWiring:
    """SAFE-02/SAFE-03: OverwatchAdapter.attach() called and state on receipt."""

    @patch("ollarma.run_ledger.append_run_receipt")
    @patch("ollarma.governance_store.GovernanceStore.store", return_value="a" * 64)
    @patch("ollarma.overwatch_adapter.OverwatchAdapter.attach", return_value="NOT_CONFIGURED")
    @patch("ollarma.agents.get_agent")
    def test_overwatch_state_on_receipt(self, mock_get_agent, mock_attach, mock_store, mock_append):
        from ollarma.service import run_agent
        mock_agent = MagicMock()
        mock_agent.run.return_value = _make_mock_receipt()
        mock_get_agent.return_value = mock_agent

        result = run_agent("helper", "test prompt")

        mock_attach.assert_called_once()
        assert result.overwatch_state == "NOT_CONFIGURED"

    @patch("ollarma.run_ledger.append_run_receipt")
    @patch("ollarma.governance_store.GovernanceStore.store", return_value="a" * 64)
    @patch("ollarma.overwatch_adapter.OverwatchAdapter.attach", return_value="ATTACHED")
    @patch("ollarma.agents.get_agent")
    def test_overwatch_attached_state_propagated(self, mock_get_agent, mock_attach, mock_store, mock_append):
        from ollarma.service import run_agent
        mock_agent = MagicMock()
        mock_agent.run.return_value = _make_mock_receipt()
        mock_get_agent.return_value = mock_agent

        result = run_agent("helper", "test prompt")

        assert result.overwatch_state == "ATTACHED"

    @patch("ollarma.run_ledger.append_run_receipt")
    @patch("ollarma.governance_store.GovernanceStore.store", return_value="f" * 64)
    @patch("ollarma.overwatch_adapter.OverwatchAdapter.attach", return_value="ATTACHED")
    @patch("ollarma.agents.get_agent")
    def test_overwatch_state_overrides_base_receipt_without_duplicate_kwarg(
        self,
        mock_get_agent,
        mock_attach,
        mock_store,
        mock_append,
    ):
        from ollarma.service import run_agent
        mock_agent = MagicMock()
        mock_agent.run.return_value = _make_mock_receipt_with_overwatch_state()
        mock_get_agent.return_value = mock_agent

        result = run_agent("helper", "test prompt")

        assert result.overwatch_state == "ATTACHED"


class TestRunAgentGovernanceWiring:
    """SAFE-04/SAFE-07: GovernanceStore.store() called, digest in RunReceipt."""

    @patch("ollarma.run_ledger.append_run_receipt")
    @patch("ollarma.governance_store.GovernanceStore.store", return_value="b" * 64)
    @patch("ollarma.overwatch_adapter.OverwatchAdapter.attach", return_value="NOT_CONFIGURED")
    @patch("ollarma.agents.get_agent")
    def test_governance_store_called(self, mock_get_agent, mock_attach, mock_store, mock_append):
        from ollarma.service import run_agent
        mock_agent = MagicMock()
        mock_agent.run.return_value = _make_mock_receipt()
        mock_get_agent.return_value = mock_agent

        run_agent("helper", "test prompt")

        mock_store.assert_called_once()
        # Verify store receives a dict (receipt payload)
        store_arg = mock_store.call_args[0][0]
        assert isinstance(store_arg, dict)

    @patch("ollarma.run_ledger.append_run_receipt")
    @patch("ollarma.governance_store.GovernanceStore.store", return_value="c" * 64)
    @patch("ollarma.overwatch_adapter.OverwatchAdapter.attach", return_value="NOT_CONFIGURED")
    @patch("ollarma.agents.get_agent")
    def test_run_receipt_has_governance_refs(self, mock_get_agent, mock_attach, mock_store, mock_append):
        from ollarma.service import run_agent
        mock_agent = MagicMock()
        mock_agent.run.return_value = _make_mock_receipt()
        mock_get_agent.return_value = mock_agent

        run_agent("helper", "test prompt")

        mock_append.assert_called_once()
        run_receipt = mock_append.call_args[1]["receipt"]
        assert run_receipt.governance_refs == ("c" * 64,)

    @patch("ollarma.run_ledger.append_run_receipt")
    @patch("ollarma.governance_store.GovernanceStore.store", return_value="d" * 64)
    @patch("ollarma.overwatch_adapter.OverwatchAdapter.attach", return_value="NOT_CONFIGURED")
    @patch("ollarma.agents.get_agent")
    def test_run_receipt_fields(self, mock_get_agent, mock_attach, mock_store, mock_append):
        from ollarma.service import run_agent
        mock_agent = MagicMock()
        mock_agent.run.return_value = _make_mock_receipt()
        mock_get_agent.return_value = mock_agent

        run_agent("helper", "test prompt", namespace="ns1")

        run_receipt = mock_append.call_args[1]["receipt"]
        assert run_receipt.stage == "agent_run"
        assert run_receipt.step_id == "helper"
        assert run_receipt.task_or_command == "run_agent:helper"
        assert run_receipt.lane == "agent"
        assert run_receipt.status == "completed"
        assert run_receipt.namespace == "ns1"
        assert run_receipt.run_id.startswith("agent-helper-")


class TestRunAgentAdapterConfigFix:
    """Fix latent bug: AdapterConfig() no-arg construction raises ValidationError."""

    @patch("ollarma.run_ledger.append_run_receipt")
    @patch("ollarma.governance_store.GovernanceStore.store", return_value="e" * 64)
    @patch("ollarma.overwatch_adapter.OverwatchAdapter.attach", return_value="NOT_CONFIGURED")
    @patch("ollarma.agents.get_agent")
    def test_run_agent_does_not_raise_validation_error(self, mock_get_agent, mock_attach, mock_store, mock_append):
        """Bare AdapterConfig() would crash. This test confirms the fix."""
        from ollarma.service import run_agent
        mock_agent = MagicMock()
        mock_agent.run.return_value = _make_mock_receipt()
        mock_get_agent.return_value = mock_agent

        # Should NOT raise pydantic ValidationError
        result = run_agent("helper", "test prompt")
        assert result is not None
