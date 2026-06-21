"""guardrail.py -- Antigence guardrail gate for local model output validation.

Wraps Antigence's GuardrailPipeline with tristate escalation (pass/flag/block),
per-project antibody loading from adapter config, and structured gate logging.

Every local model output passes through this gate before apply/commit.
When Antigence is not installed, the gate operates in pass-through mode
(returns pass for everything, never crashes the agent loop).

Exports:
    GateResult          -- Pydantic model for gate validation result
    GuardrailGate       -- Main gate class wrapping GuardrailPipeline
    ANTIBODY_REGISTRY   -- Registry mapping antibody keys to dotted import paths
    map_gate_result     -- Function mapping GuardrailResult to tristate string
    ANTIGENCE_AVAILABLE -- Bool indicating whether antigence is importable
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from ollarma.antibody_profile import AntibodyLaneSelection, resolve_antibody_lanes
from ollarma.fleet import AdapterConfig

logger = logging.getLogger(__name__)

TOOL_INTENT_REJECTED = "TOOL_INTENT_REJECTED"
GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
GUARDRAIL_FLAG_RETRYABLE = "GUARDRAIL_FLAG_RETRYABLE"

# ---------------------------------------------------------------------------
# Antigence availability check (sole eager import)
# ---------------------------------------------------------------------------

try:
    from antigence.guardrails import (  # type: ignore[import-untyped]
        GuardrailConfig,
        GuardrailPipeline,
        GuardrailResult,
    )

    ANTIGENCE_AVAILABLE = True
except ImportError:
    GuardrailConfig = None  # type: ignore[assignment,misc]
    GuardrailPipeline = None  # type: ignore[assignment,misc]
    GuardrailResult = None  # type: ignore[assignment,misc]
    ANTIGENCE_AVAILABLE = False


# ---------------------------------------------------------------------------
# GateResult model
# ---------------------------------------------------------------------------


class GateResult(BaseModel):
    """Immutable record of a guardrail gate validation.

    Fields:
        tristate: Escalation signal -- "pass", "flag", or "block".
        blocked: Whether the pipeline blocked the output.
        risk_level: Risk level string from the pipeline ("LOW", "MEDIUM", "HIGH").
        confidence: Pipeline confidence score (0.0-1.0).
        reasons: List of reason strings from the pipeline.
        is_code: Whether the input was validated as code (vs text).
    """

    model_config = ConfigDict(frozen=True)

    tristate: str  # "pass" | "flag" | "block"
    blocked: bool
    risk_level: str  # "LOW" | "MEDIUM" | "HIGH"
    confidence: float
    reasons: list[str]
    is_code: bool


class IntentGateResult(BaseModel):
    """Guardrail review result for a proposed mutating tool call."""

    model_config = ConfigDict(frozen=True)

    tristate: str
    reason_code: str | None = None
    tool_name: str
    gate_result: GateResult


# ---------------------------------------------------------------------------
# Pass-through constant
# ---------------------------------------------------------------------------

_PASSTHROUGH_REASONS = ["antigence not available -- pass-through"]


def _make_passthrough_result(is_code: bool = False) -> GateResult:
    """Build a pass-through GateResult for when Antigence is unavailable."""
    return GateResult(
        tristate="pass",
        blocked=False,
        risk_level="LOW",
        confidence=0.0,
        reasons=list(_PASSTHROUGH_REASONS),
        is_code=is_code,
    )


# ---------------------------------------------------------------------------
# Antibody registry (lazy import paths -- not eagerly loaded)
# ---------------------------------------------------------------------------

ANTIBODY_REGISTRY: dict[str, str] = {
    "citation": "antigence.agents.citation_antibodies.CitationAntibodySystem",
    "citation_pattern": "antigence.agents.citation_pattern_antibodies.CitationPatternAntibodySystem",
    "data_analysis": "antigence.agents.data_analysis_antibodies.DataAnalysisAntibodySystem",
    "figure_integrity": "antigence.agents.figure_integrity_antibodies.FigureIntegrityAntibodySystem",
    "infra": "antigence.agents.infra_antibodies.InfraAntibodySystem",
    "logic": "antigence.agents.logic_antibodies.LogicAntibodySystem",
    "methodology": "antigence.agents.methodology_antibodies.MethodologyAntibodySystem",
    "network_security": "antigence.agents.network_security_antibodies.NetworkSecurityAntibodySystem",
    "prompt_injection": "antigence.agents.prompt_injection_antibodies.PromptInjectionAntibodySystem",
    "reasoning_audit": "antigence.agents.reasoning_audit_antibodies.ReasoningAuditAntibodySystem",
}


# ---------------------------------------------------------------------------
# Tristate mapping
# ---------------------------------------------------------------------------


def map_gate_result(result: Any) -> str:
    """Map a GuardrailResult (or compatible object) to tristate: pass | flag | block.

    Logic:
    - blocked=True  -> "block"
    - risk_level="MEDIUM" or "HIGH", or anomaly_detected=True -> "flag"
    - Otherwise -> "pass"
    """
    if result.blocked:
        return "block"
    risk = getattr(result, "risk_level", "LOW")
    if (
        risk in ("MEDIUM", "HIGH")
        or getattr(result, "anomaly_detected", False)
    ):
        return "flag"
    return "pass"


# ---------------------------------------------------------------------------
# GuardrailGate
# ---------------------------------------------------------------------------


class GuardrailGate:
    """Gate wrapping Antigence GuardrailPipeline with tristate escalation.

    Features:
    - prompt_injection antibodies ALWAYS loaded (hardcoded, T-9-10/T-9-11)
    - Per-project antibodies loaded from adapter.antibodies list
    - Tristate mapping: block / flag / pass
    - Pass-through mode when Antigence is unavailable (T-9-12)
    - In-memory gate log for inspection
    """

    def __init__(self, adapter: AdapterConfig) -> None:
        self._adapter = adapter
        # prompt_injection is ALWAYS active (T-9-10, T-9-11)
        self._active_antibody_lanes: tuple[AntibodyLaneSelection, ...] = resolve_antibody_lanes(
            adapter,
            include_borrowed=False,
        )
        self._active_antibodies: list[str] = sorted(
            {lane.antibody_key for lane in self._active_antibody_lanes}
        )
        self._pipeline: Any = None
        self._gate_log: list[dict] = []

        if ANTIGENCE_AVAILABLE and GuardrailPipeline is not None:
            self._pipeline = GuardrailPipeline(
                config=GuardrailConfig(
                    block_on_danger=True,
                    block_on_anomaly=False,
                )
            )

    def validate(
        self, text: str, is_code: bool = False
    ) -> tuple[str, GateResult]:
        """Validate model output through the guardrail pipeline.

        Args:
            text: The model-generated text or code to validate.
            is_code: If True, routes to validate_code; otherwise validate_output.

        Returns:
            Tuple of (tristate, GateResult) where tristate is "pass", "flag", or "block".
        """
        if not ANTIGENCE_AVAILABLE or self._pipeline is None:
            passthrough = _make_passthrough_result(is_code=is_code)
            return ("pass", passthrough)

        if is_code:
            result = self._pipeline.validate_code(text)
        else:
            result = self._pipeline.validate_output(text)

        tristate = map_gate_result(result)
        gate_result = GateResult(
            tristate=tristate,
            blocked=result.blocked,
            risk_level=result.risk_level,
            confidence=result.classification_confidence,
            reasons=[result.reason] if result.reason else [],
            is_code=is_code,
        )
        self._gate_log.append(
            {
                "tristate": tristate,
                "risk_level": result.risk_level,
                "is_code": is_code,
            }
        )
        return (tristate, gate_result)

    def validate_tool_intent(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> IntentGateResult:
        """Validate a mutating tool proposal before dispatch."""
        intent_text = json.dumps(
            {"tool_name": tool_name, "arguments": arguments},
            sort_keys=True,
        )
        tristate, gate_result = self.validate(intent_text, is_code=False)
        reason_code = None
        if tristate == "block":
            reason_code = TOOL_INTENT_REJECTED
        elif tristate == "flag":
            reason_code = GUARDRAIL_FLAG_RETRYABLE
        return IntentGateResult(
            tristate=tristate,
            reason_code=reason_code,
            tool_name=tool_name,
            gate_result=gate_result,
        )

    def log_gate_result(self, gate_result: GateResult, context: str = "") -> None:
        """Append a structured gate result entry to the in-memory log.

        Uses orjson-compatible dict format. The entry includes the context
        string plus all GateResult fields.
        """
        import orjson  # noqa: F401 -- validates orjson is importable

        entry = {"context": context, **gate_result.model_dump()}
        self._gate_log.append(entry)

    @property
    def gate_log(self) -> list[dict]:
        """Return a copy of the gate log (list of dicts)."""
        return list(self._gate_log)

    @property
    def active_antibody_lanes(self) -> tuple[AntibodyLaneSelection, ...]:
        """Resolved active antibody lanes with project/profile provenance."""
        return self._active_antibody_lanes
