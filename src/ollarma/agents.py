"""agents.py — Typed local agent layer (Pydantic-validated, policy-routed)."""
from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict

from ollarma.execution_policy import WorkloadClass, resolve_selection

AGENT_RETRY_CAP = 1

AGENT_TOOL_REJECTED = "AGENT_TOOL_REJECTED"
AGENT_OUTPUT_BLOCKED = "AGENT_OUTPUT_BLOCKED"

HELPER_AGENT_TOOLS: frozenset[str] = frozenset({"kb_search", "find_on_mac"})
EXECUTOR_AGENT_TOOLS: frozenset[str] = frozenset({"submit_workflow"})
MACFIND_AGENT_TOOLS: frozenset[str] = frozenset({"find_on_mac"})


class AgentToolRejectedError(RuntimeError):
    """Raised when a tool call is blocked (not in registry or gate blocks)."""


class AgentOutputBlockedError(RuntimeError):
    """Raised when agent output is blocked by the guardrail gate (fail-closed)."""


class AgentInputError(ValueError):
    """Raised when agent input fails schema validation."""


class AgentInput(BaseModel):
    """Typed input for any agent run."""
    model_config = ConfigDict(frozen=True)
    prompt: str
    project: str | None = None
    namespace: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] = {}


class AgentOutput(BaseModel):
    """Typed output from an agent run."""
    model_config = ConfigDict(frozen=True)
    result: str
    citations: list[str] = []
    notes: str = ""


class AgentReceipt(BaseModel):
    """Immutable receipt for an agent run."""
    model_config = ConfigDict(frozen=True)
    agent_name: str
    model_selected: str
    rationale: str
    retry_count: int
    tool_invocations: list[str]
    output: dict[str, Any]
    run_at: str  # ISO UTC
    overwatch_state: str = "NOT_CONFIGURED"  # SAFE-03: tri-state from OverwatchAdapter.attach()


class BaseAgent:
    """Base typed agent: policy-resolved model, gate-enforced tools, capped retries."""

    name: str = ""
    workload_class: WorkloadClass = WorkloadClass.CHAT
    allowed_tools: frozenset[str] = frozenset()

    def __init__(self, gate: Any, results_dir: str = "results") -> None:
        self._gate = gate
        self._results_dir = results_dir

    def _resolve_model(self) -> tuple[str, str]:
        """Resolve model via execution_policy. Raises SelectionResolutionError if missing."""
        model = resolve_selection(self.workload_class, self._results_dir)
        rationale = f"resolved from selection artifact ({self.workload_class.value} workload)"
        return model, rationale

    def _validate_tool(self, tool_name: str, args: dict[str, Any]) -> None:
        """Fail-closed tool validation: registry check then guardrail gate."""
        if tool_name not in self.allowed_tools:
            raise AgentToolRejectedError(
                f"{tool_name!r} not in {self.name} tool registry {self.allowed_tools}"
            )
        result = self._gate.validate_tool_intent(tool_name, args)
        if result.tristate == "block":
            raise AgentToolRejectedError(
                f"Tool '{tool_name}' blocked by guardrail: {result.reason_code}"
            )

    def _dispatch_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        raise NotImplementedError(f"{self.__class__.__name__}._dispatch_tool")

    def _call_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        """Gate then dispatch."""
        self._validate_tool(tool_name, args)
        return self._dispatch_tool(tool_name, args)

    def _call_model(self, model: str, prompt: str) -> str:
        """Invoke Ollama /api/generate. Returns response text."""
        import httpx
        resp = httpx.post(
            "http://localhost:11434/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")

    def _validate_output(self, text: str) -> str:
        """Gate model output. Returns tristate. Raises AgentOutputBlockedError on block."""
        tristate, gate_result = self._gate.validate(text)
        if tristate == "block":
            raise AgentOutputBlockedError(
                f"Agent output blocked by guardrail (no fail-open): {gate_result.reasons}"
            )
        return tristate

    def run(self, agent_input: AgentInput) -> AgentReceipt:
        """Resolve model -> optional tool -> infer -> gate output -> receipt."""
        model, rationale = self._resolve_model()
        tool_invocations: list[str] = []
        retry_count = 0

        # Optional tool invocation (fail-closed)
        tool_result_text = ""
        if agent_input.tool_name:
            tool_result = self._call_tool(agent_input.tool_name, agent_input.tool_args)
            tool_invocations.append(agent_input.tool_name)
            tool_result_text = f"\n\nTool result ({agent_input.tool_name}): {tool_result}"

        # Inference with retry cap
        full_prompt = agent_input.prompt + tool_result_text
        text = self._call_model(model, full_prompt)
        tristate = self._validate_output(text)

        if tristate == "flag" and AGENT_RETRY_CAP >= 1:
            retry_count = 1
            text = self._call_model(model, full_prompt)
            self._validate_output(text)  # second block still raises

        output = AgentOutput(result=text).model_dump()
        return AgentReceipt(
            agent_name=self.name,
            model_selected=model,
            rationale=rationale,
            retry_count=retry_count,
            tool_invocations=tool_invocations,
            output=output,
            run_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )


class HelperAgent(BaseAgent):
    """Read-only agent: kb_search + find_on_mac tools, CHAT workload."""
    name = "helper"
    workload_class = WorkloadClass.CHAT
    allowed_tools = HELPER_AGENT_TOOLS

    def _dispatch_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        from ollarma import service
        if tool_name == "kb_search":
            return service.search_project_kb(**args)
        if tool_name == "find_on_mac":
            return service.find_on_mac(**args)
        raise NotImplementedError(tool_name)


class ExecutorAgent(BaseAgent):
    """Execution agent: submit_workflow tool, PIPELINE_STEP workload."""
    name = "executor"
    workload_class = WorkloadClass.PIPELINE_STEP
    allowed_tools = EXECUTOR_AGENT_TOOLS

    def _dispatch_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        from ollarma import service
        if tool_name == "submit_workflow":
            return service.submit_workflow(**args)
        raise NotImplementedError(tool_name)


class MacfindAgent(BaseAgent):
    """Semantic file search agent: find_on_mac tool only, CHAT workload."""
    name = "macfind"
    workload_class = WorkloadClass.CHAT
    allowed_tools = MACFIND_AGENT_TOOLS

    def _dispatch_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        from ollarma import service
        if tool_name == "find_on_mac":
            return service.find_on_mac(**args)
        raise NotImplementedError(tool_name)


_AGENT_REGISTRY: dict[str, type[BaseAgent]] = {
    "helper": HelperAgent,
    "executor": ExecutorAgent,
    "macfind": MacfindAgent,
}


def get_agent(name: str, gate: Any, results_dir: str = "results") -> BaseAgent:
    """Return a typed agent by name. Raises ValueError for unknown names."""
    cls = _AGENT_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown agent: {name!r}. Known: {sorted(_AGENT_REGISTRY)}")
    return cls(gate, results_dir=results_dir)
