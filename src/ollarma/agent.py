"""agent.py -- Ollama agent conversation loop with tool calling.

Drives a multi-turn chat conversation where the model can invoke tools
(read_file, edit_file, run_bash, grep_search) and receive results.
The loop continues until the model produces a final text response
with no tool_calls, or max_turns is reached.

Used by both ``ollarma execute`` (plan task execution) and ``ollarma chat`` (REPL).

Also provides ``fleet_agent_loop`` for project-scoped agent runs with
expanded fleet tools and guardrail gate validation on every output.
"""
from __future__ import annotations

import re
from typing import Optional

import ollama
from pydantic import BaseModel, ConfigDict

from ollarma.escalation import (
    EscalationReceipt,
    ReasonCode,
    build_escalation_receipt,
)
from ollarma.execution_policy import (
    SelectionResolutionError,
    WorkloadClass,
    resolve_selection,
)
from ollarma.reserved_models import assert_model_not_reserved
from ollarma.conversation_provenance import (
    extract_tool_call_artifacts,
    record_conversation_turn,
)
from ollarma.tools import TOOL_REGISTRY, TOOL_DEFINITIONS
from ollarma.fleet_tools import build_tool_registry
from ollarma.guardrail import GuardrailGate, GateResult
from ollarma.fleet import AdapterConfig


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL: str = "qwen2.5-coder:7b"
DEFAULT_MAX_TURNS: int = 20
MUTATING_TOOL_NAMES: frozenset[str] = frozenset(
    {"edit_file", "run_bash", "git_cmd", "workflow", "submit_workflow", "run_project_workflow"}
)

# Regex for <tool_call> XML tags emitted by models that don't get parsed by
# Ollama's runtime (e.g. qwen2.5-coder wraps tool calls in XML but Ollama
# sometimes returns them as plain content instead of msg.tool_calls).
_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    re.DOTALL,
)


def _extract_tool_calls_from_text(content: str) -> list[dict] | None:
    """Fallback: parse <tool_call> XML tags from model text output.

    Some models (qwen2.5-coder) emit tool calls as XML in msg.content but
    Ollama's runtime doesn't always parse them into msg.tool_calls.  This
    extracts them so the agent loop can dispatch normally.

    Returns a list of {"name": str, "arguments": dict} dicts, or None if
    no valid tool calls found.
    """
    import json as _json

    matches = _TOOL_CALL_RE.findall(content)
    if not matches:
        return None

    parsed: list[dict] = []
    for raw in matches:
        try:
            obj = _json.loads(raw)
        except _json.JSONDecodeError:
            continue
        name = obj.get("name")
        arguments = obj.get("arguments", {})
        if name and isinstance(arguments, dict):
            parsed.append({"name": name, "arguments": arguments})

    return parsed or None


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

class AgentResult(BaseModel):
    """Immutable record of an agent conversation run.

    Fields:
        final_response: The last assistant message text (no tool_calls).
        messages: Full conversation history (list of dicts).
        tool_calls_count: Number of rounds that contained tool calls.
        model: Ollama model used for the conversation.
    """

    final_response: str
    messages: list[dict]
    tool_calls_count: int
    model: str

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def dispatch_tool(name: str, arguments: dict, project_root: str) -> str:
    """Route a tool call to the matching TOOL_REGISTRY function.

    Returns the tool output as a string. Never raises — errors are
    returned as descriptive strings so the model can react.
    """
    if name not in TOOL_REGISTRY:
        return (
            f"Error: Unknown tool '{name}'. "
            f"Available: {sorted(TOOL_REGISTRY.keys())}"
        )

    try:
        fn = TOOL_REGISTRY[name]
        if name == "read_file":
            return fn(path=arguments["path"], project_root=project_root)
        elif name == "edit_file":
            return fn(
                path=arguments["path"],
                old_text=arguments["old_text"],
                new_text=arguments["new_text"],
                project_root=project_root,
            )
        elif name == "run_bash":
            return fn(command=arguments["command"], cwd=project_root)
        elif name == "grep_search":
            return fn(
                pattern=arguments["pattern"],
                path=arguments.get("path", project_root),
                project_root=project_root,
            )
        else:
            # Fallback: try passing arguments directly
            return fn(**arguments)
    except Exception as exc:
        return f"Error executing {name}: {exc}"


# ---------------------------------------------------------------------------
# Default model resolution
# ---------------------------------------------------------------------------

def resolve_default_model(
    results_dir: str = "results",
    *,
    workload_class: WorkloadClass = WorkloadClass.CHAT,
) -> str:
    """Compatibility wrapper for workload-aware selection resolution.

    The legacy helper name is preserved so existing callers can migrate
    incrementally, but policy-backed execution no longer silently falls back to
    DEFAULT_MODEL when selection coverage is missing or stale.
    """
    return resolve_selection(workload_class=workload_class, results_dir=results_dir)


def _is_mutating_tool(name: str) -> bool:
    """Return True when the tool proposal can mutate project state."""
    return name in MUTATING_TOOL_NAMES or "workflow" in name


def _format_receipt(receipt: EscalationReceipt) -> str:
    """Serialize escalation receipts for tool-message history."""
    return receipt.model_dump_json(indent=2)


def _append_agent_conversation_turn(
    *,
    surface: str,
    prompt: str,
    final_response: str,
    messages: list[dict],
    model: str,
    project_root: str,
    project: str | None = None,
    lane: str | None = None,
) -> None:
    try:
        tool_artifacts = extract_tool_call_artifacts(messages)
        record_conversation_turn(
            repo_root=project_root,
            surface=surface,  # type: ignore[arg-type]
            role="model",
            prompt_text=prompt,
            response_text=final_response,
            project=project,
            model=model,
            lane=lane,
            metadata={
                "messages": messages,
                "tool_artifacts": [
                    artifact.model_dump(mode="json") for artifact in tool_artifacts
                ],
            },
        )
    except Exception:  # noqa: BLE001 -- provenance capture must not break agent runs
        return


# ---------------------------------------------------------------------------
# Fleet tool dispatch
# ---------------------------------------------------------------------------


def dispatch_fleet_tool(
    name: str,
    arguments: dict,
    project_root: str,
    registry: dict,
    adapter: Optional[AdapterConfig] = None,
) -> str:
    """Route a tool call to the fleet tool registry.

    Like dispatch_tool but uses the dynamically-built fleet registry
    instead of the static TOOL_REGISTRY. Handles both base tools and
    fleet-specific tools (gh, git_cmd, skill_invoke, db_query, mcp_query).

    Returns the tool output as a string. Never raises -- errors are
    returned as descriptive strings so the model can react.
    """
    if name not in registry:
        return (
            f"Error: Unknown tool '{name}'. "
            f"Available: {sorted(registry.keys())}"
        )

    try:
        fn = registry[name]

        # Fleet-specific tools -- lambdas in build_tool_registry already bind
        # cwd/db_config/server_command, so only pass model-supplied arguments.
        # (WR-02: avoid double-passing pre-bound arguments)
        if name == "gh":
            return fn(args=arguments["args"])
        elif name == "git_cmd":
            return fn(args=arguments["args"])
        elif name == "skill_invoke":
            return fn(skill_name=arguments["skill_name"])
        elif name == "db_query":
            return fn(query=arguments["query"])
        elif name == "mcp_query":
            return fn(
                query=arguments["query"],
                tool_name=arguments.get("tool_name"),
                arguments=arguments.get("arguments"),
            )

        # Base tools (read_file, edit_file, run_bash, grep_search)
        elif name == "read_file":
            return fn(path=arguments["path"], project_root=project_root)
        elif name == "edit_file":
            return fn(
                path=arguments["path"],
                old_text=arguments["old_text"],
                new_text=arguments["new_text"],
                project_root=project_root,
            )
        elif name == "run_bash":
            return fn(command=arguments["command"], cwd=project_root)
        elif name == "grep_search":
            return fn(
                pattern=arguments["pattern"],
                path=arguments.get("path", project_root),
                project_root=project_root,
            )
        else:
            # Fallback: try passing arguments directly
            return fn(**arguments)
    except Exception as exc:
        return f"Error executing {name}: {exc}"


# ---------------------------------------------------------------------------
# Agent conversation loop
# ---------------------------------------------------------------------------

def agent_loop(
    prompt: str,
    model: str,
    project_root: str,
    system_prompt: Optional[str] = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    conversation_surface: str = "agent",
) -> AgentResult:
    """Drive a multi-turn Ollama chat conversation with tool calling.

    Sends messages to the model with TOOL_DEFINITIONS. If the response
    contains tool_calls, dispatches each tool, appends results, and
    repeats. Stops when the model produces a final text response
    with no tool_calls, or after *max_turns* rounds.

    Args:
        prompt: Initial user message.
        model: Ollama model tag.
        project_root: Working directory for tool execution.
        system_prompt: Optional system message prepended to conversation.
        max_turns: Maximum tool-calling rounds before forced stop.

    Returns:
        AgentResult with final response, full message history,
        tool call count, and model name.
    """
    assert_model_not_reserved(model, context="agent loop")

    messages: list[dict] = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    messages.append({"role": "user", "content": prompt})

    client = ollama.Client()
    tool_calls_count = 0
    final_response = ""

    for _turn in range(max_turns):
        response = client.chat(
            model=model,
            messages=messages,
            tools=TOOL_DEFINITIONS,
        )

        msg = response.message

        # Resolve tool calls: native SDK first, then XML fallback
        tool_calls_to_dispatch: list[dict] | None = None
        if msg.tool_calls:
            tool_calls_to_dispatch = [
                {"name": tc.function.name, "arguments": dict(tc.function.arguments)}
                for tc in msg.tool_calls
            ]
        elif msg.content:
            tool_calls_to_dispatch = _extract_tool_calls_from_text(msg.content)

        # Build assistant message dict for history
        assistant_msg: dict = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if tool_calls_to_dispatch:
            assistant_msg["tool_calls"] = [
                {"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in tool_calls_to_dispatch
            ]
        messages.append(assistant_msg)

        if not tool_calls_to_dispatch:
            final_response = msg.content or ""
            break

        # Dispatch each tool call
        tool_calls_count += 1
        for tc in tool_calls_to_dispatch:
            tool_result = dispatch_tool(
                name=tc["name"],
                arguments=tc["arguments"],
                project_root=project_root,
            )
            messages.append({"role": "tool", "content": tool_result})

    else:
        # max_turns exhausted — use last assistant content as final response
        # WR-05: avoid referencing potentially unbound `msg` if max_turns=0
        if messages and messages[-1].get("role") == "assistant":
            final_response = messages[-1].get("content", "")
        else:
            final_response = ""

    _append_agent_conversation_turn(
        surface=conversation_surface,
        prompt=prompt,
        final_response=final_response,
        messages=messages,
        model=model,
        project_root=project_root,
    )
    return AgentResult(
        final_response=final_response,
        messages=messages,
        tool_calls_count=tool_calls_count,
        model=model,
    )


# ---------------------------------------------------------------------------
# Fleet agent conversation loop (with guardrail gate)
# ---------------------------------------------------------------------------


def fleet_agent_loop(
    prompt: str,
    model: str,
    adapter: AdapterConfig,
    guardrail: Optional[GuardrailGate] = None,
    system_prompt: Optional[str] = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    service_mode: bool = False,
    conversation_surface: str = "fleet_agent",
) -> AgentResult:
    """Drive a multi-turn Ollama chat with fleet tools and guardrail gate.

    Same structure as ``agent_loop`` but:
    - Uses ``build_tool_registry(adapter.project_root, adapter)`` for tools
    - Uses ``dispatch_fleet_tool`` for tool routing
    - After each tool result, validates via guardrail (if provided):
      - "block": replaces tool_result with blocked message
      - "flag": prepends warning to tool_result
      - "pass": uses tool_result as-is
    - After final model response, validates via guardrail (if provided)

    Args:
        prompt: Initial user message.
        model: Ollama model tag.
        adapter: AdapterConfig for the target project.
        guardrail: Optional GuardrailGate for output validation.
        system_prompt: Optional system message prepended to conversation.
        max_turns: Maximum tool-calling rounds before forced stop.

    Returns:
        AgentResult with final response, full message history,
        tool call count, and model name.
    """
    assert_model_not_reserved(model, context="fleet agent loop")

    project_root = adapter.project_root
    registry, definitions = build_tool_registry(
        project_root,
        adapter,
        service_mode=service_mode,
    )

    messages: list[dict] = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    messages.append({"role": "user", "content": prompt})

    client = ollama.Client()
    tool_calls_count = 0
    final_response = ""
    flagged_intents: dict[str, int] = {}

    for _turn in range(max_turns):
        response = client.chat(
            model=model,
            messages=messages,
            tools=definitions,
        )

        msg = response.message

        # Resolve tool calls: native SDK first, then XML fallback
        fleet_tool_calls: list[dict] | None = None
        if msg.tool_calls:
            fleet_tool_calls = [
                {"name": tc.function.name, "arguments": dict(tc.function.arguments)}
                for tc in msg.tool_calls
            ]
        elif msg.content:
            fleet_tool_calls = _extract_tool_calls_from_text(msg.content)

        # Build assistant message dict for history
        assistant_msg: dict = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if fleet_tool_calls:
            assistant_msg["tool_calls"] = [
                {"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in fleet_tool_calls
            ]
        messages.append(assistant_msg)

        if not fleet_tool_calls:
            final_response = msg.content or ""

            # Guardrail validation on final response (T-9-15)
            if guardrail is not None and final_response:
                tristate, gate_result = guardrail.validate(final_response, is_code=False)
                if tristate == "block":
                    reasons = ", ".join(gate_result.reasons)
                    final_response = f"[BLOCKED by guardrail: {reasons}]"
                elif tristate == "flag":
                    reasons = ", ".join(gate_result.reasons)
                    final_response = f"[WARNING: flagged by guardrail -- {reasons}]\n{final_response}"

            break

        # Dispatch each tool call
        tool_calls_count += 1
        for tc in fleet_tool_calls:
            tool_name = tc["name"]
            tool_args = tc["arguments"]

            if guardrail is not None and _is_mutating_tool(tool_name):
                intent_review = guardrail.validate_tool_intent(tool_name, tool_args)
                if intent_review.tristate == "block":
                    receipt = build_escalation_receipt(
                        project=adapter.project_name,
                        lane="local_inference_single",
                        task_class=tool_name,
                        local_model=model,
                        reason_code=ReasonCode.TOOL_INTENT_REJECTED,
                        reason_detail=", ".join(intent_review.gate_result.reasons)
                        or f"Guardrail blocked mutating tool intent: {tool_name}",
                        guardrail=intent_review.gate_result.model_dump(),
                        resource_snapshot={"tool_name": tool_name},
                    )
                    messages.append({"role": "tool", "content": _format_receipt(receipt)})
                    continue

                if intent_review.tristate == "flag":
                    intent_key = f"{tool_name}:{sorted(tool_args.items())}"
                    flagged_intents[intent_key] = flagged_intents.get(intent_key, 0) + 1
                    if flagged_intents[intent_key] > 1:
                        receipt = build_escalation_receipt(
                            project=adapter.project_name,
                            lane="local_inference_single",
                            task_class=tool_name,
                            local_model=model,
                            reason_code=ReasonCode.GUARDRAIL_FLAG_RETRYABLE,
                            reason_detail=", ".join(intent_review.gate_result.reasons)
                            or f"Mutating tool intent flagged more than once: {tool_name}",
                            guardrail=intent_review.gate_result.model_dump(),
                            resource_snapshot={"tool_name": tool_name},
                        )
                        messages.append({"role": "tool", "content": _format_receipt(receipt)})
                        continue

            tool_result = dispatch_fleet_tool(
                name=tool_name,
                arguments=tool_args,
                project_root=project_root,
                registry=registry,
                adapter=adapter,
            )

            # Guardrail validation on tool result (T-9-15)
            if guardrail is not None:
                tristate, gate_result = guardrail.validate(tool_result, is_code=False)
                if tristate == "block":
                    reasons = ", ".join(gate_result.reasons)
                    tool_result = f"[BLOCKED by guardrail: {reasons}]"
                elif tristate == "flag":
                    reasons = ", ".join(gate_result.reasons)
                    tool_result = f"[WARNING: flagged by guardrail -- {reasons}]\n{tool_result}"

            messages.append({"role": "tool", "content": tool_result})

    else:
        # max_turns exhausted — use last assistant content as final response
        # WR-05: avoid referencing potentially unbound `msg` if max_turns=0
        if messages and messages[-1].get("role") == "assistant":
            final_response = messages[-1].get("content", "")
        else:
            final_response = ""

    _append_agent_conversation_turn(
        surface=conversation_surface,
        prompt=prompt,
        final_response=final_response,
        messages=messages,
        model=model,
        project_root=project_root,
        project=adapter.project_name,
        lane="local_inference_single",
    )
    return AgentResult(
        final_response=final_response,
        messages=messages,
        tool_calls_count=tool_calls_count,
        model=model,
    )
