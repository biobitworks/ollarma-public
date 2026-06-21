"""tests/test_guardrail.py -- Unit tests for the Antigence guardrail gate.

Tests cover:
- GateResult model fields and immutability
- map_gate_result tristate mapping (block / flag / pass)
- ANTIBODY_REGISTRY completeness
- GuardrailGate antibody loading (prompt_injection always active)
- GuardrailGate.validate routing (is_code flag)
- Pass-through mode when Antigence is unavailable
- Gate logging
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — mock GuardrailResult matching the antigence interface
# ---------------------------------------------------------------------------


def _mock_guardrail_result(
    blocked: bool = False,
    passed: bool = True,
    risk_level: str = "LOW",
    anomaly_detected: bool = False,
    confidence: float = 0.95,
    reasons: list | None = None,
) -> SimpleNamespace:
    """Build a mock GuardrailResult matching real antigence.GuardrailResult attrs.

    Real API uses ``classification_confidence`` (not ``confidence``) and
    ``reason`` (singular str, not ``reasons`` list).  The mock exposes both
    the real names and legacy aliases so existing call-sites keep working
    during the transition.
    """
    reason_str = "; ".join(reasons) if reasons else ""
    return SimpleNamespace(
        blocked=blocked,
        passed=passed,
        risk_level=risk_level,
        anomaly_detected=anomaly_detected,
        # Real Antigence attribute names
        classification_confidence=confidence,
        reason=reason_str,
    )


def _make_adapter(**overrides):
    """Build a minimal AdapterConfig-like object for testing."""
    from ollarma.fleet import AdapterConfig

    defaults = {
        "project_name": "test-project",
        "project_root": "/tmp/test-project",
        "antibodies": [],
    }
    defaults.update(overrides)
    return AdapterConfig(**defaults)


# ===================================================================
# GateResult model tests
# ===================================================================


class TestGateResult:
    def test_gate_result_fields(self):
        """GateResult has all required fields with correct types."""
        from ollarma.guardrail import GateResult

        gr = GateResult(
            tristate="pass",
            blocked=False,
            risk_level="LOW",
            confidence=0.95,
            reasons=["all clear"],
            is_code=False,
        )
        assert gr.tristate == "pass"
        assert gr.blocked is False
        assert gr.risk_level == "LOW"
        assert gr.confidence == 0.95
        assert gr.reasons == ["all clear"]
        assert gr.is_code is False

    def test_gate_result_frozen(self):
        """GateResult is immutable (frozen)."""
        from ollarma.guardrail import GateResult

        gr = GateResult(
            tristate="pass",
            blocked=False,
            risk_level="LOW",
            confidence=0.9,
            reasons=[],
            is_code=False,
        )
        with pytest.raises(Exception):
            gr.tristate = "block"


# ===================================================================
# map_gate_result tests
# ===================================================================


class TestMapGateResult:
    def test_blocked_returns_block(self):
        """map_gate_result returns 'block' when blocked=True."""
        from ollarma.guardrail import map_gate_result

        result = _mock_guardrail_result(blocked=True, risk_level="HIGH")
        assert map_gate_result(result) == "block"

    def test_medium_risk_returns_flag(self):
        """map_gate_result returns 'flag' when risk_level='MEDIUM' and not blocked."""
        from ollarma.guardrail import map_gate_result

        result = _mock_guardrail_result(blocked=False, risk_level="MEDIUM")
        assert map_gate_result(result) == "flag"

    def test_anomaly_returns_flag(self):
        """map_gate_result returns 'flag' when anomaly_detected=True and not blocked."""
        from ollarma.guardrail import map_gate_result

        result = _mock_guardrail_result(
            blocked=False, risk_level="LOW", anomaly_detected=True
        )
        assert map_gate_result(result) == "flag"

    def test_low_risk_no_anomaly_returns_pass(self):
        """map_gate_result returns 'pass' for low risk, no anomaly, not blocked."""
        from ollarma.guardrail import map_gate_result

        result = _mock_guardrail_result(
            blocked=False, risk_level="LOW", anomaly_detected=False
        )
        assert map_gate_result(result) == "pass"

    def test_high_risk_returns_flag(self):
        """map_gate_result returns 'flag' when risk_level='HIGH' but not blocked."""
        from ollarma.guardrail import map_gate_result

        # HIGH risk but not blocked -- should flag (WR-01: HIGH risk must not pass silently)
        result = _mock_guardrail_result(
            blocked=False, risk_level="HIGH", anomaly_detected=False
        )
        assert map_gate_result(result) == "flag"


# ===================================================================
# ANTIBODY_REGISTRY tests
# ===================================================================


class TestAntibodyRegistry:
    def test_registry_contains_all_10_keys(self):
        """ANTIBODY_REGISTRY contains all 10 antibody system keys."""
        from ollarma.guardrail import ANTIBODY_REGISTRY

        expected_keys = {
            "citation",
            "citation_pattern",
            "data_analysis",
            "figure_integrity",
            "infra",
            "logic",
            "methodology",
            "network_security",
            "prompt_injection",
            "reasoning_audit",
        }
        assert set(ANTIBODY_REGISTRY.keys()) == expected_keys

    def test_registry_values_are_dotted_paths(self):
        """Each registry value is a dotted import path string."""
        from ollarma.guardrail import ANTIBODY_REGISTRY

        for key, path in ANTIBODY_REGISTRY.items():
            assert isinstance(path, str), f"Registry[{key}] is not a string"
            assert "." in path, f"Registry[{key}] has no dots: {path}"


# ===================================================================
# GuardrailGate.__init__ antibody loading tests
# ===================================================================


class TestGuardrailGateInit:
    def test_loads_prompt_injection_plus_specified(self):
        """GuardrailGate with antibodies=['citation', 'logic'] loads prompt_injection + citation + logic."""
        from ollarma.guardrail import GuardrailGate

        adapter = _make_adapter(antibodies=["citation", "logic"])
        gate = GuardrailGate(adapter)
        assert "prompt_injection" in gate._active_antibodies
        assert "citation" in gate._active_antibodies
        assert "logic" in gate._active_antibodies

    def test_empty_antibodies_still_loads_prompt_injection(self):
        """GuardrailGate with antibodies=[] still loads prompt_injection (always active)."""
        from ollarma.guardrail import GuardrailGate

        adapter = _make_adapter(antibodies=[])
        gate = GuardrailGate(adapter)
        assert "prompt_injection" in gate._active_antibodies
        assert len(gate._active_antibodies) >= 1


# ===================================================================
# GuardrailGate.validate tests
# ===================================================================


class TestGuardrailGateValidate:
    def test_validate_text_calls_validate_output(self):
        """validate(text, is_code=False) calls pipeline.validate_output."""
        from ollarma.guardrail import GuardrailGate

        adapter = _make_adapter()
        gate = GuardrailGate(adapter)

        mock_result = _mock_guardrail_result(
            blocked=False, risk_level="LOW", confidence=0.95
        )
        mock_pipeline = MagicMock()
        mock_pipeline.validate_output.return_value = mock_result
        gate._pipeline = mock_pipeline

        # Patch ANTIGENCE_AVAILABLE to True for this test
        with patch("ollarma.guardrail.ANTIGENCE_AVAILABLE", True):
            tristate, gate_result = gate.validate("some text", is_code=False)

        mock_pipeline.validate_output.assert_called_once_with("some text")
        assert tristate == "pass"
        assert gate_result.is_code is False

    def test_validate_code_calls_validate_code(self):
        """validate(code, is_code=True) calls pipeline.validate_code."""
        from ollarma.guardrail import GuardrailGate

        adapter = _make_adapter()
        gate = GuardrailGate(adapter)

        mock_result = _mock_guardrail_result(
            blocked=False, risk_level="LOW", confidence=0.90
        )
        mock_pipeline = MagicMock()
        mock_pipeline.validate_code.return_value = mock_result
        gate._pipeline = mock_pipeline

        with patch("ollarma.guardrail.ANTIGENCE_AVAILABLE", True):
            tristate, gate_result = gate.validate("def foo(): pass", is_code=True)

        mock_pipeline.validate_code.assert_called_once_with("def foo(): pass")
        assert tristate == "pass"
        assert gate_result.is_code is True

    def test_validate_tool_intent_blocks_mutation(self):
        """validate_tool_intent emits TOOL_INTENT_REJECTED on block."""
        from ollarma.guardrail import GuardrailGate, GateResult, TOOL_INTENT_REJECTED

        adapter = _make_adapter()
        gate = GuardrailGate(adapter)
        gate.validate = MagicMock(return_value=(
            "block",
            GateResult(
                tristate="block",
                blocked=True,
                risk_level="HIGH",
                confidence=0.9,
                reasons=["dangerous command"],
                is_code=False,
            ),
        ))

        result = gate.validate_tool_intent("run_bash", {"command": "rm -rf ."})

        assert result.reason_code == TOOL_INTENT_REJECTED
        assert result.tristate == "block"
        assert result.tool_name == "run_bash"

    def test_validate_tool_intent_flags_retryable(self):
        """validate_tool_intent emits GUARDRAIL_FLAG_RETRYABLE on flag."""
        from ollarma.guardrail import GuardrailGate, GUARDRAIL_FLAG_RETRYABLE
        from ollarma.guardrail import GateResult

        adapter = _make_adapter()
        gate = GuardrailGate(adapter)
        gate.validate = MagicMock(return_value=(
            "flag",
            GateResult(
                tristate="flag",
                blocked=False,
                risk_level="MEDIUM",
                confidence=0.7,
                reasons=["suspicious mutation"],
                is_code=False,
            ),
        ))

        result = gate.validate_tool_intent("edit_file", {"path": "README.md"})

        assert result.reason_code == GUARDRAIL_FLAG_RETRYABLE
        assert result.tristate == "flag"

    def test_validate_returns_pass_on_clean(self):
        """validate returns ('pass', GateResult) when pipeline says passed + LOW risk."""
        from ollarma.guardrail import GuardrailGate

        adapter = _make_adapter()
        gate = GuardrailGate(adapter)

        mock_result = _mock_guardrail_result(
            blocked=False, passed=True, risk_level="LOW", confidence=0.99
        )
        mock_pipeline = MagicMock()
        mock_pipeline.validate_output.return_value = mock_result
        gate._pipeline = mock_pipeline

        with patch("ollarma.guardrail.ANTIGENCE_AVAILABLE", True):
            tristate, gate_result = gate.validate("clean output")

        assert tristate == "pass"
        assert gate_result.tristate == "pass"
        assert gate_result.blocked is False

    def test_validate_returns_block_on_blocked(self):
        """validate returns ('block', GateResult) when pipeline returns blocked=True."""
        from ollarma.guardrail import GuardrailGate

        adapter = _make_adapter()
        gate = GuardrailGate(adapter)

        mock_result = _mock_guardrail_result(
            blocked=True, risk_level="HIGH", confidence=0.98, reasons=["injection"]
        )
        mock_pipeline = MagicMock()
        mock_pipeline.validate_output.return_value = mock_result
        gate._pipeline = mock_pipeline

        with patch("ollarma.guardrail.ANTIGENCE_AVAILABLE", True):
            tristate, gate_result = gate.validate("malicious text")

        assert tristate == "block"
        assert gate_result.tristate == "block"
        assert gate_result.blocked is True
        assert "injection" in gate_result.reasons


# ===================================================================
# Pass-through mode (Antigence unavailable)
# ===================================================================


class TestPassThroughMode:
    def test_antigence_unavailable_returns_pass(self):
        """When ANTIGENCE_AVAILABLE=False, validate returns pass-through GateResult."""
        from ollarma.guardrail import GuardrailGate

        adapter = _make_adapter()

        with patch("ollarma.guardrail.ANTIGENCE_AVAILABLE", False):
            gate = GuardrailGate(adapter)
            tristate, gate_result = gate.validate("any text", is_code=False)

        assert tristate == "pass"
        assert gate_result.tristate == "pass"
        assert gate_result.blocked is False
        assert gate_result.confidence == 0.0

    def test_pass_through_includes_reason(self):
        """Pass-through GateResult has a reason explaining why."""
        from ollarma.guardrail import GuardrailGate

        adapter = _make_adapter()

        with patch("ollarma.guardrail.ANTIGENCE_AVAILABLE", False):
            gate = GuardrailGate(adapter)
            _, gate_result = gate.validate("text")

        assert len(gate_result.reasons) > 0
        assert "not available" in gate_result.reasons[0].lower() or "pass-through" in gate_result.reasons[0].lower()


# ===================================================================
# Gate logging tests
# ===================================================================


class TestGateLogging:
    def test_log_gate_result_writes_to_gate_log(self):
        """log_gate_result appends structured entry to gate_log."""
        from ollarma.guardrail import GateResult, GuardrailGate

        adapter = _make_adapter()

        with patch("ollarma.guardrail.ANTIGENCE_AVAILABLE", False):
            gate = GuardrailGate(adapter)

        gr = GateResult(
            tristate="pass",
            blocked=False,
            risk_level="LOW",
            confidence=0.9,
            reasons=["ok"],
            is_code=False,
        )
        gate.log_gate_result(gr, context="test validation")

        log = gate.gate_log
        assert len(log) > 0
        last_entry = log[-1]
        assert last_entry["context"] == "test validation"
        assert last_entry["tristate"] == "pass"

    def test_validate_appends_to_gate_log(self):
        """validate() also appends an entry to gate_log via internal tracking."""
        from ollarma.guardrail import GuardrailGate

        adapter = _make_adapter()

        mock_result = _mock_guardrail_result(
            blocked=False, risk_level="LOW", confidence=0.95
        )
        mock_pipeline = MagicMock()
        mock_pipeline.validate_output.return_value = mock_result

        with patch("ollarma.guardrail.ANTIGENCE_AVAILABLE", True):
            gate = GuardrailGate(adapter)
            gate._pipeline = mock_pipeline
            gate.validate("some text")

        assert len(gate.gate_log) >= 1
        entry = gate.gate_log[0]
        assert entry["tristate"] == "pass"
        assert entry["is_code"] is False
