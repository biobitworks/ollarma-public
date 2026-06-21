"""http_api.py -- Starlette HTTP API exposing ollarma service tools over REST.

Provides benchmark/service endpoints plus a bounded operator dashboard:
  /health, /models, /run, /report, /report/{run_id}, /verify/{run_id}, /projects,
  /chat, /route, /workflow, /autopilot, /dashboard, /dashboard/overview,
  /dashboard/workflows/{project},
  /dashboard/runs/{run_id}, /openapi.json, /docs

CRITICAL CONSTRAINTS:
  - NO imports from rich, typer, or any CLI-only package
  - NO imports from ollarma.cli, ollarma.tools, or ollarma.fleet_tools
  - All route handlers delegate to service.py (thin wrapper pattern)
  - asyncio.Semaphore(1) guards inference routes (M1 Max 32GB: big models sequential-only)
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile
import uuid

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.schemas import SchemaGenerator

import json as _json

from ollarma import __version__
from ollarma.dashboard_html import render_dashboard
from ollarma import service
from ollarma.bridge_events import BridgeEvent, BridgeEventStore, create_bridge_event
from ollarma.conversation_provenance import record_conversation_turn, stable_hash
from ollarma.service import NoModelsError, NoResultsError
from ollarma.scheduler import SchedulerAdmissionError


# ---------------------------------------------------------------------------
# SSE helpers (HARDEN-01)
# ---------------------------------------------------------------------------

async def _sse_response(data: dict) -> StreamingResponse:
    """Wrap a single response dict as an SSE stream (single-chunk + DONE)."""
    async def generate():
        yield f"data: {_json.dumps(data)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")


def _wants_sse(request: Request) -> bool:
    """Return True if client requests SSE via Accept header or ?stream=true."""
    accept = request.headers.get("Accept", "")
    if "text/event-stream" in accept:
        return True
    return request.query_params.get("stream", "").lower() == "true"


# ---------------------------------------------------------------------------
# Backpressure constants
# ---------------------------------------------------------------------------
BACKPRESSURE_CODES = {"RESOURCE_BUDGET_EXCEEDED", "SWAP_DEGRADED"}
RETRY_AFTER_SECONDS = 30


def _backpressure_response(exc: SchedulerAdmissionError) -> JSONResponse | None:
    """Return a 429 JSONResponse with Retry-After if exc is a backpressure code.

    Returns None when the reason code is not a backpressure code (caller should
    handle as a generic 502).  Extracted from /route so /chat and /workflow can
    reuse the same logic (DEBT-06 parity fix, Phase 55).
    """
    if exc.reason_code in BACKPRESSURE_CODES:
        return JSONResponse(
            {"reason_code": exc.reason_code, "retry_after": RETRY_AFTER_SECONDS},
            status_code=429,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    return None


def _reserved_model_policy_response(exc: Exception) -> JSONResponse | None:
    """Return a policy-denial response for reserved-model requests."""
    detail = str(exc)
    if service.RESERVED_MODEL_REASON_CODE not in detail:
        return None
    return JSONResponse(
        {"error": detail, "reason_code": service.RESERVED_MODEL_REASON_CODE},
        status_code=403,
    )


# ---------------------------------------------------------------------------
# Concurrency guard: M1 Max 32GB still runs big models sequential-only (no parallel inference)
# ---------------------------------------------------------------------------
_inference_sem = asyncio.Semaphore(1)
_scheduler = service.get_scheduler()


# ---------------------------------------------------------------------------
# Bridge event helpers (RTB-01)
# ---------------------------------------------------------------------------

def _bridge_event_root() -> pathlib.Path:
    configured = os.environ.get("OLLARMA_BRIDGE_EVENTS_ROOT")
    if configured:
        return pathlib.Path(configured)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return pathlib.Path(tempfile.gettempdir()) / "ollarma-bridge-events-tests" / str(os.getpid())
    return pathlib.Path(os.getcwd())


def _bridge_event_store() -> BridgeEventStore:
    return BridgeEventStore(_bridge_event_root())


def _bounded_text(value: object, *, limit: int = 160) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "..."


def _append_bridge_event(
    event_type: str,
    *,
    source: str,
    payload: dict,
    run_id: str | None = None,
    parent_event_id: str | None = None,
    receipt_refs: tuple[str, ...] = (),
) -> BridgeEvent | None:
    """Append a bridge event best-effort without changing request semantics."""

    try:
        event = create_bridge_event(
            event_type=event_type,
            source=source,  # type: ignore[arg-type]
            payload=payload,
            run_id=run_id,
            parent_event_id=parent_event_id,
            receipt_refs=receipt_refs,
        )
        return _bridge_event_store().append(event)
    except Exception:  # noqa: BLE001 -- bridge observation must not break callers
        return None


def _append_conversation_turn(**kwargs) -> None:
    """Append transcript provenance best-effort without changing API semantics."""

    try:
        record_conversation_turn(repo_root=_bridge_event_root(), **kwargs)
    except Exception:  # noqa: BLE001 -- provenance observation must not break callers
        return None


def _bridge_limit(request: Request) -> int:
    try:
        raw = int(request.query_params.get("limit", "100"))
    except ValueError:
        raw = 100
    return max(0, min(raw, 500))


def _event_parent_id(event: BridgeEvent | None) -> str | None:
    return event.event_id if event is not None else None


def _event_run_id(event: BridgeEvent | None, fallback: str) -> str:
    return event.run_id if event is not None else fallback


def _route_kb_hits(result_payload: dict) -> list[dict]:
    hits: list[dict] = []
    for ref in result_payload.get("evidence_refs") or ():
        if not isinstance(ref, dict):
            continue
        hit: dict = {}
        for key in ("doc_id", "path", "repo_relative", "score", "chunk_id", "line"):
            if key in ref:
                hit[key] = ref[key]
        if hit:
            hits.append(hit)
        if len(hits) >= 5:
            break
    return hits


# ---------------------------------------------------------------------------
# OpenAPI schema generator
# ---------------------------------------------------------------------------
schemas = SchemaGenerator(
    {"openapi": "3.1.0", "info": {"title": "ollarma", "version": __version__}}
)


# ---------------------------------------------------------------------------
# Swagger UI HTML template (CDN-hosted)
# ---------------------------------------------------------------------------
SWAGGER_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>ollarma API</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@latest/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@latest/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({url: '/openapi.json', dom_id: '#swagger-ui'})
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Origin validation middleware
# ---------------------------------------------------------------------------
ALLOWED_ORIGINS = {
    "http://127.0.0.1",
    "http://localhost",
}


class OriginValidationMiddleware(BaseHTTPMiddleware):
    """Reject requests with non-localhost Origin header.

    No Origin header = allowed (curl/scripts do not send Origin).
    Localhost origins (with or without port) = allowed.
    All other origins = 403.
    """

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin")
        if origin is not None:
            # Strip port from origin for comparison.
            # Origin format: "http://host:port" -- split scheme first, then strip port.
            try:
                scheme_rest = origin.split("://", 1)
                if len(scheme_rest) == 2:
                    scheme, host_port = scheme_rest
                    host = host_port.split(":")[0]
                    origin_no_port = f"{scheme}://{host}"
                else:
                    origin_no_port = origin
            except Exception:
                origin_no_port = origin

            if origin_no_port not in ALLOWED_ORIGINS:
                return JSONResponse(
                    {"error": "Origin not allowed"}, status_code=403
                )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Bearer token authentication middleware
# ---------------------------------------------------------------------------
_AUTH_TOKEN = os.environ.get("OLLARMA_AUTH_TOKEN")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Optional bearer token authentication (HARDEN-03).

    Active only when OLLARMA_AUTH_TOKEN env var is set.
    401 on missing/wrong token. No-op when env var is unset.
    """

    async def dispatch(self, request: Request, call_next):
        token = _AUTH_TOKEN
        if not token:
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        if auth_header == f"Bearer {token}":
            return await call_next(request)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def health(request: Request) -> JSONResponse:
    """Health check.
    ---
    responses:
      200:
        description: Server is running
    """
    return JSONResponse(service.get_runtime_health().model_dump(mode="json"))


async def startup_readiness(request: Request) -> JSONResponse:
    """Return the structured startup readiness payload (v4.5 Phase 51).
    ---
    responses:
      200:
        description: StartupReadinessPayload JSON (schema_version=1)
    """
    payload = await asyncio.to_thread(service.get_startup_readiness)
    return JSONResponse(payload.model_dump(mode="json"))


async def list_models(request: Request) -> JSONResponse:
    """List benchmark models and task suites.
    ---
    responses:
      200:
        description: Models and tasks from models.yml
      404:
        description: No models found
    """
    try:
        result = service.list_models_and_tasks()
        return JSONResponse(result.model_dump())
    except NoModelsError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


async def run_benchmark(request: Request) -> JSONResponse:
    """Run benchmarks against Ollama models.
    ---
    responses:
      200:
        description: Benchmark results
      400:
        description: Invalid request or no results
      404:
        description: No models found
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        async with _inference_sem:
            result = await asyncio.to_thread(
                service.run_benchmark,
                dry_run=body.get("dry_run", True),
                models_filter=body.get("models"),
                suites_filter=body.get("suites"),
                trials=body.get("trials", 3),
            )
        return JSONResponse(result.model_dump())
    except NoModelsError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except NoResultsError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def get_report(request: Request) -> JSONResponse:
    """Generate benchmark report.
    ---
    responses:
      200:
        description: Report with rankings and selection tables
      404:
        description: No results found
    """
    run_id = request.path_params.get("run_id")
    try:
        result = service.generate_report(run_id=run_id)
        return JSONResponse(result.model_dump())
    except (NoResultsError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


async def verify_evidence(request: Request) -> JSONResponse:
    """Verify evidence chain for a benchmark run.
    ---
    responses:
      200:
        description: Verification result
      404:
        description: Run not found
      400:
        description: Verification failed
    """
    run_id = request.path_params["run_id"]
    try:
        result = service.verify_evidence(run_id=run_id)
        return JSONResponse(result.model_dump())
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def list_projects(request: Request) -> JSONResponse:
    """List registered fleet projects.
    ---
    responses:
      200:
        description: Project adapter registry
    """
    registry = service.list_projects(service_mode=True)
    return JSONResponse(
        {name: adapter.model_dump() for name, adapter in registry.items()}
    )


async def bridge_events(request: Request) -> JSONResponse:
    """Return a bounded replay of durable bridge events."""

    after_event_id = request.query_params.get("after_event_id") or None
    result = _bridge_event_store().list_events(
        after_event_id=after_event_id,
        limit=_bridge_limit(request),
    )
    return JSONResponse(result.model_dump(mode="json"))


async def bridge_events_stream(request: Request) -> StreamingResponse:
    """Replay bridge events as finite SSE. RTB-01 does not live-tail."""

    after_event_id = request.query_params.get("after_event_id") or None
    result = _bridge_event_store().list_events(
        after_event_id=after_event_id,
        limit=_bridge_limit(request),
    )

    async def generate():
        for event in result.events:
            yield f"event: bridge_event\ndata: {_json.dumps(event.model_dump(mode='json'))}\n\n"
        done = {
            "schema_version": result.schema_version,
            "after_event_id": result.after_event_id,
            "after_event_id_found": result.after_event_id_found,
            "count": len(result.events),
            "limit": result.limit,
        }
        yield f"event: done\ndata: {_json.dumps(done)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


async def chat(request: Request) -> JSONResponse:
    """Send a single prompt to a model.
    ---
    responses:
      200:
        description: Single-turn chat response
      400:
        description: Missing request body fields
      502:
        description: Ollama chat failure
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    message = body.get("message") or body.get("prompt")
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    run_id = str(uuid.uuid4())
    started_event = _append_bridge_event(
        "chat_started",
        source="chat",
        run_id=run_id,
        payload={
            "request_keys": sorted(str(key) for key in body.keys()),
            "message_hash": stable_hash(str(message)),
            "message_length": len(str(message)),
            "requested_model": body.get("model"),
        },
    )
    run_id = _event_run_id(started_event, run_id)

    try:
        async with _inference_sem:
            result = await asyncio.to_thread(
                service.chat_with_model,
                message=message,
                model=body.get("model"),
            )
        result_payload = result.model_dump()
        done_event = _append_bridge_event(
            "chat_done",
            source="chat",
            run_id=run_id,
            parent_event_id=_event_parent_id(started_event),
            payload={
                "model": result_payload.get("model"),
                "status": result_payload.get("status"),
                "reason_code": result_payload.get("reason_code"),
                "response_length": len(str(result_payload.get("response") or "")),
                "result_keys": sorted(result_payload.keys()),
            },
        )
        _append_conversation_turn(
            surface="http_chat",
            role="model",
            prompt_text=str(message),
            response_text=str(result_payload.get("response") or ""),
            model=result_payload.get("model"),
            reason_code=result_payload.get("reason_code"),
            bridge_event_refs=tuple(
                event.event_id for event in (started_event, done_event) if event is not None
            ),
            metadata={"status": result_payload.get("status")},
        )
        if _wants_sse(request):
            return await _sse_response(result_payload)
        return JSONResponse(result_payload)
    except SchedulerAdmissionError as exc:
        _append_bridge_event(
            "blocked",
            source="chat",
            run_id=run_id,
            parent_event_id=_event_parent_id(started_event),
            payload={"reason_code": exc.reason_code, "detail": str(exc)},
        )
        bp = _backpressure_response(exc)
        if bp is not None:
            return bp
        return JSONResponse({"error": str(exc)}, status_code=502)
    except ValueError as exc:
        policy_response = _reserved_model_policy_response(exc)
        if policy_response is not None:
            _append_bridge_event(
                "blocked",
                source="chat",
                run_id=run_id,
                parent_event_id=_event_parent_id(started_event),
                payload={
                    "reason_code": service.RESERVED_MODEL_REASON_CODE,
                    "detail": str(exc),
                },
            )
            return policy_response
        payload = {"error": str(exc)}
        _append_bridge_event(
            "blocked",
            source="chat",
            run_id=run_id,
            parent_event_id=_event_parent_id(started_event),
            payload={"reason_code": "CHAT_FAILED", "detail": str(exc)},
        )
        try:
            payload["helper_chat"] = service.resolve_generic_chat_model(
                body.get("model")
            ).model_dump(mode="json")
        except Exception:
            pass
        return JSONResponse(payload, status_code=502)
    except Exception as exc:
        payload = {"error": str(exc)}
        _append_bridge_event(
            "blocked",
            source="chat",
            run_id=run_id,
            parent_event_id=_event_parent_id(started_event),
            payload={"reason_code": "CHAT_FAILED", "detail": str(exc)},
        )
        try:
            payload["helper_chat"] = service.resolve_generic_chat_model(
                body.get("model")
            ).model_dump(mode="json")
        except Exception:
            pass
        return JSONResponse(payload, status_code=502)


async def route_prompt(request: Request) -> JSONResponse:
    """Route a prompt through a project-scoped fleet agent.
    ---
    responses:
      200:
        description: Project-routed agent response
      400:
        description: Missing request body fields
      404:
        description: Project not found
      502:
        description: Routing failed
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    prompt = body.get("prompt") or body.get("message")
    project = body.get("project")
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)
    if not project:
        return JSONResponse({"error": "project is required"}, status_code=400)

    namespace_prefix = body.get("namespace_prefix") or request.headers.get("X-Namespace-Prefix") or ""
    run_id = str(uuid.uuid4())
    started_event = _append_bridge_event(
        "route_started",
        source="route",
        run_id=run_id,
        payload={
            "request_keys": sorted(str(key) for key in body.keys()),
            "project": project,
            "prompt_hash": stable_hash(str(prompt)),
            "prompt_length": len(str(prompt)),
            "requested_model": body.get("model"),
            "namespace_prefix": namespace_prefix,
        },
    )
    run_id = _event_run_id(started_event, run_id)

    try:
        async with _inference_sem:
            result = await asyncio.to_thread(
                service.route_prompt,
                prompt=prompt,
                project=project,
                model=body.get("model"),
                adapters_dir=body.get("adapters_dir"),
                service_mode=True,
                namespace_prefix=namespace_prefix,
            )
        result_payload = result.model_dump(mode="json")
        parent_event_id = _event_parent_id(started_event)
        route_done = _append_bridge_event(
            "route_done",
            source="route",
            run_id=run_id,
            parent_event_id=parent_event_id,
            payload={
                "project": result_payload.get("project"),
                "model": result_payload.get("model"),
                "lane": result_payload.get("lane"),
                "reason_code": result_payload.get("reason_code"),
                "query_class": result_payload.get("query_class"),
                "kb_status": result_payload.get("kb_status"),
                "evidence_count": len(result_payload.get("evidence_refs") or ()),
                "next_action": result_payload.get("next_action"),
            },
        )
        hits = _route_kb_hits(result_payload)
        if hits:
            _append_bridge_event(
                "kb_hit",
                source="kb",
                run_id=run_id,
                parent_event_id=_event_parent_id(route_done) or parent_event_id,
                payload={"project": project, "hits": hits},
            )
        if result_payload.get("escalation_receipt"):
            _append_bridge_event(
                "escalated",
                source="route",
                run_id=run_id,
                parent_event_id=_event_parent_id(route_done) or parent_event_id,
                payload={
                    "project": project,
                    "reason_code": result_payload.get("reason_code"),
                    "next_action": result_payload.get("next_action"),
                },
            )
        route_receipt_ref = ".ollarma/kb/route_receipts.jsonl" if result_payload.get("route_receipt") else None
        _append_conversation_turn(
            surface="http_route",
            role="model",
            prompt_text=str(prompt),
            response_text=str(result_payload.get("final_response") or ""),
            project=str(project),
            model=result_payload.get("model"),
            lane=result_payload.get("lane"),
            reason_code=result_payload.get("reason_code"),
            bridge_event_refs=tuple(
                event.event_id for event in (started_event, route_done) if event is not None
            ),
            route_receipt_ref=route_receipt_ref,
            kb_evidence_refs=tuple(
                str(ref.get("doc_id") or ref.get("path") or ref.get("chunk_id"))
                for ref in result_payload.get("evidence_refs") or ()
                if isinstance(ref, dict)
            ),
            metadata={
                "query_class": result_payload.get("query_class"),
                "kb_status": result_payload.get("kb_status"),
                "next_action": result_payload.get("next_action"),
            },
        )
        if _wants_sse(request):
            return await _sse_response(result_payload)
        return JSONResponse(result_payload)
    except SchedulerAdmissionError as exc:
        _append_bridge_event(
            "blocked",
            source="route",
            run_id=run_id,
            parent_event_id=_event_parent_id(started_event),
            payload={"project": project, "reason_code": exc.reason_code, "detail": str(exc)},
        )
        bp = _backpressure_response(exc)
        if bp is not None:
            return bp
        return JSONResponse({"error": str(exc)}, status_code=502)
    except ValueError as exc:
        _append_bridge_event(
            "blocked",
            source="route",
            run_id=run_id,
            parent_event_id=_event_parent_id(started_event),
            payload={"project": project, "reason_code": "ROUTE_VALUE_ERROR", "detail": str(exc)},
        )
        policy_response = _reserved_model_policy_response(exc)
        if policy_response is not None:
            return policy_response
        if "UNKNOWN_NAMESPACE" in str(exc):
            return JSONResponse({"error": str(exc), "reason_code": "UNKNOWN_NAMESPACE"}, status_code=400)
        return JSONResponse({"error": str(exc)}, status_code=404)
    except FileNotFoundError as exc:
        _append_bridge_event(
            "blocked",
            source="route",
            run_id=run_id,
            parent_event_id=_event_parent_id(started_event),
            payload={"project": project, "reason_code": "ROUTE_FILE_NOT_FOUND", "detail": str(exc)},
        )
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        _append_bridge_event(
            "blocked",
            source="route",
            run_id=run_id,
            parent_event_id=_event_parent_id(started_event),
            payload={"project": project, "reason_code": "ROUTE_FAILED", "detail": str(exc)},
        )
        return JSONResponse({"error": str(exc)}, status_code=502)


async def kb_status(request: Request) -> JSONResponse:
    """Return read-only KB status for a project."""
    project = request.path_params["project"]
    try:
        result = await asyncio.to_thread(
            service.get_project_kb_status,
            project,
            request.query_params.get("adapters_dir"),
            service_mode=True,
        )
    except (ValueError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result.model_dump(mode="json"))


async def kb_search(request: Request) -> JSONResponse:
    """Return bounded read-only KB hits for a project query."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    project = body.get("project")
    query = body.get("query")
    if not project or not query:
        return JSONResponse({"error": "project and query are required"}, status_code=400)
    limit = body.get("limit", 5)
    if not isinstance(limit, int) or limit < 1:
        return JSONResponse({"error": "limit must be a positive integer"}, status_code=400)

    try:
        result = await asyncio.to_thread(
            service.search_project_kb,
            project,
            query,
            body.get("adapters_dir"),
            service_mode=True,
            limit=limit,
        )
    except (ValueError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result.model_dump(mode="json"))


async def workflow(request: Request) -> JSONResponse:
    """Submit a manifest-backed validated workflow step to the explicit workflow lane.
    ---
    responses:
      200:
        description: Workflow admission metadata
      400:
        description: Missing request body fields
      404:
        description: Project not found
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    project = body.get("project")
    manifest_ref = body.get("manifest_ref")
    step_id = body.get("step_id")
    if not project or not manifest_ref or not step_id:
        return JSONResponse(
            {"error": "project, manifest_ref, and step_id are required"},
            status_code=400,
        )

    try:
        result = await asyncio.to_thread(
            service.submit_workflow,
            project=project,
            manifest_ref=manifest_ref,
            step_id=step_id,
            model=body.get("model"),
            adapters_dir=body.get("adapters_dir"),
        )
        if result.status == "accepted":
            status = 200
        elif result.status == "queued":
            status = 202
        else:
            status = 409
        return JSONResponse(result.model_dump(), status_code=status)
    except SchedulerAdmissionError as exc:
        bp = _backpressure_response(exc)
        if bp is not None:
            return bp
        return JSONResponse({"error": str(exc)}, status_code=502)
    except ValueError as exc:
        policy_response = _reserved_model_policy_response(exc)
        if policy_response is not None:
            return policy_response
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


async def autopilot(request: Request) -> JSONResponse:
    """Run bounded autopilot discovery or execution for one project.
    ---
    responses:
      200:
        description: Autopilot report
      400:
        description: Missing request body fields or invalid options
      404:
        description: Project not found
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    project = body.get("project")
    if not project:
        return JSONResponse({"error": "project is required"}, status_code=400)

    threshold = body.get("threshold", 0.9)
    if not isinstance(threshold, (int, float)):
        return JSONResponse({"error": "threshold must be numeric"}, status_code=400)

    include = body.get("include", [])
    exclude = body.get("exclude", [])
    if not isinstance(include, list) or not all(isinstance(item, str) for item in include):
        return JSONResponse({"error": "include must be a list of strings"}, status_code=400)
    if not isinstance(exclude, list) or not all(isinstance(item, str) for item in exclude):
        return JSONResponse({"error": "exclude must be a list of strings"}, status_code=400)

    try:
        result = await asyncio.to_thread(
            service.submit_autopilot,
            project=project,
            run_assets=bool(body.get("run_assets", False)),
            threshold=float(threshold),
            include=tuple(include),
            exclude=tuple(exclude),
            adapters_dir=body.get("adapters_dir"),
            service_mode=True,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result.model_dump(mode="json"))


async def sibling_read_handler(request: Request) -> JSONResponse:
    """Read an allowlisted sibling-project file.
    ---
    responses:
      200:
        description: SiblingReadReceipt with path, content, namespace
      400:
        description: Missing path or file unreadable
      403:
        description: Path not in allowlisted fleet roots
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    path = body.get("path", "")
    namespace_prefix = body.get("namespace_prefix") or request.headers.get("X-Namespace-Prefix")
    if not path:
        return JSONResponse({"error": "path is required"}, status_code=400)
    try:
        receipt = service.read_sibling_file(path, namespace_prefix)
        return JSONResponse(receipt.model_dump())
    except ValueError as exc:
        reason = str(exc)
        if "SIBLING_NOT_ALLOWLISTED" in reason:
            return JSONResponse({"reason_code": "SIBLING_NOT_ALLOWLISTED"}, status_code=403)
        return JSONResponse({"error": reason}, status_code=400)


async def dashboard_workflows(request: Request) -> JSONResponse:
    """Return discovered workflow manifests and validated steps for one project."""
    project = request.path_params["project"]
    try:
        catalog = await asyncio.to_thread(
            service.list_project_workflows,
            project,
            request.query_params.get("adapters_dir"),
            service_mode=True,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(catalog.model_dump(mode="json"))


async def dashboard_overview(request: Request) -> JSONResponse:
    """Return typed dashboard overview data for the local operator surface."""
    overview = await asyncio.to_thread(
        service.get_dashboard_overview,
        request.query_params.get("adapters_dir"),
    )
    return JSONResponse(overview.model_dump(mode="json"))


async def dashboard_run_detail(request: Request) -> JSONResponse:
    """Return drill-down data for one dashboard run."""
    run_id = request.path_params["run_id"]
    try:
        detail = await asyncio.to_thread(
            service.get_dashboard_run_detail,
            run_id,
            request.query_params.get("adapters_dir"),
        )
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(detail.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Pipeline error code → HTTP status mapping
# ---------------------------------------------------------------------------
_PIPELINE_ERROR_STATUS = {
    "SWAP_DEGRADED": 503,
    "PIPELINE_MODEL_PINNED": 409,
    "BENCHMARK_ACTIVE": 423,
}


async def models_status_handler(request: Request) -> JSONResponse:
    """Return current model pipeline status snapshot.
    ---
    responses:
      200:
        description: ModelStatusSnapshot JSON
    """
    snapshot = service.get_pipeline_status()
    return JSONResponse(snapshot.model_dump())


async def pipelines_warmup_handler(request: Request) -> JSONResponse:
    """Warm up a model (keep_alive=5m).
    ---
    responses:
      202:
        description: PipelineReceipt JSON
      400:
        description: model is required
      503:
        description: SWAP_DEGRADED
      423:
        description: BENCHMARK_ACTIVE
    """
    body = await request.json()
    model = body.get("model", "").strip()
    if not model:
        return JSONResponse({"error": "model is required"}, status_code=400)
    try:
        receipt = service.pipeline_warmup(model)
        return JSONResponse(receipt.model_dump(), status_code=202)
    except ValueError as exc:
        reason = str(exc)
        status = _PIPELINE_ERROR_STATUS.get(reason, 500)
        return JSONResponse({"reason_code": reason}, status_code=status)


async def pipelines_pin_handler(request: Request) -> JSONResponse:
    """Pin a model (increment refcount).
    ---
    responses:
      200:
        description: PipelineReceipt JSON
      400:
        description: model is required
      423:
        description: BENCHMARK_ACTIVE
    """
    body = await request.json()
    model = body.get("model", "").strip()
    if not model:
        return JSONResponse({"error": "model is required"}, status_code=400)
    try:
        receipt = service.pipeline_pin(model)
        return JSONResponse(receipt.model_dump(), status_code=200)
    except ValueError as exc:
        return JSONResponse({"reason_code": str(exc)}, status_code=423)


async def pipelines_evict_handler(request: Request) -> JSONResponse:
    """Evict a model from Ollama memory.
    ---
    responses:
      200:
        description: PipelineReceipt JSON
      400:
        description: model is required
      409:
        description: PIPELINE_MODEL_PINNED
      423:
        description: BENCHMARK_ACTIVE
    """
    body = await request.json()
    model = body.get("model", "").strip()
    if not model:
        return JSONResponse({"error": "model is required"}, status_code=400)
    try:
        receipt = service.pipeline_evict(model)
        return JSONResponse(receipt.model_dump(), status_code=200)
    except ValueError as exc:
        reason = str(exc)
        status = _PIPELINE_ERROR_STATUS.get(reason, 500)
        return JSONResponse({"reason_code": reason}, status_code=status)


async def pipelines_drain_swap_handler(request: Request) -> JSONResponse:
    """Soft-drain in-flight requests then swap models.
    ---
    responses:
      202:
        description: PipelineReceipt JSON
      400:
        description: old_model and new_model are required
      423:
        description: BENCHMARK_ACTIVE
    """
    body = await request.json()
    old_model = body.get("old_model", "").strip()
    new_model = body.get("new_model", "").strip()
    timeout_s = float(body.get("timeout_s", 30.0))
    if not old_model or not new_model:
        return JSONResponse({"error": "old_model and new_model are required"}, status_code=400)
    try:
        receipt = service.pipeline_drain_swap(old_model, new_model, timeout_s=timeout_s)
        return JSONResponse(receipt.model_dump(), status_code=202)
    except ValueError as exc:
        reason = str(exc)
        status = _PIPELINE_ERROR_STATUS.get(reason, 500)
        return JSONResponse({"reason_code": reason}, status_code=status)


async def macfind_query_handler(request: Request):
    """Search macfind index via hybrid BM25+cosine search.
    ---
    responses:
      200:
        description: MacFindReceipt with hits
      400:
        description: query is required
      412:
        description: macfind not configured
    """
    body = await request.json()
    query = body.get("query", "").strip()
    namespace_prefix = body.get("namespace_prefix") or request.headers.get("X-Namespace-Prefix")
    max_results = body.get("max_results")
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)
    try:
        receipt = service.find_on_mac(query, namespace_prefix, max_results=max_results)
        return JSONResponse(receipt.model_dump())
    except ValueError as exc:
        reason = str(exc)
        if "MACFIND_NOT_CONFIGURED" in reason:
            return JSONResponse({"reason_code": "MACFIND_NOT_CONFIGURED"}, status_code=412)
        return JSONResponse({"error": reason}, status_code=400)


async def macfind_reindex_handler(request: Request):
    """Rebuild the macfind file index from approved directories.
    ---
    responses:
      200:
        description: Index stats
      412:
        description: macfind not configured
    """
    try:
        stats = service.reindex_macfind()
        return JSONResponse({"status": "ok", "stats": stats})
    except ValueError as exc:
        reason = str(exc)
        if "MACFIND_NOT_CONFIGURED" in reason:
            return JSONResponse({"reason_code": "MACFIND_NOT_CONFIGURED"}, status_code=412)
        return JSONResponse({"error": reason}, status_code=400)


async def embed_handler(request: Request) -> JSONResponse:
    """Embed text via the pinned local embed model (keep_alive=-1).
    ---
    responses:
      200:
        description: EmbedResult JSON with embedding vector and dim
      400:
        description: text is required
      503:
        description: embed degraded (bridge down / model unavailable / evicted)
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = body.get("text") or body.get("prompt")
    if not text or not str(text).strip():
        return JSONResponse({"error": "text is required"}, status_code=400)

    async with _inference_sem:
        result = await asyncio.to_thread(service.embed_text, str(text))
    payload = result.model_dump()
    if result.status != "ok":
        # LOUD degraded — never a silent zero vector.
        return JSONResponse(payload, status_code=503)
    return JSONResponse(payload)


async def embed_status_handler(request: Request) -> JSONResponse:
    """Report the pinned embed model's posture (available/resident/pinned).
    ---
    responses:
      200:
        description: EmbedStatus JSON
    """
    status = await asyncio.to_thread(service.embed_status)
    return JSONResponse(status.model_dump())


async def agents_run_handler(request: Request) -> Response:
    """Run a named typed agent.
    ---
    responses:
      200:
        description: AgentReceipt JSON
      400:
        description: prompt is required or invalid input
      404:
        description: Unknown agent name
    """
    name = request.path_params["name"]
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)
    try:
        receipt = service.run_agent(
            name,
            prompt,
            project=body.get("project") or None,
            namespace=body.get("namespace") or None,
            tool_name=body.get("tool_name") or None,
            tool_args=body.get("tool_args") or {},
        )
        if _wants_sse(request):
            return await _sse_response(receipt.model_dump())
        return JSONResponse(receipt.model_dump(), status_code=200)
    except ValueError as exc:
        reason = str(exc)
        if "Unknown agent" in reason:
            return JSONResponse({"error": reason}, status_code=404)
        return JSONResponse({"error": reason}, status_code=400)


async def metrics_handler(request: Request) -> Response:
    """Expose Prometheus-format metrics (HARDEN-02)."""
    from ollarma.metrics import get_registry, collect_pipeline_gauges
    collect_pipeline_gauges()
    registry = get_registry()
    registry.counter_inc(
        "ollarma_requests_total",
        {"endpoint": "/metrics", "status": "200"},
        help_text="Total HTTP requests",
    )
    body = registry.expose_text()
    return Response(body, media_type="text/plain; version=0.0.4; charset=utf-8")


async def dashboard(request: Request) -> HTMLResponse:
    """Server-rendered localhost dashboard for operator review."""
    overview = await asyncio.to_thread(
        service.get_dashboard_overview,
        request.query_params.get("adapters_dir"),
    )
    return HTMLResponse(render_dashboard(overview))


async def recovery_status_handler(request: Request) -> JSONResponse:
    """GET /recovery/status -- return the latest recovery packet (404 if none)."""
    try:
        packet = await asyncio.to_thread(service.recover_latest)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    if packet is None:
        return JSONResponse(
            {"error": "no_recovery_packet", "message": "Run POST /recovery/scan first."},
            status_code=404,
        )
    return JSONResponse(packet)


async def recovery_scan_handler(request: Request) -> JSONResponse:
    """POST /recovery/scan -- run a scan, persist a packet, return it."""
    try:
        body: dict = {}
        if request.headers.get("content-length", "0") not in ("", "0"):
            try:
                body = await request.json()
            except Exception:
                body = {}
        base_branch = body.get("base_branch", "main") if isinstance(body, dict) else "main"
        packet = await asyncio.to_thread(
            service.recover_scan, None, "http", base_branch, True,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(packet)


async def gateway_submit(request: Request) -> JSONResponse:
    """Phase 57 gateway ingress endpoint.
    ---
    responses:
      200:
        description: FrontierReceipt JSON. status=disabled when gateway.enabled=false (D-10); status=dry_run when dry_run requested (D-13, D-15).
      400:
        description: Malformed or missing escalation_receipt (GATE-02, D-12); reject admission persisted to the audit stream.
      501:
        description: gateway.enabled=true with dry_run=false; Phase 57 ships substrate only. Phase 59 lands the Anthropic adapter.
    """
    try:
        body = await request.json()
    except _json.JSONDecodeError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # Resolve dry_run: query-param takes precedence over body field.
    dry_run_override: bool | None = None
    qp = request.query_params.get("dry_run", "").lower()
    if qp in {"true", "1", "yes"}:
        dry_run_override = True
    elif qp in {"false", "0", "no"}:
        dry_run_override = False
    elif isinstance(body.get("dry_run"), bool):
        dry_run_override = body["dry_run"]

    # Phase 58-01: optional ``virtual_key_id`` on the request body (D-58-05).
    # Not required for disabled / dry-run paths (they short-circuit before
    # admission). Required for enabled + non-dry-run path or the admission
    # pipeline will reject with VIRTUAL_KEY_UNKNOWN.
    vk_id_raw = body.get("virtual_key_id")
    virtual_key_id = vk_id_raw if isinstance(vk_id_raw, str) and vk_id_raw else None

    try:
        result = await asyncio.to_thread(
            service.submit_gateway_request,
            body,
            dry_run_override=dry_run_override,
            virtual_key_id=virtual_key_id,
        )
    except service.GatewayInputError as exc:
        # GATE-02 + D-12 -- 400 with structured envelope. Reject admission was
        # persisted inside submit_gateway_request before the exception raised.
        return JSONResponse(
            {
                "error": "GATEWAY_INPUT_INVALID",
                "reason_code": "GATEWAY_INPUT_INVALID",
                "reason_detail": str(exc)[:500],
            },
            status_code=400,
        )
    except NotImplementedError as exc:
        # Phase 57 boundary: enabled=true + dry_run=false is not implemented
        # until Phase 59. Explicit 501 (NOT 500, NOT a silent stub) -- the
        # loud-fail behavior v4.5 retro-review identified as the pattern to
        # keep.
        return JSONResponse(
            {
                "error": "GATEWAY_NOT_IMPLEMENTED",
                "reason_detail": str(exc)[:500],
                "phase": "57",
                "next_phase_with_provider": "59",
            },
            status_code=501,
        )

    # Phase 58-01 admission status mapping (D-58-05). When admission rejected
    # the request, the service layer returned a FrontierReceipt dict with
    # ``status="failed"`` and one of the admission-stage reason codes; map it
    # to the appropriate HTTP status. Other responses (disabled / dry_run)
    # keep Phase 57's 200 semantics (D-10).
    #
    # Phase 58-02 additions: rate-cap rejections map to HTTP 429 with a
    # ``Retry-After`` header (integer seconds, RFC 7231). The service layer
    # surfaces ``retry_after_seconds`` in the response dict for this purpose
    # (non-persisted; FrontierReceipt is frozen).
    admission_status_map = {
        "PROJECT_NOT_ALLOWED": 403,
        "VIRTUAL_KEY_UNKNOWN": 400,
        "VIRTUAL_KEY_KEYCHAIN_MISS": 400,
        "RATE_CAP_REQ_PER_MIN_EXCEEDED": 429,
        "RATE_CAP_TOKENS_PER_DAY_EXCEEDED": 429,
    }
    status_code = 200
    headers: dict[str, str] = {}
    if isinstance(result, dict) and result.get("status") == "failed":
        reason_code = result.get("reason_code")
        if isinstance(reason_code, str) and reason_code in admission_status_map:
            status_code = admission_status_map[reason_code]
            if status_code == 429:
                retry_after = result.get("retry_after_seconds")
                if isinstance(retry_after, int) and retry_after > 0:
                    headers["Retry-After"] = str(retry_after)
                else:
                    # Safe default if enforcer didn't supply a value.
                    headers["Retry-After"] = "60"
    return JSONResponse(result, status_code=status_code, headers=headers or None)


async def openapi_schema(request: Request) -> JSONResponse:
    """OpenAPI 3.1.0 schema."""
    schema = schemas.get_schema(routes=app.routes)
    return JSONResponse(schema)


async def docs(request: Request) -> HTMLResponse:
    """Swagger UI documentation."""
    return HTMLResponse(SWAGGER_HTML)


# ---------------------------------------------------------------------------
# App lifespan (v4.5 Phase 51 PERSIST-02)
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager  # noqa: E402 — positional with Starlette block


@asynccontextmanager
async def lifespan(_app: "Starlette"):
    """Starlette lifespan: apply residency policy once, then refresh startup readiness.

    Two startup actions (both best-effort — failures are swallowed so a disk
    issue or model-load failure cannot prevent the service accepting requests):

    1. ``apply_residency_policy_once`` — pins rescue model / warms opportunistic
       model / evicts under swap pressure per the Phase 52 GPU residency policy.
    2. ``refresh_startup_readiness`` — builds + persists the readiness JSON after
       the best-effort residency mutation, so ``/startup/readiness`` reflects the
       post-policy posture.
    """
    try:
        await asyncio.to_thread(service.apply_residency_policy_once)
    except Exception:  # noqa: BLE001 — residency policy is best-effort at startup
        pass
    try:
        await asyncio.to_thread(service.refresh_startup_readiness)
    except Exception:  # noqa: BLE001 — lifespan must never crash the app
        pass
    yield


# ---------------------------------------------------------------------------
# Starlette application
# ---------------------------------------------------------------------------
app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/health", health),
        Route("/startup/readiness", startup_readiness),
        Route("/models", list_models),
        Route("/run", run_benchmark, methods=["POST"]),
        Route("/report", get_report),
        Route("/report/{run_id}", get_report),
        Route("/verify/{run_id}", verify_evidence),
        Route("/projects", list_projects),
        Route("/bridge/events", bridge_events, methods=["GET"]),
        Route("/bridge/events/stream", bridge_events_stream, methods=["GET"]),
        Route("/chat", chat, methods=["POST"]),
        Route("/route", route_prompt, methods=["POST"]),
        Route("/kb/status/{project}", kb_status),
        Route("/kb/search", kb_search, methods=["POST"]),
        Route("/workflow", workflow, methods=["POST"]),
        Route("/autopilot", autopilot, methods=["POST"]),
        Route("/v1/sibling/read", sibling_read_handler, methods=["POST"]),
        Route("/models/status", models_status_handler, methods=["GET"]),
        Route("/pipelines/warmup", pipelines_warmup_handler, methods=["POST"]),
        Route("/pipelines/pin", pipelines_pin_handler, methods=["POST"]),
        Route("/pipelines/evict", pipelines_evict_handler, methods=["POST"]),
        Route("/pipelines/drain-swap", pipelines_drain_swap_handler, methods=["POST"]),
        Route("/macfind/query", macfind_query_handler, methods=["POST"]),
        Route("/macfind/reindex", macfind_reindex_handler, methods=["POST"]),
        Route("/embed", embed_handler, methods=["POST"]),
        Route("/embed/status", embed_status_handler, methods=["GET"]),
        Route("/agents/{name}/run", agents_run_handler, methods=["POST"]),
        Route("/metrics", metrics_handler, methods=["GET"]),
        Route("/recovery/status", recovery_status_handler, methods=["GET"]),
        Route("/recovery/scan", recovery_scan_handler, methods=["POST"]),
        Route("/gateway/submit", gateway_submit, methods=["POST"]),
        Route("/dashboard", dashboard),
        Route("/dashboard/overview", dashboard_overview),
        Route("/dashboard/workflows/{project}", dashboard_workflows),
        Route("/dashboard/runs/{run_id}", dashboard_run_detail),
        Route("/openapi.json", openapi_schema),
        Route("/docs", docs),
    ],
    middleware=[
        Middleware(OriginValidationMiddleware),
        Middleware(BearerAuthMiddleware),
    ],
)
