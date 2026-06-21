"""mcp_server.py -- MCP server exposing ollarma service tools over stdio.

Provides service tools for Claude Code and any MCP client:
  list_models, run_benchmark, get_report, verify_evidence, list_projects,
  chat_with_model, route_prompt, kb_status, kb_search, submit_workflow

CRITICAL CONSTRAINTS:
  - NO imports from ollarma.cli (cli.py imports Rich/Typer at module level)
  - NO imports from ollarma.tools (dangerous tool registry)
  - NO imports of rich or typer
  - All logging to stderr via Python logging module
  - asyncio.Semaphore(1) guards all Ollama inference calls
"""
import sys
import logging

# Configure stderr-only logging BEFORE any application imports.
# This ensures zero stdout contamination of the MCP JSON-RPC stream.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s - %(levelname)s - %(message)s",
)

import asyncio
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ollarma import service
from ollarma.conversation_provenance import record_conversation_turn
from ollarma.service import NoModelsError, NoResultsError

logger = logging.getLogger("ollarma.mcp")

# ---------------------------------------------------------------------------
# Concurrency guard: M1 Max 32GB still runs big models sequential-only (no parallel inference)
# ---------------------------------------------------------------------------
_inference_sem = asyncio.Semaphore(1)
_scheduler = service.get_scheduler()


def _append_conversation_turn(**kwargs) -> None:
    try:
        record_conversation_turn(**kwargs)
    except Exception as exc:  # noqa: BLE001 -- MCP result must not depend on telemetry
        logger.debug("conversation provenance append failed: %s", exc)

# ---------------------------------------------------------------------------
# Tool annotations
# ---------------------------------------------------------------------------
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITES_LOCAL_FILES = ToolAnnotations(readOnlyHint=False, destructiveHint=False)


# ---------------------------------------------------------------------------
# Server lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(server: FastMCP):
    """Server startup/shutdown lifecycle."""
    logger.info("ollarma MCP server starting")
    yield {}
    logger.info("ollarma MCP server stopping")


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------
mcp = FastMCP("ollarma", json_response=True, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Tool: list_models
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
async def list_models(ctx: Context) -> dict:
    """List all configured benchmark models and task suites from models.yml and tasks/ directory."""
    try:
        result = service.list_models_and_tasks()
        return result.model_dump()
    except NoModelsError as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Tool: run_benchmark
# ---------------------------------------------------------------------------
@mcp.tool(annotations=WRITES_LOCAL_FILES)
async def run_benchmark(
    ctx: Context,
    dry_run: bool = True,
    models: list[str] | None = None,
    suites: list[str] | None = None,
    trials: int = 3,
) -> dict:
    """Run benchmarks against specified Ollama models. dry_run=True (default) tests one model x one task. Full runs take 10-60 minutes."""
    try:
        async with _inference_sem:
            result = await asyncio.to_thread(
                service.run_benchmark,
                dry_run=dry_run,
                models_filter=models,
                suites_filter=suites,
                trials=trials,
                on_progress=lambda msg: logger.info(msg),
            )
        return result.model_dump()
    except (NoModelsError, NoResultsError) as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Tool: get_report
# ---------------------------------------------------------------------------
@mcp.tool(annotations=WRITES_LOCAL_FILES)
async def get_report(ctx: Context, run_id: str | None = None) -> dict:
    """Generate benchmark report with model rankings and selection tables. Returns Markdown-formatted results."""
    try:
        result = service.generate_report(run_id=run_id)
        return result.model_dump()
    except (NoResultsError, FileNotFoundError) as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Tool: verify_evidence
# ---------------------------------------------------------------------------
@mcp.tool(annotations=WRITES_LOCAL_FILES)
async def verify_evidence(ctx: Context, run_id: str) -> dict:
    """Verify the cryptographic evidence chain for a sealed benchmark run. Returns validity, receipt count, and evidence root hash."""
    try:
        result = service.verify_evidence(run_id=run_id)
        return result.model_dump()
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Tool: list_projects
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
async def list_projects(ctx: Context) -> dict:
    """List all registered projects from fleet adapter configs with paths and capabilities."""
    registry = service.list_projects(service_mode=True)
    return {name: adapter.model_dump() for name, adapter in registry.items()}


# ---------------------------------------------------------------------------
# Tool: chat_with_model
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
async def chat_with_model(ctx: Context, model: str, message: str) -> dict:
    """Send a single message to a local Ollama model and get a response. Use for quick model queries."""
    async with _inference_sem:
        try:
            result = await asyncio.to_thread(
                service.chat_with_model,
                message=message,
                model=model,
            )
            _append_conversation_turn(
                surface="mcp_chat",
                role="model",
                prompt_text=message,
                response_text=result.response,
                model=result.model,
                reason_code=result.reason_code,
                metadata={"status": result.status},
            )
            return result.model_dump()
        except Exception as exc:
            raise ToolError(f"Ollama chat failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Tool: route_prompt
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
async def route_prompt(
    ctx: Context,
    project: str,
    prompt: str,
    model: str | None = None,
    namespace_prefix: str | None = None,
) -> dict:
    """Route a prompt through a registered project adapter using the bounded service-mode tool surface."""
    async with _inference_sem:
        try:
            result = await asyncio.to_thread(
                service.route_prompt,
                prompt=prompt,
                project=project,
                model=model,
                service_mode=True,
                namespace_prefix=namespace_prefix,
            )
            _append_conversation_turn(
                surface="mcp_route",
                role="model",
                prompt_text=prompt,
                response_text=result.final_response,
                project=result.project,
                model=result.model,
                lane=result.lane,
                reason_code=result.reason_code,
                route_receipt_ref=".ollarma/kb/route_receipts.jsonl"
                if result.route_receipt is not None
                else None,
                kb_evidence_refs=tuple(
                    str(ref.get("doc_id") or ref.get("path") or ref.get("chunk_id"))
                    for ref in result.evidence_refs
                    if isinstance(ref, dict)
                ),
                metadata={
                    "query_class": result.query_class,
                    "kb_status": result.kb_status,
                    "next_action": result.next_action,
                },
            )
            return result.model_dump()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except FileNotFoundError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError(f"Project routing failed: {exc}") from exc


@mcp.tool(annotations=READ_ONLY)
async def kb_status(
    ctx: Context,
    project: str,
) -> dict:
    """Return read-only KB status for a registered project."""
    try:
        result = await asyncio.to_thread(
            service.get_project_kb_status,
            project,
            service_mode=True,
        )
        return result.model_dump(mode="json")
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
async def kb_search(
    ctx: Context,
    project: str,
    query: str,
    limit: int = 5,
) -> dict:
    """Return bounded read-only KB hits for a project query."""
    try:
        result = await asyncio.to_thread(
            service.search_project_kb,
            project,
            query,
            service_mode=True,
            limit=limit,
        )
        return result.model_dump(mode="json")
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=WRITES_LOCAL_FILES)
async def submit_workflow(
    ctx: Context,
    project: str,
    manifest_ref: str,
    step_id: str,
    model: str | None = None,
    namespace_prefix: str | None = None,
) -> dict:
    """Submit a manifest-backed validated workflow step to the explicit workflow lane."""
    try:
        result = await asyncio.to_thread(
            service.submit_workflow,
            project=project,
            manifest_ref=manifest_ref,
            step_id=step_id,
            model=model,
            namespace_prefix=namespace_prefix,
        )
        return result.model_dump()
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Tools: Pipeline control (Plan 33-02)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def get_pipeline_status() -> dict:
    """Return current model pipeline status snapshot."""
    return service.get_pipeline_status().model_dump()


@mcp.tool(annotations=READ_ONLY)
def pipeline_warmup(model: str) -> dict:
    """Warm up a model. Returns PipelineReceipt or error."""
    try:
        return service.pipeline_warmup(model).model_dump()
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def pipeline_pin(model: str) -> dict:
    """Pin a model (increment refcount)."""
    try:
        return service.pipeline_pin(model).model_dump()
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def pipeline_evict(model: str) -> dict:
    """Evict a model (fails if pinned)."""
    try:
        return service.pipeline_evict(model).model_dump()
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=WRITES_LOCAL_FILES)
def pipeline_drain_swap(old_model: str, new_model: str, timeout_s: float = 30.0) -> dict:
    """Soft-drain in-flight requests then swap models."""
    try:
        return service.pipeline_drain_swap(old_model, new_model, timeout_s=timeout_s).model_dump()
    except ValueError as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: run_agent (Plan 35-02)
# ---------------------------------------------------------------------------
@mcp.tool(annotations=WRITES_LOCAL_FILES)
def run_agent(
    name: str,
    prompt: str,
    project: str = "",
    namespace: str = "",
    tool_name: str = "",
    tool_args_json: str = "{}",
) -> dict:
    """Invoke a named typed agent (helper, executor, macfind) and return AgentReceipt."""
    import json as _json
    try:
        tool_args = _json.loads(tool_args_json) if tool_args_json.strip() else {}
    except Exception:
        tool_args = {}
    try:
        receipt = service.run_agent(
            name,
            prompt,
            project=project or None,
            namespace=namespace or None,
            tool_name=tool_name or None,
            tool_args=tool_args,
        )
        return receipt.model_dump()
    except ValueError as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: read_sibling_file
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
def read_sibling_file(
    path: str,
    namespace_prefix: str | None = None,
) -> dict:
    """Read an allowlisted sibling-project file. Fail-closed on non-allowlisted paths."""
    try:
        receipt = service.read_sibling_file(path, namespace_prefix)
        return receipt.model_dump()
    except ValueError as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: find_on_mac
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
def find_on_mac(
    query: str,
    namespace_prefix: str | None = None,
) -> dict:
    """Semantically search user-approved Mac directories via find_on_mac."""
    try:
        receipt = service.find_on_mac(query, namespace_prefix)
        return receipt.model_dump()
    except ValueError as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: reindex_macfind
# ---------------------------------------------------------------------------
@mcp.tool(annotations=WRITES_LOCAL_FILES)
def reindex_macfind() -> dict:
    """Rebuild the macfind file index from approved directories."""
    try:
        return service.reindex_macfind()
    except ValueError as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: embed_text (RTB-REQ-25 — pinned embed surface)
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
def embed_text(text: str) -> dict:
    """Embed text via the pinned local embed model (keep_alive=-1).

    Returns an EmbedResult; on failure status is "degraded" with a reason_code
    (LOUD), never a silent zero vector.
    """
    return service.embed_text(text).model_dump()


# ---------------------------------------------------------------------------
# Tool: embed_status
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
def embed_status() -> dict:
    """Report the pinned embed model's posture (available/resident/pinned)."""
    return service.embed_status().model_dump()


# ---------------------------------------------------------------------------
# Tool: scribe_progress (token-loss resilience)
# ---------------------------------------------------------------------------
@mcp.tool(annotations=WRITES_LOCAL_FILES)
def scribe_progress(
    project_root: str,
    project: str,
    phase: str = "",
    task: str = "",
    state: str = "in_progress",
    artifacts: list[str] | None = None,
    notes: str = "",
    decisions: list[str] | None = None,
    next_action: str = "",
) -> dict:
    """Log structured progress to disk for token-loss resilience.

    Call this DURING work (not after) so state survives token limit crashes.
    Each call appends to {project_root}/.ollarma/session-log.jsonl and
    regenerates a human-readable RESUME.md.

    Args:
        project_root: Absolute path to the project (e.g., <repo>)
        project: Short project name (e.g., "ollarma", "cellico-bio")
        phase: Current phase identifier (e.g., "39", "v5.0-research")
        task: Current task identifier (e.g., "39-01-T2")
        state: One of: started, in_progress, completed, blocked, paused
        artifacts: Files created or modified in this step
        notes: Freeform context for the next session
        decisions: Key decisions made in this step
        next_action: What to do next if this session dies
    """
    try:
        return service.scribe_progress(
            project_root=project_root,
            project=project,
            phase=phase,
            task=task,
            state=state,
            artifacts=artifacts,
            notes=notes,
            decisions=decisions,
            next_action=next_action,
        )
    except Exception as exc:
        return {"error": str(exc), "written": False}


# ---------------------------------------------------------------------------
# Tool: read_resume (token-loss resilience)
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
def read_resume(project_root: str) -> dict:
    """Read the current RESUME.md for a project. Returns the session resume
    content that was auto-generated by scribe_progress calls.

    Use this to recover context after a token limit crash or session restart.
    """
    try:
        content = service.read_resume(project_root=project_root)
        return {"content": content, "has_resume": bool(content)}
    except Exception as exc:
        return {"error": str(exc), "has_resume": False}


# ---------------------------------------------------------------------------
# Tool: scan_recovery_state (v4.3 — writes packet)
# ---------------------------------------------------------------------------
@mcp.tool(annotations=WRITES_LOCAL_FILES)
def scan_recovery_state(
    project_root: str,
    base_branch: str = "main",
) -> dict:
    """Scan a repo for interrupted agent/model work.

    Writes a deterministic recovery packet to ``.ollarma/incidents/`` and
    returns the packet dict. Use this before starting new bounded execution
    to detect stranded worktrees, sidecar branches ahead of base, and
    resume artifacts from prior sessions.
    """
    try:
        return service.recover_scan(
            project_root=project_root,
            source="mcp",
            base_branch=base_branch,
            persist=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "state": "error", "blocker_code": "SCAN_ERROR"}


# ---------------------------------------------------------------------------
# Tool: read_recovery_report (v4.3 — read-only)
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
def read_recovery_report(project_root: str) -> dict:
    """Read the latest recovery packet without running a new scan.

    Returns ``{"has_report": false}`` when no packet exists.
    """
    try:
        packet = service.recover_latest(project_root=project_root)
        if packet is None:
            return {"has_report": False}
        return {"has_report": True, "packet": packet}
    except Exception as exc:  # noqa: BLE001
        return {"has_report": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Entry point: python -m ollarma.mcp_server
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")
