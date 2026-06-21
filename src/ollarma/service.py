"""service.py -- Pure business logic layer for ollarma.

Provides 7 public functions, 6 Pydantic response models, and 3 error classes.
This module is the shared entry point for CLI, MCP server, and HTTP API.

CRITICAL CONSTRAINTS:
  - NO imports from rich, typer, or any CLI-only package
  - NO print(), console.print(), or any stdout/stderr writes
    (except via the on_progress callback in run_benchmark)
  - NO sys.exit() or typer.Exit() -- use exceptions for error paths
  - All functions return Pydantic BaseModel instances (or dict for list_projects)
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import pathlib
import re
import shutil
import time
from typing import TYPE_CHECKING, Callable, Literal, Optional

if TYPE_CHECKING:  # pragma: no cover
    from ollarma.routing_ladder import LadderDecision

import ollama
import orjson
from pydantic import BaseModel, ConfigDict, Field

from ollarma.agent import resolve_default_model
from ollarma.dashboard import (
    DashboardBoundaryInfo,
    DashboardCitation,
    DashboardCheckpointSummary,
    DashboardCommandHint,
    DashboardGatewayAdmission,
    DashboardGatewayPosture,
    DashboardGatewayReceipt,
    DashboardKBStatus,
    DashboardModelOption,
    DashboardWorkflowCatalog,
    DashboardWorkflowManifest,
    DashboardWorkflowStep,
    DashboardReadinessItem,
    DashboardResource,
    DashboardRouteReceipt,
    DashboardOverview,
    DashboardReceiptPreview,
    DashboardRunDetail,
    DashboardRunSummary,
)
from ollarma.discovery import (
    discover_entry_point_adapters,
    resolve_adapters_dir,
    validate_adapter_path,
)
from ollarma.autopilot import AutopilotReport, run_autopilot
from ollarma.escalation import ReasonCode, build_escalation_receipt
from ollarma.execution_policy import (
    RouteQueryClass,
    RouteReasonCode,
    SelectionResolutionError,
    WorkloadClass,
    classify_route_prompt,
    is_exact_answer_query,
    requires_execution_handoff,
    resolve_selection,
    resolve_ranked_selection,
)
from ollarma.evidence import (
    build_receipt_chain,
    build_selection_artifact,
    canonical_hash,
    verify_evidence_chain,
    write_artifact_file,
    write_evidence_file,
)
from ollarma.executor import (
    BenchmarkResult,
    list_present_models,
    model_is_present,
    run_inference,
)
from ollarma.fleet import AdapterConfig, load_fleet_registry, resolve_project
from ollarma.guards import preflight_check, warmup_model, PreflightError
from ollarma.kb_contract import (
    ProjectKnowledgeContract,
    build_project_knowledge_contract,
)
from ollarma.kb import KBBuildArtifacts, build_kb_artifacts
from ollarma.kb_search import KBSearchResult, KBStatus, load_kb_status, search_kb
from ollarma.registry import load_models, load_tasks, ModelConfig, TaskConfig
from ollarma.reporter import (
    ModelSuiteStats,
    aggregate_trials,
    find_latest_sealed,
    load_sealed_results,
    render_results_md,
    render_selection_md,
)
from ollarma.run_ledger import (
    CheckpointState,
    RunReceipt,
    append_run_receipt,
    apply_receipt_to_checkpoint,
    load_autopilot_run_detail,
    load_workflow_run_detail,
    list_autopilot_runs,
    list_workflow_runs,
    resume_workflow_run as resolve_workflow_resume,
    write_checkpoint_state,
)
from ollarma.idempotency import get_store, make_compound_key
from ollarma.namespace_registry import NAMESPACE_REGISTRY
from ollarma.scheduler import JobRequest, Lane, RuntimeSnapshot, Scheduler
from ollarma.scheduler import SchedulerAdmissionError
from ollarma.scorers import score_bigcodebench, score_code, score_mteb, score_science, score_swarm
from ollarma.sonifier import sonify_chain, sonify_comparison, validate_receipts
from ollarma.store import ResultStore
from ollarma.stage_router import WorkflowStage, determine_next_stage
from ollarma.workflow_manifest import ManifestRefInput, ManifestStep, ValidatedWorkflowManifest, load_workflow_manifest


# ---------------------------------------------------------------------------
# Phase 53: module-level in-process cache for the last routing ladder decision.
# Single-entry; updated every time _route_prompt_impl resolves a ladder.
# Read by get_runtime_health() and _build_dashboard_readiness() for OBS-02/03.
# ---------------------------------------------------------------------------

_last_ladder_decision: "LadderDecision | None" = None

LOCAL_INFERENCE_TIMEOUT_REASON_CODE = "LOCAL_INFERENCE_TIMEOUT"
_DEFAULT_LOCAL_INFERENCE_TIMEOUT_S = 35.0

# Reserved-model policy lives in the leaf module ollarma.reserved_models so that
# routing_ladder.py and execution_policy.py can share it without importing service.py
# (which would be circular). Re-exported here for the existing service call sites/tests.
from ollarma.reserved_models import (  # noqa: E402
    RESERVED_ANTIGENCE_SENTINEL_MODELS,
    RESERVED_MODEL_REASON_CODE,
    assert_model_not_reserved as _reserved_assert_model_not_reserved,
    drop_reserved_models as _reserved_drop_reserved_models,
    is_reserved_model as _reserved_is_reserved_model,
)


def _set_last_ladder_decision(decision: "LadderDecision") -> None:
    global _last_ladder_decision  # noqa: PLW0603
    _last_ladder_decision = decision


def get_last_ladder_decision() -> "LadderDecision | None":
    """Return the most recent in-process routing ladder decision (may be None)."""
    return _last_ladder_decision


def _local_inference_timeout_s() -> float:
    raw = os.environ.get("OLLARMA_LOCAL_INFERENCE_TIMEOUT_SECONDS")
    if raw is None:
        return _DEFAULT_LOCAL_INFERENCE_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_LOCAL_INFERENCE_TIMEOUT_S
    return max(1.0, value)


def _ollama_client_for_local_inference() -> ollama.Client:
    """Build an Ollama SDK client with a finite request timeout.

    The SDK accepts httpx-style keyword arguments. The TypeError fallback keeps
    older test doubles and older SDKs usable, but production SDKs get the
    bounded request behavior.
    """

    timeout_s = _local_inference_timeout_s()
    try:
        return ollama.Client(timeout=timeout_s)
    except TypeError:
        return ollama.Client()


def _is_local_inference_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    module = type(exc).__module__.lower()
    text = str(exc).lower()
    return (
        "timeout" in name
        or "timed out" in text
        or ("httpx" in module and "timeout" in name)
    )


# ---------------------------------------------------------------------------
# Error classes
# ---------------------------------------------------------------------------


class BenchmarkError(Exception):
    """Base exception for benchmark service errors."""


class NoModelsError(BenchmarkError):
    """No models found in models.yml."""


class NoResultsError(BenchmarkError):
    """No sealed results found."""


# Re-export the canonical GatewayInputError from gateway_client so that both
# ``ollarma.service.GatewayInputError`` and ``ollarma.gateway_client.GatewayInputError``
# resolve to the SAME class (Phase 57 WR-02 convergence). Prior state: service.py
# defined its own GatewayInputError(Exception) that was unrelated to the
# gateway_client.GatewayInputError(GatewayError) — a consumer catching one would
# silently miss the other. Never bare-caught per DEBT-10.
from ollarma.gateway_client import GatewayInputError  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class ModelsAndTasks(BaseModel):
    """Response for list_models_and_tasks."""
    models: list[ModelConfig]
    tasks: list[TaskConfig]

    model_config = ConfigDict(frozen=True)


class BenchmarkRunResult(BaseModel):
    """Response for run_benchmark."""
    run_id: str
    rows_written: int
    sealed_path: str
    evidence_path: str
    receipt_count: int
    results: list[BenchmarkResult]
    dry_run: bool

    model_config = ConfigDict(frozen=True)


class ReportResult(BaseModel):
    """Response for generate_report."""
    rows_loaded: int
    stats: list[ModelSuiteStats]
    results_md: str
    selection_md: str
    evidence_root: str
    artifact_hash: str

    model_config = ConfigDict(frozen=True)


class VerifyResult(BaseModel):
    """Response for verify_evidence."""
    valid: bool
    receipt_count: int
    evidence_root: str
    artifact_valid: Optional[bool] = None
    artifact_hash: Optional[str] = None

    model_config = ConfigDict(frozen=True)


class SonifyResult(BaseModel):
    """Response for sonify_evidence."""
    wav_bytes: bytes
    receipt_count: int
    duration_s: float
    tampered_positions: list[int]
    mode: str  # "single" or "compare"

    model_config = ConfigDict(frozen=True)


class EscalationResult(BaseModel):
    """Response for generate_escalation."""
    items: list[dict]
    count: int

    model_config = ConfigDict(frozen=True)


class ChatResult(BaseModel):
    """Response for single-turn chat requests."""
    response: str
    model: str
    status: str = "answered"
    reason_code: str | None = None
    detail: str | None = None
    recovery_commands: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class HelperModelResolution(BaseModel):
    """Runtime helper-model resolution state for generic chat."""

    requested_model: str | None = None
    effective_model: str | None = None
    status: str
    reason_code: str | None = None
    detail: str | None = None
    recovery_commands: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class SelectionHealth(BaseModel):
    """Deterministic readiness for one selection-backed workload class."""

    workload_class: str
    status: str
    model: str | None = None
    reason_code: str | None = None
    detail: str | None = None
    recovery_commands: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class RuntimeHealthResult(BaseModel):
    """HTTP/runtime health payload with helper readiness details."""

    status: str
    helper_chat: HelperModelResolution
    chat_selection: SelectionHealth
    route_selection: SelectionHealth
    project_count: int
    # v4.5 Phase 51 PERSIST-02: structured startup readiness.
    # Optional here so legacy callers stay working; always populated by
    # get_runtime_health() in practice.
    startup_readiness: "StartupReadinessPayload | None" = None
    # Phase 53 OBS-02/03: compact last routing ladder summary for /health.
    # None until a route_prompt call has been made in this process.
    last_routing_ladder: dict | None = None

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Startup readiness contract (v4.5 Phase 51 PERSIST-02)
# ---------------------------------------------------------------------------

_STARTUP_STATUS_PRIORITY = {"ready": 0, "degraded": 1, "blocked": 2}


class StartupReadinessCheck(BaseModel):
    """One probe result inside the startup readiness payload."""

    name: str
    status: str  # "ready" | "degraded" | "blocked"
    detail: str = ""
    reason_code: str | None = None

    model_config = ConfigDict(frozen=True)


class StartupModelAvailability(BaseModel):
    """Helper/chat model resolution posture at startup."""

    status: str
    effective_model: str | None = None
    reason_code: str | None = None
    detail: str = ""

    model_config = ConfigDict(frozen=True)


class StartupSwapPosture(BaseModel):
    """Swap pressure at startup."""

    status: str
    swap_used_mb: float | None = None
    threshold_mb: float
    reason_code: str | None = None

    model_config = ConfigDict(frozen=True)


class StartupAdmissionPosture(BaseModel):
    """Recovery admission state at startup."""

    enabled: bool
    detail: str = ""

    model_config = ConfigDict(frozen=True)


class StartupPipelinePosture(BaseModel):
    """Pipeline residency at startup (read-only reporting in Phase 51)."""

    loaded_model_count: int = 0
    loaded_models: tuple[str, ...] = ()
    telemetry_source: str = "unavailable"

    model_config = ConfigDict(frozen=True)


class StartupResidencyPosture(BaseModel):
    """GPU residency policy outcome at startup (Phase 52 GPU-01..04, OBS-01)."""

    state: str = "unknown"              # "ready" | "degraded" | "blocked" | "unknown"
    rescue_target: str = ""
    rescue_resident: bool = False
    opportunistic_target: str | None = None
    opportunistic_resident: bool = False
    swap_used_mb: float | None = None
    reason_code: str | None = None
    next_action: str = "hold"

    model_config = ConfigDict(frozen=True)


class GatewayPosture(BaseModel):
    """Gateway posture sub-payload for StartupReadinessPayload (Plan 57.1-02, F-05).

    Additive optional field; StartupReadinessPayload.schema_version stays at 1.
    ``virtual_keys_configured`` is a COUNT only — no vk identifier strings ever
    appear in the readiness payload (sibling-project isolation, D-57.1-02).
    """

    enabled: bool = False
    allowlist_size: int = 0
    virtual_keys_configured: int = 0
    rate_cap_state: Literal["not_configured", "within_caps", "degraded"] = "not_configured"
    admissions_today: int = 0
    receipts_today: int = 0

    model_config = ConfigDict(frozen=True)


class StartupReadinessPayload(BaseModel):
    """Deterministic startup readiness payload (schema_version=1)."""

    schema_version: int = 1
    service_label: str = "com.byron.ollarma"
    generated_at: str
    status: str  # "ready" | "degraded" | "blocked"
    checks: tuple[StartupReadinessCheck, ...] = ()
    model_availability: StartupModelAvailability
    swap: StartupSwapPosture
    admission: StartupAdmissionPosture
    pipeline: StartupPipelinePosture
    # Optional — populated by Phase 52 residency policy; None for backwards compat.
    residency: "StartupResidencyPosture | None" = None
    # Optional — populated by Plan 57.1-02; None for pre-v5.0 callers.
    gateway: "GatewayPosture | None" = None
    next_fix_commands: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


def aggregate_startup_status(
    checks: "tuple[StartupReadinessCheck, ...] | list[StartupReadinessCheck]",
) -> str:
    """Return the worst-severity status across ``checks``.

    Severity ordering: blocked > degraded > ready.
    Unknown/missing statuses are treated as ``degraded`` (fail-noisy).
    """
    if not checks:
        return "ready"
    worst = "ready"
    for c in checks:
        s = c.status if c.status in _STARTUP_STATUS_PRIORITY else "degraded"
        if _STARTUP_STATUS_PRIORITY[s] > _STARTUP_STATUS_PRIORITY[worst]:
            worst = s
    return worst


def startup_readiness_path(root: pathlib.Path | None = None) -> pathlib.Path:
    """Return the canonical readiness JSON path.

    Default: ``<cwd>/.ollarma/startup/readiness.json``. Tests can pass an
    explicit ``root`` to redirect.
    """
    base = pathlib.Path(root) if root else pathlib.Path.cwd()
    return base / ".ollarma" / "startup" / "readiness.json"


def _check_model_availability() -> tuple[StartupModelAvailability, StartupReadinessCheck]:
    helper = resolve_generic_chat_model()
    status = helper.status
    # GPU-08: resolve_generic_chat_model emits several "good" statuses that
    # should all register as ready — "ready" (legacy), "selection_ready"
    # (happy path after a fresh benchmark artifact), and "explicit" (operator
    # passed a model directly). "fallback_ready" remains degraded because it
    # means selection resolution failed and we are on a probed fallback.
    # Anything else ("blocked" / unknown) is a true block.
    if status in ("ready", "selection_ready", "explicit"):
        posture_status = "ready"
        check_status = "ready"
        reason = None
    elif status == "fallback_ready":
        posture_status = "degraded"
        check_status = "degraded"
        reason = helper.reason_code or "MODEL_FALLBACK"
    else:
        posture_status = "blocked"
        check_status = "blocked"
        reason = helper.reason_code or "MODEL_UNAVAILABLE"
    posture = StartupModelAvailability(
        status=posture_status,
        effective_model=helper.effective_model,
        reason_code=reason,
        detail=helper.detail or "",
    )
    check = StartupReadinessCheck(
        name="model_availability",
        status=check_status,
        detail=helper.detail or "",
        reason_code=reason,
    )
    return posture, check


def _check_swap_posture() -> tuple[StartupSwapPosture, StartupReadinessCheck]:
    import logging  # noqa: PLC0415
    from ollarma.scheduler import SWAP_DEGRADED_THRESHOLD_MB  # noqa: PLC0415
    from ollarma.guards import collect_runtime_telemetry  # noqa: PLC0415

    try:
        telemetry = collect_runtime_telemetry()
        swap_mb = telemetry.swap_used_mb
    except (OSError, ValueError, RuntimeError) as exc:
        swap_mb = None
        logging.getLogger(__name__).warning(
            "swap telemetry collection failed: %s: %s", type(exc).__name__, exc,
        )

    threshold = float(SWAP_DEGRADED_THRESHOLD_MB)
    if swap_mb is None:
        posture_status = "degraded"
        reason = "SWAP_UNKNOWN"
        detail = "swap telemetry unavailable"
    elif swap_mb > threshold:
        posture_status = "degraded"
        reason = "SWAP_DEGRADED"
        detail = f"swap_used_mb={swap_mb:.0f} exceeds threshold {threshold:.0f}MB"
    else:
        posture_status = "ready"
        reason = None
        detail = f"swap_used_mb={swap_mb:.0f} within threshold {threshold:.0f}MB"
    posture = StartupSwapPosture(
        status=posture_status,
        swap_used_mb=swap_mb,
        threshold_mb=threshold,
        reason_code=reason,
    )
    check = StartupReadinessCheck(
        name="swap_posture",
        status=posture_status,
        detail=detail,
        reason_code=reason,
    )
    return posture, check


def _check_admission_posture() -> tuple[StartupAdmissionPosture, StartupReadinessCheck]:
    from ollarma import admission as _admission  # noqa: PLC0415
    enabled = _admission.admission_enabled()
    posture = StartupAdmissionPosture(
        enabled=enabled,
        detail="fail-closed recovery admission is ON" if enabled else "admission disabled by OLLARMA_RECOVERY_ADMISSION=off",
    )
    # Admission opt-out isn't automatically degraded — it's informational.
    # Only "blocked" if admission itself errored, which we don't probe here.
    check = StartupReadinessCheck(
        name="admission_posture",
        status="ready" if enabled else "degraded",
        detail=posture.detail,
        reason_code=None if enabled else "ADMISSION_DISABLED",
    )
    return posture, check


def _check_pipeline_posture() -> tuple[StartupPipelinePosture, StartupReadinessCheck]:
    from ollarma.guards import collect_runtime_telemetry  # noqa: PLC0415
    try:
        telemetry = collect_runtime_telemetry()
        posture = StartupPipelinePosture(
            loaded_model_count=telemetry.loaded_model_count,
            loaded_models=tuple(telemetry.loaded_models),
            telemetry_source=telemetry.telemetry_source,
        )
    except Exception:  # noqa: BLE001
        posture = StartupPipelinePosture(telemetry_source="error")

    # Phase 51 reports posture only. No automatic residency decisions.
    check = StartupReadinessCheck(
        name="pipeline_posture",
        status="ready",
        detail=f"loaded_model_count={posture.loaded_model_count} source={posture.telemetry_source}",
    )
    return posture, check


def _startup_fix_commands(
    checks: tuple[StartupReadinessCheck, ...],
) -> tuple[str, ...]:
    """Return deterministic operator commands for any non-ready check."""
    fixes: list[str] = []
    for c in checks:
        if c.status == "ready":
            continue
        if c.reason_code == "MODEL_UNAVAILABLE" or c.name == "model_availability":
            fixes.append("ollarma run --suites code --trials 3")
            fixes.append("ollarma report --run-id <fresh_run_id>")
        elif c.reason_code in ("SWAP_DEGRADED", "SWAP_UNKNOWN"):
            fixes.append("vm_stat | head  # inspect macOS memory pressure")
            fixes.append("sudo purge      # release file cache if needed")
        elif c.reason_code == "ADMISSION_DISABLED":
            fixes.append("unset OLLARMA_RECOVERY_ADMISSION  # restore fail-closed admission")
    if any(c.status != "ready" for c in checks):
        fixes.append("launchctl print gui/$(id -u)/com.byron.ollarma")
        fixes.append("curl -sS http://127.0.0.1:8484/startup/readiness | jq")
    # Dedupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for f in fixes:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return tuple(out)


def _build_gateway_posture(repo_root: pathlib.Path | None = None) -> GatewayPosture:
    """Compute gateway posture for the readiness payload (Plan 57.1-02, OBS-57).

    All reads are defensive — missing files yield zero-count defaults so a
    fresh deployment without any gateway config returns a coherent disabled
    posture. The ``virtual_keys_configured`` field is a COUNT only; no vk
    identifier strings cross into the readiness payload (D-57.1-02, F-05).

    ``rate_cap_state`` returns ``"not_configured"`` when no
    ``.ollarma/gateway/rate_state.json`` exists (Phase 58-02 ships that
    surface); if the file is present but unparseable, the state folds to
    ``"not_configured"`` after ``warnings.warn``.
    """
    import json as _json  # noqa: PLC0415
    import warnings as _warnings  # noqa: PLC0415

    root = pathlib.Path(repo_root) if repo_root is not None else pathlib.Path.cwd()

    # --- Config: features.gateway --------------------------------------------
    enabled = False
    allowlist_size = 0
    virtual_keys_configured = 0
    config_path = root / ".planning" / "config.json"
    data: object | None = None
    try:
        raw = config_path.read_text(encoding="utf-8")
        data = _json.loads(raw)
    except FileNotFoundError:
        data = None
    except (_json.JSONDecodeError, OSError) as exc:
        _warnings.warn(
            f"gateway posture: config unreadable at {config_path} ({exc})",
            RuntimeWarning,
            stacklevel=2,
        )
        data = None
    if isinstance(data, dict):
        features = data.get("features")
        if isinstance(features, dict):
            gateway = features.get("gateway")
            if isinstance(gateway, dict):
                enabled = bool(gateway.get("enabled", False))
                allowlist = gateway.get("allowlist")
                if isinstance(allowlist, list):
                    allowlist_size = sum(1 for s in allowlist if isinstance(s, str))
                vks = gateway.get("virtual_keys")
                if isinstance(vks, list):
                    virtual_keys_configured = sum(
                        1 for v in vks if isinstance(v, dict) and isinstance(v.get("id"), str)
                    )

    # --- Stream counts: today's UTC date --------------------------------------
    today_utc = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    admissions_today = _count_gateway_entries_for_date(
        root / ".ollarma" / "gateway" / "admissions.jsonl", today_utc
    )
    receipts_today = _count_gateway_entries_for_date(
        root / ".ollarma" / "gateway" / "receipts.jsonl", today_utc
    )

    # --- Rate-cap state (Phase 58-02 surface) ---------------------------------
    rate_state_path = root / ".ollarma" / "gateway" / "rate_state.json"
    rate_cap_state: str = "not_configured"
    if rate_state_path.exists():
        try:
            raw_rs = rate_state_path.read_text(encoding="utf-8")
            rs = _json.loads(raw_rs)
        except (FileNotFoundError, _json.JSONDecodeError, OSError) as exc:
            _warnings.warn(
                f"gateway posture: rate_state unreadable at {rate_state_path} ({exc})",
                RuntimeWarning,
                stacklevel=2,
            )
            rs = None
        if isinstance(rs, dict):
            state_raw = rs.get("state")
            if state_raw in ("within_caps", "degraded", "not_configured"):
                rate_cap_state = state_raw  # type: ignore[assignment]

    return GatewayPosture(
        enabled=enabled,
        allowlist_size=allowlist_size,
        virtual_keys_configured=virtual_keys_configured,
        rate_cap_state=rate_cap_state,  # type: ignore[arg-type]
        admissions_today=admissions_today,
        receipts_today=receipts_today,
    )


def _count_gateway_entries_for_date(path: pathlib.Path, iso_date: str) -> int:
    """Count JSONL entries whose ``created_at`` starts with ``iso_date``.

    Missing file, unreadable file, or unparseable lines contribute zero; never
    raises. The ``created_at`` field format is ``"%Y-%m-%dT%H:%M:%SZ"`` (set by
    ollarma.gateway._utc_now_iso); ISO prefix matching on the first 10 chars
    is sufficient to isolate one UTC calendar day.
    """
    import json as _json  # noqa: PLC0415

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    except OSError:
        return 0

    count = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            obj = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        created_at = obj.get("created_at")
        if isinstance(created_at, str) and created_at.startswith(iso_date):
            count += 1
    return count


def _build_residency_posture() -> "StartupResidencyPosture":
    """Build a StartupResidencyPosture from a live residency decision (read-only probe)."""
    try:
        decision = get_residency_decision()
        return StartupResidencyPosture(
            state=decision.state,
            rescue_target=decision.rescue_target,
            rescue_resident=decision.rescue_resident,
            opportunistic_target=decision.opportunistic_target,
            opportunistic_resident=decision.opportunistic_resident,
            swap_used_mb=decision.swap_used_mb,
            reason_code=decision.reason_code,
            next_action=decision.next_action,
        )
    except Exception:  # noqa: BLE001
        return StartupResidencyPosture(state="unknown")


def build_startup_readiness(
    *,
    adapters_dir: str | None = None,  # noqa: ARG001 -- reserved for future project probe
    repo_root: pathlib.Path | None = None,
) -> StartupReadinessPayload:
    """Compute the current readiness payload from live probes (no persistence)."""
    model_posture, model_check = _check_model_availability()
    swap_posture, swap_check = _check_swap_posture()
    admission_posture, admission_check = _check_admission_posture()
    pipeline_posture, pipeline_check = _check_pipeline_posture()
    residency_posture = _build_residency_posture()
    gateway_posture = _build_gateway_posture(repo_root)

    checks = (model_check, swap_check, admission_check, pipeline_check)
    status = aggregate_startup_status(checks)
    fix_commands = _startup_fix_commands(checks) if status != "ready" else ()

    return StartupReadinessPayload(
        schema_version=1,
        service_label="com.byron.ollarma",
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        status=status,
        checks=checks,
        model_availability=model_posture,
        swap=swap_posture,
        admission=admission_posture,
        pipeline=pipeline_posture,
        residency=residency_posture,
        gateway=gateway_posture,
        next_fix_commands=fix_commands,
    )


def write_startup_readiness(
    payload: StartupReadinessPayload,
    path: pathlib.Path | None = None,
) -> pathlib.Path:
    """Persist payload to readiness JSON with atomic rename + sorted-keys bytes."""
    import os as _os  # noqa: PLC0415
    import tempfile as _tempfile  # noqa: PLC0415
    target = path if path is not None else startup_readiness_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = orjson.dumps(
        payload.model_dump(mode="json"),
        option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2,
    )
    fd, tmp_path = _tempfile.mkstemp(prefix=".readiness.", dir=str(target.parent))
    try:
        with _os.fdopen(fd, "wb") as f:
            f.write(data)
        _os.replace(tmp_path, target)
    except Exception:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def refresh_startup_readiness(
    *, path: pathlib.Path | None = None,
) -> StartupReadinessPayload:
    """Build + persist the readiness payload. Called once on service startup."""
    payload = build_startup_readiness()
    try:
        write_startup_readiness(payload, path=path)
    except OSError:
        # Persistence failure must not block readiness reporting.
        pass
    return payload


def get_startup_readiness(
    *,
    path: pathlib.Path | None = None,
    max_age_seconds: float = 10.0,
) -> StartupReadinessPayload:
    """Return a readiness payload — persisted if fresh, rebuilt otherwise.

    Fresh build (no persistence) if:
      - no file exists yet
      - file is unreadable or corrupt
      - persisted ``generated_at`` is older than ``max_age_seconds``

    Pass ``max_age_seconds=float("inf")`` to always use the cached value when
    one exists. Default 10s balances telemetry freshness with the cost of
    rebuilding (subprocess sysctl + HTTP probe to /api/ps, typically <100ms).

    GPU-06: introduced TTL so /health and /startup/readiness no longer serve
    the startup snapshot indefinitely after service launch.
    """
    target = path if path is not None else startup_readiness_path()
    if target.exists():
        try:
            raw = target.read_bytes()
            data = orjson.loads(raw)
            payload = StartupReadinessPayload.model_validate(data)
        except Exception:  # noqa: BLE001
            payload = None
        if payload is not None:
            if max_age_seconds == float("inf"):
                return payload
            try:
                generated = datetime.datetime.fromisoformat(payload.generated_at)
                now = datetime.datetime.now(datetime.timezone.utc)
                age = (now - generated).total_seconds()
                if age <= max_age_seconds:
                    return payload
            except (ValueError, TypeError):
                # Malformed timestamp — treat as stale and rebuild.
                pass
    return build_startup_readiness()


class KBContractResult(BaseModel):
    """Resolved repo-local KB contract for one project."""
    contract: ProjectKnowledgeContract

    model_config = ConfigDict(frozen=True)


class KBBuildResult(BaseModel):
    """Materialized repo-local KB artifact result."""
    project: str
    artifact_root: dict
    manifest_ref: dict
    documents_ref: dict
    chunks_ref: dict
    tags_ref: dict
    receipt_ref: dict
    source_count: int
    document_count: int
    chunk_count: int
    reason_code: str | None = None

    model_config = ConfigDict(frozen=True)


class RouteReceipt(BaseModel):
    """Machine-readable receipt for retrieval-first helper routing."""

    lane: str
    reason_code: str
    query_class: str
    kb_status: str | None = None
    evidence_count: int = 0
    next_action: str = "none"
    selected_model: str | None = None
    namespace: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    model_config = ConfigDict(frozen=True)


class RouteResult(BaseModel):
    """Response for project-routed agent requests."""
    final_response: str
    tool_calls_count: int
    model: str | None = None
    project: str
    lane: str = "grounded_local"
    reason_code: str | None = None
    query_class: str = "open_ended"
    kb_status: str | None = None
    evidence_refs: tuple[dict[str, str | int | float], ...] = ()
    next_action: str = "none"
    route_receipt: RouteReceipt | None = None
    escalation_receipt: dict | None = None
    # Phase 53 ROUTE-01..03, OBS-03: routing ladder decision receipt.
    # Optional (default None) for backwards compat with existing tests/callers.
    ladder: "LadderDecision | None" = None

    model_config = ConfigDict(frozen=True)


class WorkflowSubmissionResult(BaseModel):
    """Response for explicit workflow-lane submissions."""

    project: str
    manifest_ref: dict[str, str]
    manifest_digest: str
    run_id: str
    step_id: str
    task_class: str
    consumer_repo: str | None = None
    materialization_root: dict[str, str] | None = None
    lane: str
    status: str
    model: str | None = None
    queue_depth: int = 0
    reason_code: str | None = None
    scheduler: RuntimeSnapshot
    escalation_receipt: dict | None = None
    receipt_ref: dict[str, str] | None = None
    checkpoint_ref: dict[str, str] | None = None
    owning_lane: str | None = None
    next_stage: str | None = None
    handoff_required: bool = False

    model_config = ConfigDict(frozen=True)


class DashboardAutopilotRequest(BaseModel):
    """Bounded autopilot request shape for HTTP/dashboard submission."""

    project: str
    run_assets: bool = False
    threshold: float = 0.9
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    model: str | None = None

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Private helpers (moved from cli.py)
# ---------------------------------------------------------------------------


def _dispatch_scorer(result: BenchmarkResult) -> Optional[float]:
    """Route to the correct scorer by suite. Unknown suite returns None (not 0.0).

    None preserves the unknown-suite signal so Phase 5 reporter does not average
    it into quality metrics as a real zero score.
    """
    if result.suite == "science":
        return score_science(result)
    if result.suite == "code":
        return score_code(result)
    if result.suite == "swarm":
        return score_swarm(result)
    if result.suite == "bigcodebench":
        return score_bigcodebench(result)
    if result.suite == "mteb":
        return score_mteb(result)
    return None


def _is_reserved_model(model: str | None) -> bool:
    """Return whether model is reserved for Antigence/Sentinel direct use."""
    return _reserved_is_reserved_model(model)


def _assert_model_not_reserved(model: str | None, *, context: str) -> None:
    """Refuse Antigence/Sentinel-reserved models on Ollarma service lanes."""
    _reserved_assert_model_not_reserved(model, context=context)


def _drop_reserved_models(models: tuple[str, ...]) -> tuple[str, ...]:
    """Remove Antigence/Sentinel-reserved tags from Ollarma candidate lists."""
    return _reserved_drop_reserved_models(models)


def _apply_model_filter(
    all_models: list[ModelConfig],
    filter_names: Optional[list[str]],
) -> list[ModelConfig]:
    """Return models matching filter_names, or all models if filter_names is None."""
    if not filter_names:
        return [m for m in all_models if not _is_reserved_model(m.name)]
    for name in filter_names:
        _assert_model_not_reserved(name, context="benchmark")
    return [m for m in all_models if m.name in filter_names and not _is_reserved_model(m.name)]


def _apply_suite_filter(
    all_tasks: list[TaskConfig],
    filter_suites: Optional[list[str]],
) -> list[TaskConfig]:
    """Return tasks matching filter_suites, or all tasks if filter_suites is None."""
    if not filter_suites:
        return all_tasks
    return [t for t in all_tasks if t.suite in filter_suites]


def _load_project_registry(
    adapters_dir: str | None = None,
    *,
    service_mode: bool = False,
) -> dict[str, AdapterConfig]:
    """Load fleet registry from resolved adapter directories plus entry points."""
    candidate_dirs: list[str] = []
    seen: set[str] = set()

    for raw_dir in [resolve_adapters_dir(adapters_dir), *discover_entry_point_adapters()]:
        if raw_dir in seen:
            continue
        seen.add(raw_dir)
        candidate_dirs.append(raw_dir)

    allowlist = candidate_dirs if service_mode else None
    merged: dict[str, AdapterConfig] = {}
    for raw_dir in candidate_dirs:
        resolved_dir = str(
            validate_adapter_path(
                raw_dir,
                allowlist=allowlist,
                service_mode=service_mode,
            )
        )
        merged.update(load_fleet_registry(resolved_dir))

    return merged


def _validate_service_project_root(project_root: str) -> pathlib.Path:
    """Enforce a narrow project-root policy for service-mode routing.

    Service surfaces are intended for local cross-project access, not arbitrary
    filesystem traversal. Project roots must resolve to an existing directory
    beneath the current user's home directory and cannot target the home root
    itself.
    """
    raw = pathlib.Path(project_root).expanduser()
    # Confine the *registered* path to home using a lexical (non-symlink-resolved)
    # absolute path. An operator may symlink an active project under ~ to a
    # mounted mirror (e.g. ~/projects/active/tf-cellico -> /Volumes/.../tf-cellico);
    # that is legitimate local cross-project access, so we gate on where the
    # entry lives, not on its symlink target — while still verifying the target
    # exists and is a real directory.
    lexical = pathlib.Path(os.path.abspath(raw))
    resolved = raw.resolve()
    home_lexical = pathlib.Path(os.path.abspath(pathlib.Path.home()))

    if not resolved.is_dir():
        raise FileNotFoundError(f"Project root does not exist: {project_root}")
    if lexical == pathlib.Path(os.path.abspath(os.sep)):
        raise ValueError("Project root '/' is not allowed in service mode")
    if lexical == home_lexical:
        raise ValueError(
            "Project root cannot be the current user's home directory in service mode"
        )
    if not lexical.is_relative_to(home_lexical):
        raise ValueError(
            "Project root must be within the current user's home directory in "
            f"service mode: {lexical}"
        )

    return resolved


_scheduler = Scheduler()


def get_scheduler() -> Scheduler:
    """Return the shared scheduler used by service and transport layers."""
    return _scheduler


def get_scheduler_snapshot() -> RuntimeSnapshot:
    """Expose shared scheduler state for admission and reporting."""
    return _scheduler.snapshot()


def _service_repo_root() -> pathlib.Path:
    """Return the repository root when available for docs-backed metadata."""
    return pathlib.Path(__file__).resolve().parents[2]


def _build_dashboard_boundary_info() -> DashboardBoundaryInfo:
    """Build the bounded dashboard contract for the localhost operator surface."""
    helper_surfaces: tuple[str, ...] = ()
    execution_surfaces: tuple[str, ...] = ()
    safe_for: tuple[str, ...] = ()
    unsafe_for: tuple[str, ...] = ()
    fallback_policy: dict[str, str] = {}
    read_only = True
    interactive = True
    review_gate = "review when the ollarma dashboard is available"
    integration_contract = "links_exports_only"
    portfolio_dashboard_dependency = False

    policy_path = _service_repo_root() / "docs" / "DETERMINISTIC_EXECUTION_POLICY.json"
    if policy_path.exists():
        policy = orjson.loads(policy_path.read_bytes())
        dashboard_policy = policy.get("dashboard_policy", {})
        helper_surfaces = tuple(
            item["surface"]
            for item in policy.get("helper_surfaces", [])
            if isinstance(item, dict) and isinstance(item.get("surface"), str)
        )
        execution_surfaces = tuple(
            item["surface"]
            for item in policy.get("deterministic_execution_surfaces", [])
            if isinstance(item, dict) and isinstance(item.get("surface"), str)
        )
        safe_for = tuple(item for item in policy.get("safe_for", []) if isinstance(item, str))
        unsafe_for = tuple(item for item in policy.get("unsafe_for", []) if isinstance(item, str))
        fallback_policy = {
            key: value
            for key, value in policy.get("fallback_policy", {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if isinstance(dashboard_policy, dict):
            read_only = bool(dashboard_policy.get("read_only", read_only))
            interactive = bool(dashboard_policy.get("interactive", interactive))
            if isinstance(dashboard_policy.get("review_gate"), str):
                review_gate = str(dashboard_policy["review_gate"]).replace("_", " ")
            if isinstance(dashboard_policy.get("integration_contract"), str):
                integration_contract = dashboard_policy["integration_contract"]
            portfolio_dashboard_dependency = bool(
                dashboard_policy.get(
                    "portfolio_dashboard_dependency",
                    portfolio_dashboard_dependency,
                )
            )

    return DashboardBoundaryInfo(
        dashboard_owner="ollarma",
        read_only=read_only,
        interactive=interactive,
        dashboard_path="/dashboard",
        overview_path="/dashboard/overview",
        run_detail_path_template="/dashboard/runs/{run_id}",
        review_gate=review_gate,
        integration_contract=integration_contract,
        portfolio_dashboard_dependency=portfolio_dashboard_dependency,
        helper_surfaces=helper_surfaces,
        deterministic_execution_surfaces=execution_surfaces,
        safe_for=safe_for,
        unsafe_for=unsafe_for,
        fallback_policy=fallback_policy,
    )


_CHAT_FALLBACK_MODELS: tuple[str, ...] = (
    "granite4.1:8b",    # measured quality winner (science 0.95 / swarm 0.98) — preferred
                        # generic-chat fallback when memory allows; previously absent, so
                        # degraded chat could never reach it (only the quality-0 tiny model).
    "qwen2.5:1.5b",     # always-resident lean rescue; the pressure-aware path prefers this
                        # under swap so the big winner is never loaded under memory pressure.
    "phi4-mini:latest",
    "qwen3.5:2b",
    "qwen3.5:4b",
    "qwen3:4b",
    "smollm2:latest",
    "qwen3:8b",
    "granite3.3:8b",
)
_SELECTION_RECOVERY_COMMANDS: tuple[str, ...] = (
    "ollarma run --suites code --trials 3",
    "ollarma report --run-id <fresh_run_id>",
    "ollarma verify <fresh_run_id>",
)


def _list_installed_model_names() -> tuple[str, ...]:
    """Return installed local model names, or an empty tuple when unavailable."""
    try:
        available = ollama.Client().list().models
    except Exception:
        return ()
    names = [
        name
        for name in (getattr(item, "model", None) for item in available)
        if isinstance(name, str)
    ]
    return tuple(names)


def _is_chat_capable_model_name(name: str) -> bool:
    if _is_reserved_model(name):
        return False
    lowered = name.lower()
    blocked_prefixes = ("bge", "llava")
    blocked_fragments = ("embed", "vision")
    if lowered.startswith(blocked_prefixes):
        return False
    return not any(fragment in lowered for fragment in blocked_fragments)


def _pick_chat_fallback_model(model_names: tuple[str, ...]) -> str | None:
    """Choose a bounded generic-chat fallback model from installed local models."""
    for candidate in _CHAT_FALLBACK_MODELS:
        if candidate in model_names:
            return candidate
    for name in model_names:
        if not _is_chat_capable_model_name(name):
            continue
        return name
    return None


def _pick_pressure_aware_fallback_model(
    model_names: tuple[str, ...],
    *,
    resident_models: tuple[str, ...] = (),
    swap_used_mb: float | None = None,
    swap_threshold_mb: float | None = None,
) -> str | None:
    """Choose a local fallback model sized for the current pressure posture.

    When swap is degraded, prefer an already-resident chat-capable model, then
    the ordered rescue/small-model list. This keeps degraded Ollarma useful for
    low-risk sidecar chat without loading a larger benchmark winner.
    """
    installed = tuple(name for name in model_names if _is_chat_capable_model_name(name))
    if not installed:
        return None

    swap_over_threshold = (
        swap_used_mb is not None
        and swap_threshold_mb is not None
        and swap_used_mb > swap_threshold_mb
    )
    if swap_over_threshold:
        for candidate in _CHAT_FALLBACK_MODELS:
            if candidate in installed and candidate in resident_models:
                return candidate
        for name in resident_models:
            if name in installed:
                return name
        for candidate in _CHAT_FALLBACK_MODELS:
            if candidate in installed:
                return candidate
        return installed[0]

    return _pick_chat_fallback_model(model_names)


def _fallback_pressure_context() -> tuple[tuple[str, ...], float | None, float | None]:
    """Return resident models, swap used, and threshold for fallback selection."""
    from ollarma.scheduler import SWAP_DEGRADED_THRESHOLD_MB  # noqa: PLC0415
    try:
        from ollarma.guards import collect_runtime_telemetry  # noqa: PLC0415
        telemetry = collect_runtime_telemetry()
        return (
            tuple(telemetry.loaded_models),
            telemetry.swap_used_mb,
            float(SWAP_DEGRADED_THRESHOLD_MB),
        )
    except Exception:  # noqa: BLE001
        return (), None, float(SWAP_DEGRADED_THRESHOLD_MB)


def _probe_installed_model_names() -> tuple[tuple[str, ...], str | None]:
    """Return installed model names plus an optional probe error detail."""
    try:
        available = ollama.Client().list().models
    except Exception as exc:
        return (), str(exc)
    names = [
        name
        for name in (getattr(item, "model", None) for item in available)
        if isinstance(name, str)
    ]
    return tuple(names), None


def _selection_health(workload_class: WorkloadClass) -> SelectionHealth:
    """Return selection state plus pressure-aware local fallback when degraded."""
    try:
        model_name = resolve_selection(workload_class=workload_class)
        _assert_model_not_reserved(model_name, context=f"{workload_class.value} selection")
        return SelectionHealth(
            workload_class=workload_class.value,
            status="ready",
            model=model_name,
        )
    except SelectionResolutionError as exc:
        fallback_names, probe_error = _probe_installed_model_names()
        resident_models, swap_mb, threshold_mb = _fallback_pressure_context()
        fallback_model = _pick_pressure_aware_fallback_model(
            fallback_names,
            resident_models=resident_models,
            swap_used_mb=swap_mb,
            swap_threshold_mb=threshold_mb,
        )
        if fallback_model:
            detail = (
                f"{exc.detail}. Using pressure-aware local fallback "
                f"{fallback_model!r}; strict validated selection still needs refresh."
            )
            return SelectionHealth(
                workload_class=workload_class.value,
                status="fallback_ready",
                model=fallback_model,
                reason_code=exc.reason_code,
                detail=detail,
                recovery_commands=_SELECTION_RECOVERY_COMMANDS,
            )
        detail = exc.detail
        if probe_error:
            detail = f"{detail}. Local Ollama daemon probe failed: {probe_error}"
        return SelectionHealth(
            workload_class=workload_class.value,
            status="blocked",
            reason_code=exc.reason_code,
            detail=detail,
            recovery_commands=_SELECTION_RECOVERY_COMMANDS,
        )


def resolve_generic_chat_model(model: str | None = None) -> HelperModelResolution:
    """Resolve the model for bounded generic chat without weakening strict lanes."""
    if model:
        _assert_model_not_reserved(model, context="generic chat")
        return HelperModelResolution(
            requested_model=model,
            effective_model=model,
            status="explicit",
        )

    try:
        effective_model = resolve_default_model(workload_class=WorkloadClass.CHAT)
        _assert_model_not_reserved(effective_model, context="generic chat selection")
        return HelperModelResolution(
            effective_model=effective_model,
            status="selection_ready",
        )
    except SelectionResolutionError as exc:
        fallback_names, probe_error = _probe_installed_model_names()
        resident_models, swap_mb, threshold_mb = _fallback_pressure_context()
        fallback_model = _pick_pressure_aware_fallback_model(
            fallback_names,
            resident_models=resident_models,
            swap_used_mb=swap_mb,
            swap_threshold_mb=threshold_mb,
        )
        if fallback_model:
            return HelperModelResolution(
                effective_model=fallback_model,
                status="fallback_ready",
                reason_code=exc.reason_code,
                detail=(
                    f"{exc.detail}. Using pressure-aware local fallback "
                    f"{fallback_model!r}; strict validated selection still needs refresh."
                ),
                recovery_commands=_SELECTION_RECOVERY_COMMANDS,
            )
        detail = exc.detail
        if probe_error:
            detail = f"{detail}. Local Ollama daemon probe failed: {probe_error}"
        elif not fallback_names:
            detail = (
                f"{detail}. No installed local fallback model is available for generic chat."
            )
        return HelperModelResolution(
            effective_model=None,
            status="blocked",
            reason_code=exc.reason_code,
            detail=detail,
            recovery_commands=(
                "ollama list",
                "ollama pull qwen2.5:1.5b",
                *_SELECTION_RECOVERY_COMMANDS,
            ),
        )


def get_runtime_health(
    *,
    adapters_dir: str | None = None,
) -> RuntimeHealthResult:
    """Return deterministic runtime readiness for helper/chat routing surfaces.

    v4.5 Phase 51 PERSIST-02/03: the top-level ``status`` now folds startup
    readiness — ``blocked`` > ``degraded`` > ``ready``. A degraded startup
    cannot hide behind a generic ``ok``.
    """
    helper_chat = resolve_generic_chat_model()
    try:
        project_count = len(list_projects(adapters_dir=adapters_dir, service_mode=True))
    except Exception:
        project_count = 0

    # Prefer the most recent *persisted* readiness (from the startup refresh).
    # If the file is missing (e.g. we're in a test or fresh install), fall
    # back to a live build so the field is always populated.
    startup_readiness = get_startup_readiness()

    # Fold startup readiness into the top-level status. Mirror the GPU-08
    # policy in _check_model_availability: "ready"/"selection_ready"/"explicit"
    # are all healthy, "fallback_ready" is degraded (strict selection failed and
    # we are on a probed fallback), anything else is a block. Previously this
    # check wrongly treated "selection_ready"/"explicit" as degraded — which
    # folded a fully-healthy helper into a false top-level "degraded".
    helper_degraded = helper_chat.status not in ("ready", "selection_ready", "explicit")
    combined = startup_readiness.status
    if helper_degraded and combined == "ready":
        combined = "degraded"

    # Phase 53 OBS-02/03: surface last routing ladder decision in /health.
    _last_ld = get_last_ladder_decision()
    last_ladder_compact: dict | None = None
    if _last_ld is not None:
        last_ladder_compact = {
            "status": _last_ld.status,
            "chosen_model": _last_ld.chosen_model,
            "reason_code": _last_ld.reason_code,
            "detail": _last_ld.detail,
            "escalation_hint": _last_ld.escalation_hint,
        }

    return RuntimeHealthResult(
        status=combined,
        helper_chat=helper_chat,
        chat_selection=_selection_health(WorkloadClass.CHAT),
        route_selection=_selection_health(WorkloadClass.ROUTE_PROMPT),
        project_count=project_count,
        startup_readiness=startup_readiness,
        last_routing_ladder=last_ladder_compact,
    )


def _dashboard_citation(repo_relative: str, *, label: str | None = None) -> DashboardCitation:
    return DashboardCitation(
        label=label or repo_relative,
        repo_relative=repo_relative,
    )


def _build_dashboard_model_options() -> tuple[DashboardModelOption, ...]:
    """Return deterministic model-picker options for the dashboard."""
    installed_names, _probe_error = _probe_installed_model_names()

    try:
        declared_models = load_models()
    except Exception:
        declared_models = []

    declared_by_name = {model.name: model for model in declared_models}
    ordered_names: list[str] = []
    installed_canonical: dict[str, str] = {}
    for name in installed_names:
        if not _is_chat_capable_model_name(name):
            continue
        canonical_name = name
        if name.endswith(":latest"):
            # ``model`` and ``model:latest`` are the same model; collapse the
            # implicit default tag so the picker shows the bare name regardless
            # of whether it is also a declared catalog entry.
            canonical_name = name[: -len(":latest")]
        installed_canonical.setdefault(canonical_name, name)
        if canonical_name not in ordered_names:
            ordered_names.append(canonical_name)
    for model in declared_models:
        if not _is_chat_capable_model_name(model.name):
            # Keep embedding/vision/reserved catalog entries (e.g. nomic-embed-text)
            # out of the chat model picker, mirroring the installed-name filter.
            continue
        if model.name not in ordered_names:
            ordered_names.append(model.name)

    options: list[DashboardModelOption] = []
    for name in ordered_names:
        declared = declared_by_name.get(name)
        installed = name in installed_canonical
        source = "installed+catalog" if installed and declared else "installed" if installed else "catalog"
        label_parts = [name]
        if installed:
            label_parts.append("installed")
        elif declared:
            label_parts.append("catalog")
        options.append(
            DashboardModelOption(
                name=name,
                label=" - ".join(label_parts),
                description=declared.description if declared else "Installed local model discovered from Ollama.",
                installed=installed,
                source=source,
            )
        )
    return tuple(options)


def _build_dashboard_operator_resources() -> tuple[DashboardResource, ...]:
    return (
        DashboardResource(
            slug="dashboard-start",
            title="Start With The Dashboard",
            category="operator",
            summary="Use the localhost dashboard as the first place to inspect state, ask bounded questions, and see what is still missing.",
            why_it_matters="This is the intended operator entry point for review, generic chat, and project-routed help without jumping across separate tools first.",
            suggested_questions=(
                "How do I use ollarma day to day?",
                "What am I missing before project routing will work well?",
                "Which surface should I use for project help versus generic chat?",
            ),
            citations=(
                _dashboard_citation("docs/OLLARMA_DASHBOARD.md"),
                _dashboard_citation("docs/DETERMINISTIC_EXECUTION_BOUNDARY.md"),
            ),
            commands=(
                DashboardCommandHint(
                    label="serve",
                    command="ollarma serve",
                    purpose="Start the local HTTP API and dashboard.",
                ),
                DashboardCommandHint(
                    label="open dashboard",
                    command="ollarma dashboard",
                    purpose="Print the dashboard URL quickly.",
                ),
            ),
        ),
        DashboardResource(
            slug="project-routing",
            title="Project-Routed Help",
            category="grounded-help",
            summary="Use project routing when you want citations and answers grounded in a registered project KB instead of generic uncited chat.",
            why_it_matters="Grounded project help is the lane that can point to files, manifests, and KB evidence; plain chat cannot do that.",
            suggested_questions=(
                "Where is the workflow manifest for this project?",
                "What docs define the adapter KB contract?",
                "Why did project routing escalate instead of answering directly?",
            ),
            citations=(
                _dashboard_citation("docs/OLLARMA_PROJECT_HELPER_PROMPT.md"),
                _dashboard_citation("docs/LOCAL_ADOPTION.md"),
                _dashboard_citation("docs/OLLARMA_KB_CONTRACT.md"),
            ),
            commands=(
                DashboardCommandHint(
                    label="list projects",
                    command="ollarma projects",
                    purpose="Confirm the registered sibling repos the router can see.",
                ),
                DashboardCommandHint(
                    label="build KB",
                    command="ollarma kb-build --project <name>",
                    purpose="Materialize repo-local KB artifacts before grounded help.",
                ),
            ),
        ),
        DashboardResource(
            slug="kb-inspection",
            title="KB Status And Search",
            category="knowledge-base",
            summary="Use KB status/search to inspect freshness, build state, and deterministic hits before relying on routed summaries.",
            why_it_matters="If a KB is stale, blocked, or missing, the dashboard should tell you that instead of pretending retrieval is ready.",
            suggested_questions=(
                "Is the project KB ready?",
                "Which documents mention workflow manifests?",
                "Why is the KB blocked or stale?",
            ),
            citations=(
                _dashboard_citation("docs/OLLARMA_KB_CONTRACT.md"),
                _dashboard_citation("docs/DETERMINISTIC_EXECUTION_BOUNDARY.md"),
            ),
            commands=(
                DashboardCommandHint(
                    label="KB status",
                    command="ollarma kb-status --project <name>",
                    purpose="Inspect the current repo-local KB state.",
                ),
                DashboardCommandHint(
                    label="KB search",
                    command="ollarma kb-search --project <name> --query \"workflow\" --limit 5",
                    purpose="See deterministic evidence hits directly.",
                ),
            ),
        ),
        DashboardResource(
            slug="execution-boundary",
            title="Execution Boundary And Escalation",
            category="safety",
            summary="The dashboard can help you inspect and ask bounded questions, but it is not a broad execution, git-mutation, or writeback surface.",
            why_it_matters="Operators need to know where `ollarma` stops so unsafe or high-stakes tasks get escalated instead of improvised locally.",
            suggested_questions=(
                "What can the dashboard do safely?",
                "When should I escalate to frontier or human review?",
                "Why is the dashboard interactive but still bounded?",
            ),
            citations=(
                _dashboard_citation("docs/DETERMINISTIC_EXECUTION_BOUNDARY.md"),
                _dashboard_citation("docs/DETERMINISTIC_EXECUTION_POLICY.json"),
            ),
            commands=(
                DashboardCommandHint(
                    label="project chat REPL",
                    command="ollarma chat --project <name>",
                    purpose="Use the CLI REPL when you want an interactive project session outside the browser.",
                ),
            ),
        ),
    )


def _build_dashboard_readiness(
    *,
    projects: list[str],
    kb_status_items: list[DashboardKBStatus],
    route_receipts: list[DashboardRouteReceipt],
    runtime_health: RuntimeHealthResult,
) -> tuple[DashboardReadinessItem, ...]:
    items: list[DashboardReadinessItem] = []

    # v4.5 Phase 51 PERSIST-03: loud-on-degraded startup posture.
    # A non-ready startup surfaces at the top of dashboard readiness so the
    # operator sees it first, with the same fix commands `/startup/readiness`
    # would return.
    sr = runtime_health.startup_readiness
    if sr is not None and sr.status != "ready":
        action = "\n".join(sr.next_fix_commands) if sr.next_fix_commands else None
        title = (
            "Startup readiness is blocked"
            if sr.status == "blocked"
            else "Startup readiness is degraded"
        )
        detail_parts = [
            f"Overall startup status: {sr.status}.",
            "; ".join(
                f"{c.name}={c.status}" + (f" ({c.reason_code})" if c.reason_code else "")
                for c in sr.checks
                if c.status != "ready"
            ) or "See /startup/readiness for full posture.",
        ]
        items.append(
            DashboardReadinessItem(
                severity="warning" if sr.status == "degraded" else "blocked",
                title=title,
                detail=" ".join(detail_parts),
                action_label="Inspect startup readiness",
                action_command=action,
            )
        )

    # Phase 52 OBS-01: surface residency state when not ready.
    if sr is not None and sr.residency is not None:
        res = sr.residency
        if res.state != "ready":
            if not res.rescue_resident:
                res_title = "Rescue model not resident"
                res_detail = (
                    f"Rescue model '{res.rescue_target}' is not loaded. "
                    f"reason={res.reason_code or 'unknown'} swap={res.swap_used_mb} MB."
                )
            elif res.state == "degraded" and not res.opportunistic_resident:
                res_title = "Opportunistic model evicted under swap pressure"
                res_detail = (
                    f"'{res.opportunistic_target}' was evicted due to swap pressure "
                    f"({res.swap_used_mb} MB). Rescue model '{res.rescue_target}' is still resident."
                )
            else:
                res_title = f"GPU residency policy state: {res.state}"
                res_detail = f"reason={res.reason_code or 'unknown'} next_action={res.next_action}"
            items.append(
                DashboardReadinessItem(
                    severity="warning" if res.state == "degraded" else "info",
                    title=res_title,
                    detail=res_detail,
                    action_label="Inspect residency posture",
                    action_command="curl -sS http://127.0.0.1:8484/startup/readiness | jq .residency",
                )
            )

    # Phase 53 OBS-02: surface routing ladder degradation when last decision
    # was not chose_preferred. Simple in-process cache check — no extra state.
    _last_ld = get_last_ladder_decision()
    if _last_ld is not None and _last_ld.status != "chose_preferred":
        if _last_ld.status == "blocked_escalate":
            ld_title = "Routing ladder blocked — no local model available"
            ld_severity = "blocked"
        elif _last_ld.status == "chose_rescue":
            ld_title = "Routing ladder degraded (rescue-only)"
            ld_severity = "warning"
        else:
            ld_title = "Routing ladder degraded (swap pressure)"
            ld_severity = "warning"
        items.append(
            DashboardReadinessItem(
                severity=ld_severity,
                title=ld_title,
                detail=(
                    f"reason_code={_last_ld.reason_code} "
                    f"chosen={_last_ld.chosen_model or 'none'} "
                    f"detail={_last_ld.detail}"
                ),
                action_label="Inspect routing ladder",
                action_command="curl -sS http://127.0.0.1:8484/health | jq .last_routing_ladder",
            )
        )

    if not projects:
        items.append(
            DashboardReadinessItem(
                severity="warning",
                title="No registered projects detected",
                detail="Generic chat will work, but project-routed grounded help is not ready until at least one adapter is discoverable.",
                action_label="Register adapters",
                action_command='ollarma projects --adapters-dir "<path-to-adapters>"',
            )
        )
    if runtime_health.helper_chat.status == "blocked":
        items.append(
            DashboardReadinessItem(
                severity="warning",
                title="Generic dashboard chat is blocked",
                detail=(
                    runtime_health.helper_chat.detail
                    or "The dashboard can render, but `/chat` cannot resolve a usable local model yet."
                ),
                action_label="Check Ollama runtime and rebuild selection",
                action_command="\n".join(runtime_health.helper_chat.recovery_commands) or None,
            )
        )
    elif runtime_health.helper_chat.status == "fallback_ready":
        items.append(
            DashboardReadinessItem(
                severity="info",
                title="Generic dashboard chat is using a fallback model",
                detail=(
                    f"Current fallback: {runtime_health.helper_chat.effective_model}. "
                    "Chat can work now, but the validated selection artifact is still stale or incomplete."
                ),
                action_label="Refresh validated selection",
                action_command="\n".join(runtime_health.helper_chat.recovery_commands) or None,
            )
        )
    if runtime_health.route_selection.status == "blocked":
        items.append(
            DashboardReadinessItem(
                severity="warning",
                title="Grounded synthesis selection is blocked",
                detail=(
                    runtime_health.route_selection.detail
                    or "Project-routed synthesis cannot choose a validated model yet."
                ),
                action_label="Regenerate validated route selection",
                action_command="\n".join(runtime_health.route_selection.recovery_commands) or None,
            )
        )
    blocked = [item for item in kb_status_items if item.status != "ready"]
    for item in blocked[:3]:
        items.append(
            DashboardReadinessItem(
                severity="warning",
                title=f"KB not ready for {item.project}",
                detail=(
                    f"Status is {item.status}"
                    + (f" ({item.reason_code})" if item.reason_code else "")
                    + ". Grounded project help may escalate or answer with limited evidence."
                ),
                action_label="Build or inspect KB",
                action_command=f'ollarma kb-build --project "{item.project}" --adapters-dir "$OLLARMA_ADAPTERS_DIR"',
            )
        )
    if projects and not route_receipts:
        items.append(
            DashboardReadinessItem(
                severity="info",
                title="No recent route receipts yet",
                detail="Project routing is available, but this dashboard has not recorded a recent routed help interaction yet.",
                action_label="Try project-routed help",
                action_command=None,
            )
        )
    if not items:
        items.append(
            DashboardReadinessItem(
                severity="ok",
                title="Dashboard interaction looks ready",
                detail="Projects are registered and at least one grounded helper lane has recent evidence/receipt activity.",
                action_label="Ask a routed question",
                action_command=None,
            )
        )
    return tuple(items)


def _dashboard_run_summary(project: str, detail: dict) -> DashboardRunSummary:
    """Convert one ledger detail dict into a typed dashboard run summary."""
    return DashboardRunSummary(
        project=project,
        run_id=detail["run_id"],
        run_kind=detail["run_kind"],
        current_stage=detail.get("current_stage"),
        last_validated_stage=detail.get("last_validated_stage"),
        status=detail.get("status"),
        reason_code=detail.get("reason_code"),
        step_id=detail.get("step_id"),
        updated_at=detail.get("updated_at"),
        receipt_count=detail.get("receipt_count", 0),
        receipt_ref=detail.get("receipt_ref"),
        checkpoint_ref=detail.get("checkpoint_ref"),
    )


def _dashboard_run_detail(project: str, detail: dict) -> DashboardRunDetail:
    """Convert one ledger detail dict into a typed dashboard run detail."""
    checkpoint = detail.get("checkpoint")
    return DashboardRunDetail(
        project=project,
        run_id=detail["run_id"],
        run_kind=detail["run_kind"],
        current_stage=detail.get("current_stage"),
        last_validated_stage=detail.get("last_validated_stage"),
        status=detail.get("status"),
        reason_code=detail.get("reason_code"),
        step_id=detail.get("step_id"),
        updated_at=detail.get("updated_at"),
        receipt_count=detail.get("receipt_count", 0),
        receipt_ref=detail.get("receipt_ref"),
        checkpoint_ref=detail.get("checkpoint_ref"),
        checkpoint=(
            DashboardCheckpointSummary.model_validate(checkpoint)
            if isinstance(checkpoint, dict)
            else None
        ),
        receipts=tuple(
            DashboardReceiptPreview.model_validate(receipt)
            for receipt in detail.get("receipts", [])
            if isinstance(receipt, dict)
        ),
    )


def _iter_dashboard_projects(
    adapters_dir: str | None = None,
) -> list[tuple[str, pathlib.Path]]:
    """Return project names plus roots that can contribute dashboard data."""
    projects: list[tuple[str, pathlib.Path]] = []
    for name, adapter in list_projects(adapters_dir=adapters_dir, service_mode=True).items():
        try:
            projects.append((name, pathlib.Path(adapter.project_root).resolve()))
        except OSError:
            continue
    return projects


def _dashboard_kb_status(project_name: str, adapter: AdapterConfig) -> DashboardKBStatus:
    try:
        contract = build_project_knowledge_contract(
            project_name=adapter.project_name,
            project_root=adapter.project_root,
            knowledge_base=adapter.knowledge_base,
            databases=adapter.databases,
        )
    except FileNotFoundError:
        return DashboardKBStatus(
            project=project_name,
            status="missing",
            reason_code="PROJECT_ROOT_MISSING",
            source_count=0,
            document_count=0,
            chunk_count=0,
            freshness_hours=0,
            stale_behavior="block",
            built_at=None,
            artifact_root="",
            search_db_path="",
        )
    status = load_kb_status(contract)
    return DashboardKBStatus(
        project=project_name,
        status=status.status,
        reason_code=status.reason_code,
        source_count=len(contract.sources),
        document_count=status.document_count,
        chunk_count=status.chunk_count,
        freshness_hours=status.freshness_hours,
        stale_behavior=status.stale_behavior,
        built_at=status.built_at,
        artifact_root=status.artifact_root,
        search_db_path=status.search_db_path,
    )


def _load_dashboard_route_receipts(
    *,
    project_name: str,
    repo_root: pathlib.Path,
    limit: int,
) -> list[DashboardRouteReceipt]:
    receipts_path = repo_root / ".ollarma" / "kb" / "route_receipts.jsonl"
    if not receipts_path.exists():
        return []
    receipts: list[DashboardRouteReceipt] = []
    for line in receipts_path.read_bytes().splitlines():
        if not line.strip():
            continue
        raw = orjson.loads(line)
        if not isinstance(raw, dict):
            continue
        raw.setdefault("project", project_name)
        receipts.append(DashboardRouteReceipt.model_validate(raw))
    receipts.sort(key=lambda item: item.created_at, reverse=True)
    return receipts[:limit]


def _load_dashboard_gateway_receipts(
    *,
    repo_root: pathlib.Path,
    limit: int,
) -> list[DashboardGatewayReceipt]:
    """Tail the last ``limit`` FrontierReceipts from ``.ollarma/gateway/receipts.jsonl``.

    Defensive reads per Plan 63-01: missing file, unreadable file, or
    unparseable lines contribute nothing — never raises. The on-disk schema is
    ``FrontierReceipt`` (see ``ollarma.gateway``); this loader extracts only
    the redaction-safe fields surfaced by ``DashboardGatewayReceipt``.
    """
    import pydantic as _pydantic  # noqa: PLC0415

    path = repo_root / ".ollarma" / "gateway" / "receipts.jsonl"
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError:
        return []

    entries: list[DashboardGatewayReceipt] = []
    for line in raw_bytes.splitlines():
        if not line.strip():
            continue
        try:
            obj = orjson.loads(line)
        except orjson.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        try:
            entry = DashboardGatewayReceipt(
                provider=str(obj.get("provider", "")),
                model_id=str(obj.get("model_id", "")),
                status=str(obj.get("status", "")),
                reason_code=obj.get("reason_code") if obj.get("reason_code") is None
                    else str(obj.get("reason_code")),
                cost_usd=str(obj.get("cost_usd", "0")),
                latency_ms=int(obj.get("latency_ms", 0) or 0),
                prompt_tokens=int(obj.get("prompt_tokens", 0) or 0),
                response_tokens=int(obj.get("response_tokens", 0) or 0),
                created_at=str(obj.get("created_at", "")),
            )
        except _pydantic.ValidationError:
            continue
        except (TypeError, ValueError):
            continue
        entries.append(entry)

    return entries[-limit:]


def _load_dashboard_gateway_admissions(
    *,
    repo_root: pathlib.Path,
    limit: int,
) -> list[DashboardGatewayAdmission]:
    """Tail the last ``limit`` admissions from ``.ollarma/gateway/admissions.jsonl``.

    Mirrors ``_load_dashboard_gateway_receipts`` — defensive reads, narrow
    excepts, vk-safe fields only (outcome / reason_code / project).
    """
    import pydantic as _pydantic  # noqa: PLC0415

    path = repo_root / ".ollarma" / "gateway" / "admissions.jsonl"
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError:
        return []

    entries: list[DashboardGatewayAdmission] = []
    for line in raw_bytes.splitlines():
        if not line.strip():
            continue
        try:
            obj = orjson.loads(line)
        except orjson.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        try:
            entry = DashboardGatewayAdmission(
                outcome=str(obj.get("outcome", "")),
                reason_code=obj.get("reason_code") if obj.get("reason_code") is None
                    else str(obj.get("reason_code")),
                project=str(obj.get("project", "")),
                created_at=str(obj.get("created_at", "")),
            )
        except _pydantic.ValidationError:
            continue
        except (TypeError, ValueError):
            continue
        entries.append(entry)

    return entries[-limit:]


def get_dashboard_overview(
    adapters_dir: str | None = None,
    *,
    workflow_limit: int = 8,
    autopilot_limit: int = 8,
    route_receipt_limit: int = 8,
    gateway_entry_limit: int = 5,
    repo_root: pathlib.Path | None = None,
) -> DashboardOverview:
    """Return typed overview data for the localhost operator dashboard."""
    workflow_runs: list[DashboardRunSummary] = []
    autopilot_runs: list[DashboardRunSummary] = []
    kb_status_items: list[DashboardKBStatus] = []
    route_receipts: list[DashboardRouteReceipt] = []
    project_names: list[str] = []
    runtime_health = get_runtime_health(adapters_dir=adapters_dir)

    registry = list_projects(adapters_dir=adapters_dir, service_mode=True)
    projects = []
    for project_name, adapter in registry.items():
        try:
            repo_root = pathlib.Path(adapter.project_root).resolve()
        except OSError:
            continue
        projects.append((project_name, repo_root))
        project_names.append(project_name)
        workflow_runs.extend(
            _dashboard_run_summary(project_name, detail)
            for detail in list_workflow_runs(repo_root=repo_root, limit=workflow_limit)
        )
        autopilot_runs.extend(
            _dashboard_run_summary(project_name, detail)
            for detail in list_autopilot_runs(repo_root=repo_root, limit=autopilot_limit)
        )
        kb_status_items.append(_dashboard_kb_status(project_name, adapter))
        route_receipts.extend(
            _load_dashboard_route_receipts(
                project_name=project_name,
                repo_root=repo_root,
                limit=route_receipt_limit,
            )
        )

    workflow_runs.sort(key=lambda item: item.updated_at or "", reverse=True)
    autopilot_runs.sort(key=lambda item: item.updated_at or "", reverse=True)
    project_names.sort()
    kb_status_items.sort(key=lambda item: item.project)
    route_receipts.sort(key=lambda item: item.created_at, reverse=True)

    # Plan 63-01 (OBS-58): gateway panel payload. Gateway files live at the
    # ollarma repo root (NOT inside a registered project) since the gateway is
    # a cross-project substrate. Defaults to cwd unless the caller overrides.
    gateway_root = pathlib.Path(repo_root) if repo_root is not None else pathlib.Path.cwd()
    gateway_posture_raw = _build_gateway_posture(gateway_root)
    gateway_posture = DashboardGatewayPosture(
        enabled=gateway_posture_raw.enabled,
        allowlist_size=gateway_posture_raw.allowlist_size,
        virtual_keys_configured=gateway_posture_raw.virtual_keys_configured,
        rate_cap_state=gateway_posture_raw.rate_cap_state,
        admissions_today=gateway_posture_raw.admissions_today,
        receipts_today=gateway_posture_raw.receipts_today,
    )
    recent_gateway_receipts = _load_dashboard_gateway_receipts(
        repo_root=gateway_root, limit=gateway_entry_limit,
    )
    recent_gateway_admissions = _load_dashboard_gateway_admissions(
        repo_root=gateway_root, limit=gateway_entry_limit,
    )

    return DashboardOverview(
        generated_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        scheduler=get_scheduler_snapshot(),
        project_count=len(projects),
        projects=tuple(project_names),
        kb_status=tuple(kb_status_items),
        model_options=_build_dashboard_model_options(),
        readiness=_build_dashboard_readiness(
            projects=project_names,
            kb_status_items=kb_status_items,
            route_receipts=route_receipts,
            runtime_health=runtime_health,
        ),
        operator_resources=_build_dashboard_operator_resources(),
        recent_route_receipts=tuple(route_receipts[:route_receipt_limit]),
        workflow_runs=tuple(workflow_runs[:workflow_limit]),
        autopilot_runs=tuple(autopilot_runs[:autopilot_limit]),
        gateway=gateway_posture,
        recent_gateway_receipts=tuple(recent_gateway_receipts),
        recent_gateway_admissions=tuple(recent_gateway_admissions),
        boundary=_build_dashboard_boundary_info(),
    )


def get_dashboard_run_detail(
    run_id: str,
    adapters_dir: str | None = None,
) -> DashboardRunDetail:
    """Return typed drill-down data for one workflow or autopilot run."""
    for project_name, repo_root in _iter_dashboard_projects(adapters_dir=adapters_dir):
        try:
            return _dashboard_run_detail(
                project_name,
                load_workflow_run_detail(repo_root=repo_root, run_id=run_id),
            )
        except FileNotFoundError:
            pass
        try:
            return _dashboard_run_detail(
                project_name,
                load_autopilot_run_detail(repo_root=repo_root, run_id=run_id),
            )
        except FileNotFoundError:
            pass
    raise FileNotFoundError(f"dashboard run not found: {run_id}")


def _submit_inference_job(
    *,
    project: str | None,
    lane: Lane,
    model: str | None,
    callback: Callable[[], BaseModel],
) -> BaseModel:
    """Run inference-bearing work under the shared scheduler."""
    request = JobRequest(
        project=project,
        lane=lane,
        model=model,
    )
    return _scheduler.submit(request, lambda _lease: callback())


_ROUTE_STOPWORDS = {
    "the",
    "and",
    "with",
    "from",
    "that",
    "this",
    "what",
    "where",
    "which",
    "about",
    "into",
    "when",
    "have",
    "does",
    "how",
    "your",
    "their",
    "project",
}


def _route_evidence_refs(search_result: KBSearchResult) -> tuple[dict[str, str | int | float], ...]:
    return tuple(
        {
            "chunk_id": hit.chunk_id,
            "document_id": hit.document_id,
            "path": hit.path,
            "chunk_index": hit.chunk_index,
            "authority": hit.authority,
            "source_kind": hit.source_kind,
            "score": round(hit.score, 6),
        }
        for hit in search_result.hits
    )


def _route_receipt(
    *,
    lane: str,
    reason_code: str,
    query_class: RouteQueryClass,
    kb_status: str | None,
    evidence_count: int,
    next_action: str,
    selected_model: str | None = None,
    namespace: str = "",
) -> RouteReceipt:
    return RouteReceipt(
        lane=lane,
        reason_code=reason_code,
        query_class=query_class.value,
        kb_status=kb_status,
        evidence_count=evidence_count,
        next_action=next_action,
        selected_model=selected_model,
        namespace=namespace,
    )


_ABSOLUTE_PATH_RE = re.compile(r"(?:(?:/Users|/home)/[^\s\"']+|/[A-Za-z0-9_.-]+(?:/[^\s\"']+){1,})")
_SECRETISH_RE = re.compile(r"\b(?:sk|ghp|gho|ghu|pat)_[A-Za-z0-9_-]{8,}\b")


def _redact_prompt_preview(prompt: str) -> str:
    text = " ".join(prompt.strip().split())
    text = _ABSOLUTE_PATH_RE.sub("[path]", text)
    text = _SECRETISH_RE.sub("[secret]", text)
    return text[:160]


def _append_route_receipt_log(
    *,
    repo_root: pathlib.Path,
    prompt: str,
    result: RouteResult,
) -> None:
    if result.route_receipt is None:
        return
    receipts_path = repo_root / ".ollarma" / "kb" / "route_receipts.jsonl"
    receipts_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "project": result.project,
        "lane": result.route_receipt.lane,
        "reason_code": result.route_receipt.reason_code,
        "query_class": result.route_receipt.query_class,
        "kb_status": result.route_receipt.kb_status,
        "evidence_count": result.route_receipt.evidence_count,
        "next_action": result.route_receipt.next_action,
        "selected_model": result.route_receipt.selected_model,
        "prompt_preview": _redact_prompt_preview(prompt),
        "created_at": result.route_receipt.created_at,
    }
    with receipts_path.open("ab") as handle:
        handle.write(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS) + b"\n")


def _route_search_terms(prompt: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9_./-]+", prompt.lower())
    filtered = [
        token for token in tokens
        if len(token) >= 4 and token not in _ROUTE_STOPWORDS
    ]
    ordered = sorted(dict.fromkeys(filtered), key=lambda token: (-len(token), token))
    if prompt.strip():
        ordered.append(prompt.strip())
    return ordered[:6] or [prompt.strip()]


def _search_route_evidence(contract: ProjectKnowledgeContract, prompt: str, *, limit: int = 3) -> KBSearchResult:
    fallback = load_kb_status(contract)
    if fallback.status == "blocked":
        return KBSearchResult(
            project=contract.project,
            query=prompt,
            status=fallback.status,
            reason_code=fallback.reason_code,
        )

    for candidate in _route_search_terms(prompt):
        result = search_kb(contract, candidate, limit=limit)
        if result.hit_count > 0:
            return result

    return KBSearchResult(
        project=contract.project,
        query=prompt,
        status=fallback.status,
        reason_code=fallback.reason_code,
        hit_count=0,
        hits=(),
    )


def _render_direct_route_answer(
    *,
    query_class: RouteQueryClass,
    adapter: AdapterConfig,
    search_result: KBSearchResult,
) -> str:
    label = "KB-backed matches"
    if query_class == RouteQueryClass.FILE_LOOKUP:
        label = "KB-backed file matches"
    elif query_class == RouteQueryClass.MANIFEST_LOOKUP:
        label = "KB-backed manifest matches"

    lines = [f"{label} for {adapter.project_name}:"]
    for hit in search_result.hits:
        lines.append(
            f"- {hit.path}#{hit.chunk_index} [{hit.authority}/{hit.source_kind}]"
        )
    return "\n".join(lines)


def _render_status_route_answer(
    *,
    status: KBStatus,
    adapter: AdapterConfig,
) -> str:
    lines = [
        f"KB status for {adapter.project_name}: {status.status}",
        f"- search_db: {status.search_db_path}",
        f"- documents: {status.document_count}",
        f"- chunks: {status.chunk_count}",
    ]
    if status.reason_code:
        lines.append(f"- reason: {status.reason_code}")
    if status.built_at:
        lines.append(f"- built_at: {status.built_at}")
    return "\n".join(lines)


def _render_execution_handoff(
    *,
    adapter: AdapterConfig,
    search_result: KBSearchResult,
) -> str:
    lines = [
        f"Execution requests for {adapter.project_name} stay on explicit execution lanes.",
        "Use `ollarma workflow --project <name> --manifest-ref <ref> --step-id <id>` for validated manifest-backed work,",
        "or `ollarma autopilot <project> --run` for bounded asset execution.",
    ]
    if search_result.hit_count:
        lines.append("Relevant grounded refs:")
        for hit in search_result.hits:
            lines.append(f"- {hit.path}#{hit.chunk_index}")
    return "\n".join(lines)


def _route_escalation_result(
    *,
    adapter: AdapterConfig,
    prompt: str,
    query_class: RouteQueryClass,
    search_result: KBSearchResult,
    reason_code: str,
    reason_detail: str,
    next_action: str,
    model: str | None = None,
    namespace_prefix: str = "",
) -> RouteResult:
    receipt = build_escalation_receipt(
        project=adapter.project_name,
        lane="route_prompt",
        task_class=query_class.value,
        local_model=model,
        reason_code=ReasonCode.DEPENDENCY_MISSING,
        reason_detail=reason_detail,
        resource_snapshot={
            "prompt": prompt,
            "kb_status": search_result.status,
            "evidence_count": search_result.hit_count,
        },
        next_action=next_action,
    )
    return RouteResult(
        final_response=reason_detail,
        tool_calls_count=0,
        model=model,
        project=adapter.project_name,
        lane="frontier_or_human",
        reason_code=reason_code,
        query_class=query_class.value,
        kb_status=search_result.status,
        evidence_refs=_route_evidence_refs(search_result),
        next_action=next_action,
        route_receipt=_route_receipt(
            lane="frontier_or_human",
            reason_code=reason_code,
            query_class=query_class,
            kb_status=search_result.status,
            evidence_count=search_result.hit_count,
            next_action=next_action,
            selected_model=model,
            namespace=namespace_prefix,
        ),
        escalation_receipt=receipt.model_dump(mode="json"),
    )


def _grounded_synthesis_response(
    *,
    prompt: str,
    effective_model: str,
    adapter: AdapterConfig,
    search_result: KBSearchResult,
    namespace_prefix: str = "",
) -> RouteResult:
    evidence_blocks = []
    for index, hit in enumerate(search_result.hits, start=1):
        snippet = hit.text.strip()
        if len(snippet) > 500:
            snippet = snippet[:500].rstrip() + "..."
        evidence_blocks.append(
            f"[{index}] {hit.path}#{hit.chunk_index} ({hit.authority}/{hit.source_kind})\n{snippet}"
        )

    grounded_prompt = (
        f"You are answering a bounded local helper question for project {adapter.project_name}.\n"
        "Use only the grounded evidence below. Cite repo-relative paths in the answer.\n"
        "If the evidence is insufficient, say exactly: INSUFFICIENT_GROUNDED_EVIDENCE.\n\n"
        f"Question:\n{prompt}\n\nGrounded evidence:\n" + "\n\n".join(evidence_blocks)
    )
    try:
        response = _ollama_client_for_local_inference().chat(
            model=effective_model,
            messages=[{"role": "user", "content": grounded_prompt}],
        ).message.content or ""
    except Exception as exc:
        if not _is_local_inference_timeout(exc):
            raise
        return _route_escalation_result(
            adapter=adapter,
            prompt=prompt,
            query_class=RouteQueryClass.SUMMARY_REQUEST,
            search_result=search_result,
            reason_code=LOCAL_INFERENCE_TIMEOUT_REASON_CODE,
            reason_detail=(
                f"Local model inference exceeded {_local_inference_timeout_s():.1f}s "
                f"for {effective_model}."
            ),
            next_action="retry_smaller_local_or_escalate",
            model=effective_model,
            namespace_prefix=namespace_prefix,
        )
    if "INSUFFICIENT_GROUNDED_EVIDENCE" in response:
        return _route_escalation_result(
            adapter=adapter,
            prompt=prompt,
            query_class=RouteQueryClass.SUMMARY_REQUEST,
            search_result=search_result,
            reason_code=RouteReasonCode.INSUFFICIENT_GROUNDED_EVIDENCE.value,
            reason_detail="Grounded evidence was insufficient for a reliable helper answer.",
            next_action="orchestrator_or_frontier",
            model=effective_model,
            namespace_prefix=namespace_prefix,
        )

    return RouteResult(
        final_response=response,
        tool_calls_count=0,
        model=effective_model,
        project=adapter.project_name,
        lane="grounded_local_synthesis",
        reason_code=RouteReasonCode.GROUNDED_LOCAL_SYNTHESIS.value,
        query_class=RouteQueryClass.SUMMARY_REQUEST.value,
        kb_status=search_result.status,
        evidence_refs=_route_evidence_refs(search_result),
        next_action="none",
        route_receipt=_route_receipt(
            lane="grounded_local_synthesis",
            reason_code=RouteReasonCode.GROUNDED_LOCAL_SYNTHESIS.value,
            query_class=RouteQueryClass.SUMMARY_REQUEST,
            kb_status=search_result.status,
            evidence_count=search_result.hit_count,
            next_action="none",
            selected_model=effective_model,
            namespace=namespace_prefix,
        ),
    )


def _build_ladder_decision(
    *,
    workload_class: WorkloadClass,
    override_model: str | None,
) -> "LadderDecision":
    """Build a routing ladder decision from live telemetry + selection artifacts.

    Always returns a valid LadderDecision — errors are absorbed and result in
    a rescue-fallback decision so the caller can proceed safely.

    Phase 53 ROUTE-01/02/03, OBS-02/03.
    """
    from ollarma.routing_ladder import build_ladder  # noqa: PLC0415
    from ollarma.residency import RESCUE_MODEL  # noqa: PLC0415
    from ollarma.guards import collect_runtime_telemetry  # noqa: PLC0415
    from ollarma.scheduler import SWAP_DEGRADED_THRESHOLD_MB as _THRESHOLD  # noqa: PLC0415

    # Collect swap telemetry (best-effort)
    try:
        telemetry = collect_runtime_telemetry()
        swap_mb: float | None = telemetry.swap_used_mb
        resident_models: tuple[str, ...] = tuple(telemetry.loaded_models)
    except Exception:  # noqa: BLE001
        swap_mb = None
        resident_models = ()

    # Resolve benchmark-backed winner + pareto alternates (best-effort)
    selection_winner: str | None = None
    ranked_alternates: tuple[str, ...] = ()
    try:
        selection_winner, ranked_alternates = resolve_ranked_selection(workload_class)
    except Exception:  # noqa: BLE001
        pass
    if _is_reserved_model(selection_winner):
        selection_winner = None
    ranked_alternates = _drop_reserved_models(ranked_alternates)

    decision = build_ladder(
        workload_class.value,
        selection_result=selection_winner,
        ranked_alternates=ranked_alternates if ranked_alternates else None,
        swap_used_mb=swap_mb,
        swap_threshold_mb=float(_THRESHOLD),
        rescue_model=RESCUE_MODEL,
        resident_models=resident_models,
        override_model=override_model,
    )
    _set_last_ladder_decision(decision)
    return decision


def _route_prompt_impl(
    *,
    prompt: str,
    adapter: AdapterConfig,
    model: str | None,
    namespace_prefix: str = "",
) -> RouteResult:
    _assert_model_not_reserved(model, context="route_prompt")
    # --- Phase 53: compute routing ladder decision up-front (ROUTE-01/02/03) ---
    ladder_decision = _build_ladder_decision(
        workload_class=WorkloadClass.ROUTE_PROMPT,
        override_model=model,
    )

    # Determine effective_model from ladder (unless user passed explicit model=)
    if model is not None:
        # Explicit override — ladder still computed for observability but model wins
        ladder_model = model
    elif ladder_decision.chosen_model is not None:
        ladder_model = ladder_decision.chosen_model
    else:
        # blocked_escalate: return explicit escalation receipt (ROUTE-03)
        ladder_model = None

    prompt_text = prompt.strip()
    query_class = classify_route_prompt(prompt_text)
    contract = build_project_knowledge_contract(
        project_name=adapter.project_name,
        project_root=adapter.project_root,
        knowledge_base=adapter.knowledge_base,
        databases=adapter.databases,
    )
    kb_status = load_kb_status(contract)

    # Phase 53 ROUTE-03: if the ladder returned blocked_escalate, emit a scribe
    # entry and return an explicit escalation receipt — never silently lapse outward.
    if ladder_decision.status == "blocked_escalate" and ladder_model is None:
        from ollarma.scribe_hooks import end_of_run as _end_of_run  # noqa: PLC0415
        from ollarma.escalation import ReasonCode, build_escalation_receipt  # noqa: PLC0415
        _rungs_desc = ", ".join(
            f"{r.model}(rank={r.rank})" for r in ladder_decision.rungs_considered[:5]
        ) or "none"
        _end_of_run(
            "route_prompt",
            state="blocked",
            project=adapter.project_name,
            task="route_prompt",
            notes=(
                f"[ladder blocked_escalate] reason_code={ladder_decision.reason_code} "
                f"workload_class={ladder_decision.workload_class} "
                f"rungs_considered={_rungs_desc}"
            ),
            next_action=ladder_decision.escalation_hint or "escalate_to_caller",
        )
        _esc_receipt = build_escalation_receipt(
            project=adapter.project_name,
            lane="route_prompt",
            task_class=query_class.value,
            local_model=None,
            reason_code=ReasonCode.SWAP_DEGRADED,
            reason_detail=ladder_decision.detail,
            resource_snapshot={
                "workload_class": ladder_decision.workload_class,
                "swap_used_mb": ladder_decision.swap_used_mb,
                "reason_code": ladder_decision.reason_code,
                "rungs_considered": [r.model for r in ladder_decision.rungs_considered],
            },
            next_action=ladder_decision.escalation_hint or "escalate_to_caller",
        )
        return RouteResult(
            final_response=ladder_decision.detail,
            tool_calls_count=0,
            model=None,
            project=adapter.project_name,
            lane="frontier_or_human",
            reason_code=ladder_decision.reason_code,
            query_class=query_class.value,
            kb_status=None,
            evidence_refs=(),
            next_action=ladder_decision.escalation_hint or "escalate_to_caller",
            route_receipt=_route_receipt(
                lane="frontier_or_human",
                reason_code=ladder_decision.reason_code,
                query_class=query_class,
                kb_status=None,
                evidence_count=0,
                next_action=ladder_decision.escalation_hint or "escalate_to_caller",
                selected_model=None,
                namespace=namespace_prefix,
            ),
            escalation_receipt=_esc_receipt.model_dump(mode="json"),
            ladder=ladder_decision,
        )

    if query_class == RouteQueryClass.STATUS_LOOKUP:
        return RouteResult(
            final_response=_render_status_route_answer(status=kb_status, adapter=adapter),
            tool_calls_count=0,
            model=None,
            project=adapter.project_name,
            lane="kb_direct",
            reason_code=RouteReasonCode.KB_STATUS_ANSWER.value,
            query_class=query_class.value,
            kb_status=kb_status.status,
            evidence_refs=(),
            next_action="none",
            route_receipt=_route_receipt(
                lane="kb_direct",
                reason_code=RouteReasonCode.KB_STATUS_ANSWER.value,
                query_class=query_class,
                kb_status=kb_status.status,
                evidence_count=0,
                next_action="none",
                selected_model=None,
                namespace=namespace_prefix,
            ),
        )

    search_result = _search_route_evidence(contract, prompt_text, limit=3)

    if requires_execution_handoff(query_class):
        return RouteResult(
            final_response=_render_execution_handoff(
                adapter=adapter,
                search_result=search_result,
            ),
            tool_calls_count=0,
            model=None,
            project=adapter.project_name,
            lane="orchestrator_handoff",
            reason_code=RouteReasonCode.EXECUTION_LANE_REQUIRED.value,
            query_class=query_class.value,
            kb_status=search_result.status,
            evidence_refs=_route_evidence_refs(search_result),
            next_action="workflow_or_autopilot",
            route_receipt=_route_receipt(
                lane="orchestrator_handoff",
                reason_code=RouteReasonCode.EXECUTION_LANE_REQUIRED.value,
                query_class=query_class,
                kb_status=search_result.status,
                evidence_count=search_result.hit_count,
                next_action="workflow_or_autopilot",
                selected_model=None,
                namespace=namespace_prefix,
            ),
        )

    if search_result.status == "blocked":
        reason_code = search_result.reason_code or RouteReasonCode.INSUFFICIENT_GROUNDED_EVIDENCE.value
        detail = (
            f"Deterministic KB evidence is unavailable for {adapter.project_name}: {reason_code}. "
            "Build or repair the project KB before relying on helper routing."
        )
        return _route_escalation_result(
            adapter=adapter,
            prompt=prompt_text,
            query_class=query_class,
            search_result=search_result,
            reason_code=reason_code,
            reason_detail=detail,
            next_action="kb_build_or_frontier",
            model=None,
            namespace_prefix=namespace_prefix,
        )

    if is_exact_answer_query(query_class) and search_result.status == "ready" and search_result.hit_count > 0:
        return RouteResult(
            final_response=_render_direct_route_answer(
                query_class=query_class,
                adapter=adapter,
                search_result=search_result,
            ),
            tool_calls_count=0,
            model=None,
            project=adapter.project_name,
            lane="kb_direct",
            reason_code=RouteReasonCode.KB_DIRECT_ANSWER.value,
            query_class=query_class.value,
            kb_status=search_result.status,
            evidence_refs=_route_evidence_refs(search_result),
            next_action="none",
            route_receipt=_route_receipt(
                lane="kb_direct",
                reason_code=RouteReasonCode.KB_DIRECT_ANSWER.value,
                query_class=query_class,
                kb_status=search_result.status,
                evidence_count=search_result.hit_count,
                next_action="none",
                selected_model=None,
                namespace=namespace_prefix,
            ),
        )

    if search_result.hit_count == 0:
        detail = (
            f"No grounded KB evidence matched this {query_class.value} request for {adapter.project_name}. "
            "Helper routing will not guess beyond deterministic evidence."
        )
        return _route_escalation_result(
            adapter=adapter,
            prompt=prompt_text,
            query_class=query_class,
            search_result=search_result,
            reason_code=RouteReasonCode.INSUFFICIENT_GROUNDED_EVIDENCE.value,
            reason_detail=detail,
            next_action="orchestrator_or_frontier",
            model=None,
            namespace_prefix=namespace_prefix,
        )

    if search_result.status == "stale":
        detail = (
            f"KB evidence for {adapter.project_name} is stale. "
            "Rebuild the KB or escalate before trusting helper synthesis."
        )
        return _route_escalation_result(
            adapter=adapter,
            prompt=prompt_text,
            query_class=query_class,
            search_result=search_result,
            reason_code=RouteReasonCode.KB_STALE.value,
            reason_detail=detail,
            next_action="kb_build_or_frontier",
            model=None,
            namespace_prefix=namespace_prefix,
        )

    # Phase 53 ROUTE-01: use ladder-chosen model; fall back to default resolver
    # only if the ladder had no winner (e.g. missing artifact — rescue still runs).
    effective_model = ladder_model or resolve_default_model(
        workload_class=WorkloadClass.ROUTE_PROMPT
    )
    _assert_model_not_reserved(effective_model, context="route_prompt")
    _inference_result = RouteResult.model_validate(
        _submit_inference_job(
            project=adapter.project_name,
            lane=Lane.LOCAL_INFERENCE_SINGLE,
            model=effective_model,
            callback=lambda: _grounded_synthesis_response(
                prompt=prompt_text,
                effective_model=effective_model,
                adapter=adapter,
                search_result=search_result,
                namespace_prefix=namespace_prefix,
            ),
        )
    )
    # Attach the ladder decision to the result for OBS-03 observability.
    return RouteResult.model_validate(
        {**_inference_result.model_dump(mode="python"), "ladder": ladder_decision}
    )


def _workflow_class_from_task_class(task_class: str) -> WorkloadClass:
    """Normalize external workflow labels to execution-policy workload classes."""
    normalized = task_class.strip().lower().replace("-", "_")
    mapping = {
        "validated_script": WorkloadClass.VALIDATED_SCRIPT,
        "validated_pytest_suite": WorkloadClass.PYTEST_SUITE,
        "pytest_suite": WorkloadClass.PYTEST_SUITE,
        "validated_notebook": WorkloadClass.NOTEBOOK,
        "notebook": WorkloadClass.NOTEBOOK,
        "validated_pipeline_step": WorkloadClass.PIPELINE_STEP,
        "pipeline_step": WorkloadClass.PIPELINE_STEP,
    }
    if normalized not in mapping:
        raise ValueError(
            "task_class must be one of: validated-script, validated-pytest-suite, "
            "validated-notebook, validated-pipeline-step"
        )
    return mapping[normalized]


def _workflow_dependency_detail(workload_class: WorkloadClass) -> str | None:
    """Return a deterministic missing-dependency detail for workflow execution."""
    if workload_class == WorkloadClass.NOTEBOOK:
        if importlib.util.find_spec("papermill") is None:
            return "papermill is not installed for notebook workflow execution"
    if workload_class == WorkloadClass.PIPELINE_STEP:
        if shutil.which("snakemake") is None:
            return "snakemake is not installed for pipeline-step workflow execution"
    return None


def _resolve_workflow_step(
    manifest: ValidatedWorkflowManifest,
    step_id: str,
) -> ManifestStep:
    """Return the declared workflow step for the requested manifest step_id."""
    for step in manifest.steps:
        if step.step_id == step_id:
            return step
    raise ValueError(f"step_id not found in workflow manifest: {step_id}")


def _workflow_submission_result(
    *,
    project: str,
    manifest: ValidatedWorkflowManifest,
    step: ManifestStep,
    lane: str,
    status: str,
    scheduler: RuntimeSnapshot,
    queue_depth: int,
    model: str | None,
    reason_code: str | None,
    escalation_receipt: dict | None,
    receipt_ref: dict[str, str] | None = None,
    checkpoint_ref: dict[str, str] | None = None,
    owning_lane: str | None = None,
    next_stage: str | None = None,
    handoff_required: bool = False,
) -> WorkflowSubmissionResult:
    """Build a normalized workflow admission result with portable manifest refs."""
    payload = manifest.to_service_payload()
    manifest_ref = payload.get("manifest_ref")
    if manifest_ref is None:
        raise ValueError("workflow manifest did not expose a portable manifest_ref")
    materialization_root = None
    if hasattr(step, "materialization_root") and hasattr(step.materialization_root, "to_service_payload"):
        candidate = step.materialization_root.to_service_payload()
        if isinstance(candidate, dict):
            materialization_root = candidate
    consumer_repo = manifest.consumer_repo if isinstance(getattr(manifest, "consumer_repo", None), str) else None

    return WorkflowSubmissionResult(
        project=project,
        manifest_ref=manifest_ref,
        manifest_digest=payload["manifest_digest"],
        run_id=manifest.run_id,
        step_id=step.step_id,
        task_class=step.task_type,
        consumer_repo=consumer_repo,
        materialization_root=materialization_root,
        lane=lane,
        status=status,
        model=model,
        queue_depth=queue_depth,
        reason_code=reason_code,
        scheduler=scheduler,
        escalation_receipt=escalation_receipt,
        receipt_ref=receipt_ref,
        checkpoint_ref=checkpoint_ref,
        owning_lane=owning_lane,
        next_stage=next_stage,
        handoff_required=handoff_required,
    )


def _workflow_paths(
    manifest: ValidatedWorkflowManifest,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path] | None:
    """Return repo-root, receipts path, and checkpoint path for a bound manifest."""
    repo_root = getattr(manifest, "_repo_root", None)
    if not isinstance(repo_root, pathlib.Path):
        return None
    artifact_root = manifest.artifact_roots[0].locator.repo_relative
    checkpoint_root = manifest.checkpoint_policy.checkpoint_root.repo_relative
    if artifact_root is None or checkpoint_root is None:
        return None
    run_root = repo_root / artifact_root
    return repo_root, run_root / "receipts.json", (repo_root / checkpoint_root / "checkpoint.json")


def _record_workflow_submission(
    *,
    manifest: ValidatedWorkflowManifest,
    step: ManifestStep,
    lane: str,
    status: str,
    reason_code: str | None,
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """Append a preflight workflow receipt and checkpoint when repo context is bound."""
    paths = _workflow_paths(manifest)
    if paths is None:
        return None, None
    repo_root, receipts_path, checkpoint_path = paths
    manifest_payload = manifest.to_service_payload()
    receipt_rel = receipts_path.relative_to(repo_root).as_posix()
    checkpoint_rel = checkpoint_path.relative_to(repo_root).as_posix()
    checkpoint_ref = {"repo_relative": checkpoint_rel}
    receipt = append_run_receipt(
        repo_root=repo_root,
        receipts_path=receipts_path,
        receipt=RunReceipt(
            run_id=manifest.run_id,
            stage="preflight",
            step_id=step.step_id,
            task_or_command=f"workflow-step:{step.step_id}",
            lane=lane,
            status=status,
            inputs=(manifest_payload.get("manifest_ref", {}),),
            outputs=(
                {
                    "digest": manifest_payload["manifest_digest"],
                    "materialization_root": step.materialization_root.to_service_payload(),
                },
            ),
            duration_s=0.0,
            retry_count=0,
            reason_code=reason_code,
            checkpoint_ref=checkpoint_ref,
        ),
    )
    checkpoint = apply_receipt_to_checkpoint(
        CheckpointState(
            run_id=manifest.run_id,
            current_stage="preflight",
            last_validated_stage=None,
            last_receipt_hash=receipt.receipt_hash,
            retry_budget_remaining=manifest.checkpoint_policy.max_retries,
            resume_from_step=step.step_id,
        ),
        receipt,
    )
    write_checkpoint_state(
        repo_root=repo_root,
        checkpoint_path=checkpoint_path,
        state=checkpoint,
    )
    return (
        {"repo_relative": receipt_rel, "digest": f"sha256:{receipt.receipt_hash}"},
        checkpoint_ref,
    )


def _route_submission(
    *,
    status: str,
    reason_code: str | None,
    step_id: str,
) -> tuple[str | None, str | None, bool]:
    """Return owning-lane metadata for workflow admission responses."""
    if status in {"accepted", "queued"}:
        checkpoint_state = CheckpointState(
            run_id="submission",
            current_stage="preflight",
            last_validated_stage="preflight",
            last_receipt_hash="0" * 64,
            retry_budget_remaining=1,
            resume_from_step=step_id,
        )
        decision = determine_next_stage(
            checkpoint_state=checkpoint_state,
        )
    else:
        decision = determine_next_stage(
            requested_stage=WorkflowStage.INTERPRET_ESCALATE,
            checkpoint_state=CheckpointState(
                run_id="submission",
                current_stage="preflight",
                last_validated_stage=None,
                last_receipt_hash="0" * 64,
                retry_budget_remaining=0,
                resume_from_step=step_id,
            ),
            reason_code=reason_code,
        )
    return decision.owning_lane, decision.next_stage.value, decision.handoff_required


def resume_workflow_run(
    project: str,
    manifest_ref: ManifestRefInput,
    *,
    adapters_dir: str | None = None,
) -> dict[str, str | None]:
    """Resolve the next resumable stage for a previously admitted workflow run."""
    adapter = resolve_project_adapter(project, adapters_dir=adapters_dir, service_mode=False)
    manifest = load_workflow_manifest(
        manifest_ref,
        allowlisted_roots=[pathlib.Path(adapter.project_root)],
    )
    paths = _workflow_paths(manifest)
    if paths is None:
        raise ValueError("workflow manifest has no bound repo context for resume")
    repo_root, receipts_path, checkpoint_path = paths
    resume = resolve_workflow_resume(
        manifest.run_id,
        repo_root=repo_root,
        receipts_path=receipts_path,
        checkpoint_path=checkpoint_path,
    )
    return resume.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def run_benchmark(
    dry_run: bool = False,
    models_filter: list[str] | None = None,
    suites_filter: list[str] | None = None,
    trials: int = 3,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    skip_preflight: bool = False,
    results_dir: pathlib.Path = pathlib.Path("results"),
    on_progress: Callable[[str], None] | None = None,
) -> BenchmarkRunResult:
    """Run benchmarks and return structured result.

    Raises:
        NoModelsError: If no models found in models.yml.
        NoResultsError: If no tasks found.
        PreflightError: If pre-flight checks fail (unless skip_preflight=True).
    """
    def _log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # Log thermal state at run start (BENCH-05)
    from ollarma.bench_refresh import log_thermal_state, enforce_hf_offline
    if models_filter:
        for model_name in models_filter:
            _assert_model_not_reserved(model_name, context="benchmark")
    _log(f"thermal state at run start: {log_thermal_state()}")
    enforce_hf_offline()

    models = load_models()
    tasks = load_tasks()

    if not models:
        raise NoModelsError("No models found in models.yml")
    if not tasks:
        raise NoResultsError("No task YAMLs found under tasks/")

    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store = ResultStore(run_id)
    store.RESULTS_DIR = results_dir

    all_results: list[BenchmarkResult] = []

    if dry_run:
        model = models[0]
        _assert_model_not_reserved(model.name, context="benchmark dry-run")
        task = tasks[0]
        _log(f"dry-run: model={model.name!r} task={task.id!r} suite={task.suite!r}")
        result = run_inference(
            model=model.name,
            prompt=task.prompt,
            task_id=task.id,
            suite=task.suite,
            num_ctx=task.num_ctx,
            num_predict=num_predict if num_predict is not None else task.num_predict,
        )
        store.append(result)
        all_results.append(result)
        sealed_path = store.seal()
        rows = orjson.loads(sealed_path.read_bytes())
        chain = build_receipt_chain(rows, run_id=run_id)
        evidence_path = write_evidence_file(chain, results_dir, run_id)

        return BenchmarkRunResult(
            run_id=run_id,
            rows_written=1,
            sealed_path=str(sealed_path),
            evidence_path=str(evidence_path),
            receipt_count=chain.receipt_count,
            results=all_results,
            dry_run=True,
        )

    # --- GPU-07: Residency-aware pressure guard (replaces raw swap-only block) ---
    #
    # Phase 52 GPU-01..04 defined a rescue-model residency policy where
    # qwen2.5:1.5b is always pinned in unified memory precisely so the system
    # can keep serving under pressure. The old swap-only guard bypassed that
    # path by hard-blocking on swap > 512 MB regardless of residency state.
    #
    # New three-tier gate:
    #   1. Hard block on kernel pressure >= CRITICAL (system is thrashing).
    #   2. Hard block on swap degraded AND rescue model not resident
    #      (no safe fallback available).
    #   3. Degraded mode: swap degraded AND rescue resident -> run benchmark
    #      against rescue model ONLY; still produces a fresh selection artifact.
    from ollarma.scheduler import (  # noqa: PLC0415
        MEMORY_PRESSURE_CRITICAL,
        SWAP_DEGRADED_THRESHOLD_MB,
        _read_memory_pressure_level,
    )
    from ollarma.residency import RESCUE_MODEL  # noqa: PLC0415
    _snap = get_scheduler_snapshot()
    _pressure_level = _read_memory_pressure_level()
    _swap_mb = _snap.swap_used_mb if _snap.swap_used_mb is not None else 0.0
    _swap_over = _swap_mb > SWAP_DEGRADED_THRESHOLD_MB

    # Tier 1: critical kernel pressure -> hard block
    if _pressure_level is not None and _pressure_level >= MEMORY_PRESSURE_CRITICAL:
        raise ValueError(
            f"MEMORY_PRESSURE_CRITICAL: kern.memorystatus_vm_pressure_level="
            f"{_pressure_level} (swap_used_mb={_swap_mb:.0f})"
        )

    # Need a live residency decision to know if the rescue path is available.
    try:
        _residency = get_residency_decision()
        _rescue_resident = bool(_residency.rescue_resident)
    except Exception:  # noqa: BLE001
        _rescue_resident = False

    # Tier 2: swap degraded AND no rescue fallback -> hard block
    if _swap_over and not _rescue_resident:
        raise ValueError(
            f"SWAP_DEGRADED_NO_RESCUE: swap_used_mb={_swap_mb:.0f} "
            f"exceeds threshold {SWAP_DEGRADED_THRESHOLD_MB:.0f}MB "
            f"AND rescue model '{RESCUE_MODEL}' is not resident in GPU memory"
        )

    # Tier 3: swap degraded AND rescue resident -> constrain to rescue only.
    # Mutates local models_filter to force rescue-only benchmark. The receipt
    # chain records the raw swap_used_mb so operators can see the degraded run.
    _degraded_mode_rescue_only = _swap_over and _rescue_resident
    if _degraded_mode_rescue_only:
        _log(
            f"benchmark_degraded_mode: rescue-only path "
            f"(swap_used_mb={_swap_mb:.0f}MB > threshold "
            f"{SWAP_DEGRADED_THRESHOLD_MB:.0f}MB, rescue='{RESCUE_MODEL}' resident). "
            f"pressure_level={_pressure_level}"
        )
        if models_filter:
            _intersect = [m for m in models_filter if m == RESCUE_MODEL]
            models_filter = _intersect if _intersect else [RESCUE_MODEL]
        else:
            models_filter = [RESCUE_MODEL]

    # --- PIPE-10: Benchmark freeze flag (D-01) ---
    from ollarma.pipeline_control import get_pipeline_controller  # noqa: PLC0415
    _ctrl = get_pipeline_controller()
    _ctrl.start_benchmark()
    _log("benchmark_active.flag written")

    try:
        # Non-dry-run: guards run before any inference
        if skip_preflight:
            _log("Pre-flight checks skipped (--skip-preflight)")
        else:
            preflight_check()
            _log("Pre-flight OK")

        warmed: set[str] = set()

        filtered_models = _apply_model_filter(models, models_filter)
        filtered_tasks = _apply_suite_filter(tasks, suites_filter)

        if not filtered_models:
            raise NoModelsError(f"No models matched filter: {models_filter}")
        if not filtered_tasks:
            raise NoResultsError(f"No tasks matched suite filter: {suites_filter}")

        # models.yml is a CANDIDATE ROSTER: entries need not be pulled in ollama
        # (see models.yml header). Skip any roster model not present in
        # `ollama list` so one un-pulled candidate cannot abort the whole run
        # (which previously left selection SELECTION_STALE — no artifact sealed).
        present = list_present_models()
        runnable_models = []
        for _m in filtered_models:
            if model_is_present(_m.name, present):
                runnable_models.append(_m)
            else:
                _log(
                    f"  [skip] {_m.name}: not present in `ollama list` "
                    f"(candidate roster entry, not pulled) — skipping, run continues"
                )
        if not runnable_models:
            raise NoModelsError(
                "No roster models are present in `ollama list`. "
                "Pull at least one candidate before benchmarking."
            )
        filtered_models = runnable_models

        rows_written = 0
        try:
            for model_cfg in filtered_models:
                try:
                    warmup_model(
                        model_cfg.name,
                        filtered_tasks[0].prompt,
                        filtered_tasks[0].num_ctx,
                        warmed,
                    )
                    for task_cfg in filtered_tasks:
                        effective_num_ctx = num_ctx if num_ctx is not None else task_cfg.num_ctx
                        for trial in range(trials):
                            result = run_inference(
                                model=model_cfg.name,
                                prompt=task_cfg.prompt,
                                task_id=task_cfg.id,
                                suite=task_cfg.suite,
                                num_ctx=effective_num_ctx,
                                num_predict=num_predict if num_predict is not None else task_cfg.num_predict,
                            )
                            quality = _dispatch_scorer(result)
                            result = result.model_copy(update={"quality_score": quality})
                            store.append(result)
                            all_results.append(result)
                            rows_written += 1
                            _log(
                                f"  [{trial + 1}/{trials}] {model_cfg.name} x {task_cfg.id}: "
                                f"decode={result.decode_tps:.1f} tps  score={quality}"
                            )
                            time.sleep(2)
                except Exception as exc:
                    # No single model may abort the whole run. A roster entry can
                    # be present yet un-runnable via generate (e.g. an
                    # embedding-only model like nomic-embed-text -> HTTP 400), or
                    # fail transiently (OOM, upstream hiccup). Log and skip to the
                    # next model so prior rows still seal. KeyboardInterrupt is a
                    # BaseException, so it is NOT caught here and still reaches the
                    # interrupt handler below.
                    _log(
                        f"  [skip] {model_cfg.name}: inference failed "
                        f"({type(exc).__name__}: {exc}) — skipping model, run continues"
                    )
                    continue
        except KeyboardInterrupt:
            _log(f"Interrupted. {rows_written} row(s) written before interrupt.")
        finally:
            if rows_written > 0:
                sealed_path = store.seal()
                rows = orjson.loads(sealed_path.read_bytes())
                chain = build_receipt_chain(rows, run_id=run_id)
                evidence_path = write_evidence_file(chain, results_dir, run_id)

        if rows_written > 0:
            return BenchmarkRunResult(
                run_id=run_id,
                rows_written=rows_written,
                sealed_path=str(sealed_path),
                evidence_path=str(evidence_path),
                receipt_count=chain.receipt_count,
                results=all_results,
                dry_run=False,
            )

        raise NoResultsError("No rows written -- nothing to seal.")
    finally:
        _ctrl.end_benchmark()
        _log("benchmark_active.flag cleared")


def list_models_and_tasks() -> ModelsAndTasks:
    """Load models and tasks from YAML configs.

    Raises:
        NoModelsError: If no models found in models.yml.
    """
    models = load_models()
    tasks = load_tasks()

    if not models:
        raise NoModelsError("No models found in models.yml")

    return ModelsAndTasks(models=models, tasks=tasks)


def generate_report(
    run_id: str | None = None,
    results_dir: pathlib.Path = pathlib.Path("results"),
) -> ReportResult:
    """Generate report from sealed benchmark results.

    IMPORTANT: This function DOES write the evidence chain file and artifact file
    (they are part of the evidence contract). It does NOT write results.md or
    model_selection.md (those are presentation files the CLI writes).

    Raises:
        NoResultsError: If no sealed files found.
        FileNotFoundError: If specific run_id sealed results not found.
    """
    # Resolve which sealed JSON to read
    if run_id:
        sealed_path = results_dir / f"run-{run_id}.json"
    else:
        try:
            sealed_path = find_latest_sealed(results_dir)
        except FileNotFoundError:
            raise NoResultsError("No sealed results found in results/ directory.")

    # Load and aggregate
    rows = load_sealed_results(sealed_path)
    stats = aggregate_trials(rows)

    # Generate MD strings
    results_md = render_results_md(stats)
    selection_md = render_selection_md(stats)

    # Extract run_id from sealed_path for evidence/artifact file naming
    run_id_str = sealed_path.stem.removeprefix("run-")

    # Evidence chain: generate if missing (backward compat with pre-Phase-6 runs)
    evidence_path = results_dir / f"run-{run_id_str}.evidence.json"
    if not evidence_path.exists():
        chain = build_receipt_chain(rows, run_id=run_id_str)
        write_evidence_file(chain, results_dir, run_id_str)

    # Read evidence root for artifact
    evidence_data_final = orjson.loads(evidence_path.read_bytes())
    evidence_root_val = evidence_data_final["evidence_root"]

    # Build and write selection artifact
    artifact = build_selection_artifact(
        run_id=run_id_str,
        evidence_root=evidence_root_val,
        stats=stats,
    )
    write_artifact_file(artifact, results_dir, run_id_str)

    return ReportResult(
        rows_loaded=len(rows),
        stats=stats,
        results_md=results_md,
        selection_md=selection_md,
        evidence_root=evidence_root_val,
        artifact_hash=artifact.stable_decision_hash,
    )


def verify_evidence(
    run_id: str,
    results_dir: pathlib.Path = pathlib.Path("results"),
) -> VerifyResult:
    """Verify the evidence chain for a sealed benchmark run.

    Raises:
        FileNotFoundError: If sealed results not found.
        ValueError: If chain verification fails.
    """
    sealed_path = results_dir / f"run-{run_id}.json"
    evidence_path = results_dir / f"run-{run_id}.evidence.json"

    # Load sealed results
    rows = load_sealed_results(sealed_path)

    # Load evidence chain -- generate if missing (backward compat)
    if not evidence_path.exists():
        chain = build_receipt_chain(rows, run_id=run_id)
        write_evidence_file(chain, results_dir, run_id)

    evidence_data = orjson.loads(evidence_path.read_bytes())
    receipts = evidence_data["receipts"]

    # Replay and verify
    evidence_root = verify_evidence_chain(rows, receipts)

    # Check evidence root matches stored value
    stored_root = evidence_data.get("evidence_root", "")
    if evidence_root != stored_root:
        raise ValueError(
            f"evidence_root mismatch: computed={evidence_root}, stored={stored_root}"
        )

    result = VerifyResult(
        valid=True,
        receipt_count=len(receipts),
        evidence_root=evidence_root,
    )

    # If artifact exists, verify its decision hash
    artifact_path = results_dir / f"run-{run_id}.artifact.json"
    if artifact_path.exists():
        artifact_data = orjson.loads(artifact_path.read_bytes())
        stored_hash = artifact_data.pop("stable_decision_hash", "")
        recomputed_hash = canonical_hash(artifact_data)
        if recomputed_hash != stored_hash:
            raise ValueError("stable_decision_hash mismatch in selection artifact")
        if artifact_data.get("evidence_root") != evidence_root:
            raise ValueError("evidence_root does not match chain in selection artifact")
        result = result.model_copy(update={
            "artifact_valid": True,
            "artifact_hash": stored_hash,
        })

    return result


def sonify_evidence(
    run_id: str,
    compare_id: str | None = None,
    results_dir: pathlib.Path = pathlib.Path("results"),
) -> SonifyResult:
    """Render evidence chain as audio WAV bytes.

    Single mode: returns mono WAV + tampered positions.
    Compare mode: returns stereo WAV.

    Raises:
        FileNotFoundError: If sealed results not found.
    """
    # Load first evidence chain
    sealed_path_a = results_dir / f"run-{run_id}.json"
    evidence_path_a = results_dir / f"run-{run_id}.evidence.json"

    rows_a = load_sealed_results(sealed_path_a)

    if not evidence_path_a.exists():
        chain = build_receipt_chain(rows_a, run_id=run_id)
        write_evidence_file(chain, results_dir, run_id)

    evidence_data_a = orjson.loads(evidence_path_a.read_bytes())
    receipts_a = evidence_data_a["receipts"]

    if compare_id is None:
        # Single chain mode (mono)
        wav_bytes = sonify_chain(rows_a, receipts_a)
        validity = validate_receipts(rows_a, receipts_a)
        tampered = [i for i, v in enumerate(validity) if not v]
        duration_s = len(receipts_a) * 0.3

        return SonifyResult(
            wav_bytes=wav_bytes,
            receipt_count=len(receipts_a),
            duration_s=duration_s,
            tampered_positions=tampered,
            mode="single",
        )
    else:
        # Comparison mode (stereo)
        sealed_path_b = results_dir / f"run-{compare_id}.json"
        evidence_path_b = results_dir / f"run-{compare_id}.evidence.json"

        rows_b = load_sealed_results(sealed_path_b)

        if not evidence_path_b.exists():
            chain_b = build_receipt_chain(rows_b, run_id=compare_id)
            write_evidence_file(chain_b, results_dir, compare_id)

        evidence_data_b = orjson.loads(evidence_path_b.read_bytes())
        receipts_b = evidence_data_b["receipts"]

        wav_bytes = sonify_comparison(rows_a, receipts_a, rows_b, receipts_b)
        max_receipts = max(len(receipts_a), len(receipts_b))
        duration_s = max_receipts * 0.3

        return SonifyResult(
            wav_bytes=wav_bytes,
            receipt_count=max_receipts,
            duration_s=duration_s,
            tampered_positions=[],
            mode="compare",
        )


def list_projects(
    adapters_dir: str | None = None,
    *,
    service_mode: bool = False,
) -> dict[str, AdapterConfig]:
    """Load fleet project registry.

    Resolves adapter directories through discovery precedence and entry points.
    Returns the registry dict directly (AdapterConfig is already a Pydantic model).
    """
    return _load_project_registry(adapters_dir, service_mode=service_mode)


def resolve_project_adapter(
    project: str,
    adapters_dir: str | None = None,
    *,
    service_mode: bool = False,
) -> AdapterConfig:
    """Resolve a named project adapter through the shared discovery path."""
    registry = _load_project_registry(adapters_dir, service_mode=service_mode)
    adapter = resolve_project(project, registry)
    if adapter is None:
        raise ValueError(f"Project '{project}' not found in fleet registry")
    if service_mode:
        _validate_service_project_root(adapter.project_root)
    return adapter


def read_sibling_file(
    path: str,
    namespace_prefix: str | None = None,
    *,
    adapters_dir: str | None = None,
):
    """Read an allowlisted sibling-project file. Raises ValueError on rejection."""
    from ollarma.sibling_reader import read_sibling_path
    from ollarma.guardrail import GuardrailGate
    from ollarma.fleet import AdapterConfig
    projects = list_projects(adapters_dir=adapters_dir, service_mode=True)
    roots = [pathlib.Path(adapter.project_root) for adapter in projects.values()]
    adapter = AdapterConfig(project_name="__system__", project_root="/")
    gate = GuardrailGate(adapter)
    return read_sibling_path(
        path,
        namespace=namespace_prefix or "__unscoped__",
        allowlisted_roots=roots,
        gate=gate,
    )


def list_project_workflows(
    project: str,
    adapters_dir: str | None = None,
    *,
    service_mode: bool = False,
) -> DashboardWorkflowCatalog:
    """Discover portable workflow manifests and runnable steps for one project."""
    adapter = resolve_project_adapter(
        project,
        adapters_dir=adapters_dir,
        service_mode=service_mode,
    )
    repo_root = pathlib.Path(adapter.project_root).resolve()
    manifest_dirs = (
        repo_root / ".ollarma" / "manifests",
        repo_root / "artifacts" / "ollarma" / "manifests",
        repo_root / "runs" / "ollarma" / "manifests",
    )

    manifests: list[DashboardWorkflowManifest] = []
    seen_refs: set[str] = set()
    for manifest_dir in manifest_dirs:
        if not manifest_dir.exists():
            continue
        for manifest_path in sorted(manifest_dir.glob("*.json")):
            manifest = load_workflow_manifest(
                manifest_path,
                allowlisted_roots=[repo_root],
            )
            payload = manifest.to_service_payload()
            manifest_ref = payload.get("manifest_ref")
            if not isinstance(manifest_ref, dict):
                continue
            ref_key = orjson.dumps(manifest_ref, option=orjson.OPT_SORT_KEYS).decode("utf-8")
            if ref_key in seen_refs:
                continue
            seen_refs.add(ref_key)
            manifests.append(
                DashboardWorkflowManifest(
                    manifest_ref=manifest_ref,
                    manifest_digest=payload["manifest_digest"],
                    run_id=manifest.run_id,
                    consumer_repo=manifest.consumer_repo,
                    steps=tuple(
                        DashboardWorkflowStep(
                            step_id=step.step_id,
                            stage=step.stage,
                            task_type=step.task_type,
                        )
                        for step in manifest.steps
                    ),
                )
            )

    return DashboardWorkflowCatalog(
        project=adapter.project_name,
        manifests=tuple(manifests),
    )


def get_project_kb_contract(
    project: str,
    adapters_dir: str | None = None,
    *,
    service_mode: bool = False,
) -> KBContractResult:
    """Return the resolved repo-local KB contract for a project adapter."""
    adapter = resolve_project_adapter(
        project,
        adapters_dir=adapters_dir,
        service_mode=service_mode,
    )
    contract = build_project_knowledge_contract(
        project_name=adapter.project_name,
        project_root=adapter.project_root,
        knowledge_base=adapter.knowledge_base,
        databases=adapter.databases,
    )
    return KBContractResult(contract=contract)


def _repo_relative_locator(project_root: pathlib.Path, target: pathlib.Path, *, digest: str | None = None) -> dict:
    payload = {"repo_relative": target.relative_to(project_root).as_posix()}
    if digest is not None:
        payload["digest"] = digest
    return payload


def build_project_kb(
    project: str,
    adapters_dir: str | None = None,
    *,
    service_mode: bool = False,
    reason: str = "manual",
) -> KBBuildResult:
    """Build deterministic repo-local KB artifacts for a project."""
    adapter = resolve_project_adapter(
        project,
        adapters_dir=adapters_dir,
        service_mode=service_mode,
    )
    contract = build_project_knowledge_contract(
        project_name=adapter.project_name,
        project_root=adapter.project_root,
        knowledge_base=adapter.knowledge_base,
        databases=adapter.databases,
    )
    artifacts = build_kb_artifacts(contract, reason=reason)
    project_root = pathlib.Path(contract.project_root)
    return KBBuildResult(
        project=contract.project,
        artifact_root=_repo_relative_locator(project_root, pathlib.Path(artifacts.artifact_root)),
        manifest_ref=_repo_relative_locator(project_root, pathlib.Path(artifacts.manifest_path), digest=f"sha256:{canonical_hash(artifacts.manifest.model_dump(mode='json'))}"),
        documents_ref=_repo_relative_locator(project_root, pathlib.Path(artifacts.documents_path)),
        chunks_ref=_repo_relative_locator(project_root, pathlib.Path(artifacts.chunks_path)),
        tags_ref=_repo_relative_locator(project_root, pathlib.Path(artifacts.tags_path)),
        receipt_ref=_repo_relative_locator(project_root, pathlib.Path(artifacts.receipts_path), digest=artifacts.receipt.manifest_hash),
        source_count=artifacts.manifest.source_count,
        document_count=artifacts.manifest.document_count,
        chunk_count=artifacts.manifest.chunk_count,
        reason_code=contract.reason_code if contract.reason_code == "KB_NOT_BUILT" else None,
    )


def get_project_kb_status(
    project: str,
    adapters_dir: str | None = None,
    *,
    service_mode: bool = False,
) -> KBStatus:
    """Return read-only KB status for a project."""
    adapter = resolve_project_adapter(
        project,
        adapters_dir=adapters_dir,
        service_mode=service_mode,
    )
    contract = build_project_knowledge_contract(
        project_name=adapter.project_name,
        project_root=adapter.project_root,
        knowledge_base=adapter.knowledge_base,
        databases=adapter.databases,
    )
    return load_kb_status(contract)


def search_project_kb(
    project: str,
    query: str,
    adapters_dir: str | None = None,
    *,
    service_mode: bool = False,
    limit: int = 5,
) -> KBSearchResult:
    """Return bounded read-only KB hits for a query."""
    query_text = query.strip()
    if not query_text:
        raise ValueError("KB search query must not be empty")
    if limit < 1:
        raise ValueError("KB search limit must be >= 1")

    adapter = resolve_project_adapter(
        project,
        adapters_dir=adapters_dir,
        service_mode=service_mode,
    )
    contract = build_project_knowledge_contract(
        project_name=adapter.project_name,
        project_root=adapter.project_root,
        knowledge_base=adapter.knowledge_base,
        databases=adapter.databases,
    )
    return search_kb(contract, query_text, limit=limit)


def submit_autopilot(
    project: str,
    *,
    run_assets: bool = False,
    threshold: float = 0.9,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    adapters_dir: str | None = None,
    service_mode: bool = False,
) -> AutopilotReport:
    """Run bounded autopilot discovery or execution for one project."""
    # v4.3 ADMIT-03 + SCRIBE-01/03: recovery admission + scribe hooks.
    from ollarma.admission import check_recovery  # noqa: PLC0415
    from ollarma.scribe_hooks import pre_dispatch, end_of_run  # noqa: PLC0415
    check_recovery(entrypoint="submit_autopilot")
    pre_dispatch("submit_autopilot", project=project, task="autopilot")
    _ok = False
    try:
        result = _submit_autopilot_body(
            project=project, run_assets=run_assets, threshold=threshold,
            include=include, exclude=exclude, adapters_dir=adapters_dir,
            service_mode=service_mode,
        )
        _ok = True
        return result
    except BaseException as exc:
        end_of_run(
            "submit_autopilot", state="blocked",
            project=project, task="autopilot",
            notes=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        if _ok:
            end_of_run("submit_autopilot", state="completed", project=project, task="autopilot")


def _submit_autopilot_body(
    project: str,
    *,
    run_assets: bool,
    threshold: float,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    adapters_dir: str | None,
    service_mode: bool,
) -> AutopilotReport:
    adapter = resolve_project_adapter(
        project,
        adapters_dir=adapters_dir,
        service_mode=service_mode,
    )
    return run_autopilot(
        project_name=adapter.project_name,
        project_root=adapter.project_root,
        run=run_assets,
        threshold=threshold,
        include_patterns=list(include) or None,
        exclude_patterns=list(exclude) or None,
    )


def chat_with_model(
    message: str,
    model: str | None = None,
) -> ChatResult:
    """Send a single prompt to an Ollama model and return its response."""
    resolution = resolve_generic_chat_model(model)
    effective_model = resolution.effective_model
    if not effective_model:
        detail = resolution.detail or "No local model is available for generic helper chat."
        commands = "\n".join(f"- `{command}`" for command in resolution.recovery_commands)
        response = (
            "BLOCKED: generic helper chat cannot select a local model right now.\n\n"
            f"Reason: {detail}\n\n"
            "Recovery path:\n"
            f"{commands or '- Start Ollama and install a bounded local helper model.'}"
        )
        return ChatResult(
            response=response,
            model="unavailable",
            status="blocked",
            reason_code=resolution.reason_code,
            detail=detail,
            recovery_commands=resolution.recovery_commands,
        )
    try:
        result = _submit_inference_job(
            project=None,
            lane=Lane.LOCAL_INFERENCE_SINGLE,
            model=effective_model,
            callback=lambda: ChatResult(
                response=_ollama_client_for_local_inference().chat(
                    model=effective_model,
                    messages=[{"role": "user", "content": message}],
                ).message.content
                or "",
                model=effective_model,
            ),
        )
    except Exception as exc:
        if not _is_local_inference_timeout(exc):
            raise
        detail = (
            f"Local model inference exceeded {_local_inference_timeout_s():.1f}s "
            f"for {effective_model}."
        )
        return ChatResult(
            response=f"BLOCKED: {detail}",
            model=effective_model,
            status="blocked",
            reason_code=LOCAL_INFERENCE_TIMEOUT_REASON_CODE,
            detail=detail,
            recovery_commands=(
                "retry with a smaller local model",
                "inspect Ollama residency and scheduler state",
            ),
        )
    return ChatResult.model_validate(result)


def route_prompt(
    prompt: str,
    project: str,
    model: str | None = None,
    adapters_dir: str | None = None,
    *,
    service_mode: bool = False,
    namespace_prefix: str | None = None,
) -> RouteResult:
    """Route a prompt through the retrieval-first helper lane for a named project."""
    # v4.3 ADMIT-01 + SCRIBE-01..03: recovery admission + scribe hooks.
    from ollarma.admission import check_recovery  # noqa: PLC0415
    from ollarma.scribe_hooks import hooked  # noqa: PLC0415
    check_recovery(entrypoint="route_prompt")
    with hooked("route_prompt", project=project, task="route_prompt"):
        return _route_prompt_body(
            prompt=prompt, project=project, model=model,
            adapters_dir=adapters_dir, service_mode=service_mode,
            namespace_prefix=namespace_prefix,
        )


# Reason codes that describe a transient / remediable condition rather than a
# durable answer. Memoizing these (NS-04 idempotency cache) would replay a
# one-time "could not answer locally → escalate to cloud/human" verdict for
# every identical prompt, so KB rebuilds, swap relief, or selection refresh
# would never take effect for already-seen prompts. Re-evaluate them instead.
_TRANSIENT_ROUTE_REASON_CODES = frozenset(
    {
        "KB_STALE",
        "KB_NOT_BUILT",
        "DEPENDENCY_MISSING",
        "SWAP_DEGRADED",
        "INSUFFICIENT_GROUNDED_EVIDENCE",
        "SELECTION_STALE",
        "SELECTION_MISSING",
        "QUEUE_TIMEOUT",
        "RESOURCE_BUDGET_EXCEEDED",
        "ROUTING_DEGRADED",
        "ROUTING_RESCUE_ONLY",
        "ROUTING_BLOCKED_ESCALATE",
    }
)


def _route_result_is_memoizable(result: RouteResult) -> bool:
    """Only durable local answers belong in the idempotency cache.

    Escalations to ``frontier_or_human`` take no scheduler lease (the whole
    point of the NS-04 cache is to avoid re-leasing on replays), so skipping
    them is free, and it ensures a transient stale-KB / swap-pressure verdict
    is recomputed once the underlying condition is remediated.
    """
    if result.lane == "frontier_or_human":
        return False
    if result.reason_code in _TRANSIENT_ROUTE_REASON_CODES:
        return False
    return True


def _route_prompt_body(
    prompt: str,
    project: str,
    model: str | None,
    adapters_dir: str | None,
    *,
    service_mode: bool,
    namespace_prefix: str | None,
) -> RouteResult:
    effective_namespace = namespace_prefix or ""
    if effective_namespace and not NAMESPACE_REGISTRY.is_registered(effective_namespace):
        raise ValueError(f"UNKNOWN_NAMESPACE: {effective_namespace!r} is not a registered namespace prefix")
    # NS-04: idempotency check — must happen after namespace validation but
    # before _route_prompt_impl to avoid taking a scheduler lease on replays.
    idem_body = {"project": project, "prompt": prompt, "model": model}
    idem_key = make_compound_key(effective_namespace, "http", idem_body)
    idem_store = get_store(effective_namespace)
    cached_json = idem_store.get(idem_key)
    if cached_json is not None:
        return RouteResult.model_validate_json(cached_json)

    adapter = resolve_project_adapter(
        project,
        adapters_dir=adapters_dir,
        service_mode=service_mode,
    )
    result = RouteResult.model_validate(
        _route_prompt_impl(
            prompt=prompt,
            adapter=adapter,
            model=model,
            namespace_prefix=effective_namespace,
        )
    )
    try:
        _append_route_receipt_log(
            repo_root=pathlib.Path(adapter.project_root),
            prompt=prompt,
            result=result,
        )
    except OSError:
        # Route answers should remain available even when receipt persistence is
        # temporarily blocked by filesystem permissions or an unwritable sibling repo.
        pass
    # Only memoize durable local answers — never a transient escalation, or a
    # remediated KB/swap/selection condition would replay "go to cloud" forever.
    if _route_result_is_memoizable(result):
        idem_store.put(idem_key, result.model_dump_json(), transport="http", ttl_hours=24)
    return result


def submit_workflow(
    project: str,
    manifest_ref: ManifestRefInput,
    step_id: str,
    model: str | None = None,
    adapters_dir: str | None = None,
    *,
    wait_for_available: bool = False,
    queue_timeout_s: float | None = None,
    namespace_prefix: str | None = None,
) -> WorkflowSubmissionResult:
    """Admit an explicit validated workflow request onto the workflow lane."""
    # v4.3 ADMIT-02 + SCRIBE-01/03: recovery admission + scribe hooks.
    from ollarma.admission import check_recovery  # noqa: PLC0415
    from ollarma.scribe_hooks import pre_dispatch, end_of_run  # noqa: PLC0415
    check_recovery(entrypoint="submit_workflow")
    pre_dispatch("submit_workflow", project=project, task=step_id)
    _ok = False
    try:
        result = _submit_workflow_body(
            project=project, manifest_ref=manifest_ref, step_id=step_id,
            model=model, adapters_dir=adapters_dir,
            wait_for_available=wait_for_available,
            queue_timeout_s=queue_timeout_s,
            namespace_prefix=namespace_prefix,
        )
        _ok = True
        return result
    except BaseException as exc:
        end_of_run(
            "submit_workflow", state="blocked",
            project=project, task=step_id,
            notes=f"{type(exc).__name__}: {exc}",
            next_action="inspect logs; re-run when resolved",
        )
        raise
    finally:
        if _ok:
            end_of_run(
                "submit_workflow", state="completed",
                project=project, task=step_id,
            )


def _submit_workflow_body(
    project: str,
    manifest_ref: ManifestRefInput,
    step_id: str,
    model: str | None,
    adapters_dir: str | None,
    *,
    wait_for_available: bool,
    queue_timeout_s: float | None,
    namespace_prefix: str | None,
) -> WorkflowSubmissionResult:
    effective_namespace = namespace_prefix or ""
    if effective_namespace and not NAMESPACE_REGISTRY.is_registered(effective_namespace):
        raise ValueError(f"UNKNOWN_NAMESPACE: {effective_namespace!r} is not a registered namespace prefix")

    # NS-04: idempotency check — short-circuit before any I/O on replays.
    idem_body_wf = {
        "project": project,
        "manifest_ref": str(manifest_ref),
        "step_id": step_id,
    }
    idem_key_wf = make_compound_key(effective_namespace, "http", idem_body_wf)
    idem_store_wf = get_store(effective_namespace)
    cached_json_wf = idem_store_wf.get(idem_key_wf)
    if cached_json_wf is not None:
        return WorkflowSubmissionResult.model_validate_json(cached_json_wf)

    adapter = resolve_project_adapter(project, adapters_dir=adapters_dir, service_mode=False)
    manifest = load_workflow_manifest(
        manifest_ref,
        allowlisted_roots=[pathlib.Path(adapter.project_root)],
    )
    step = _resolve_workflow_step(manifest, step_id)
    task_class = step.task_type
    workload_class = _workflow_class_from_task_class(task_class)
    dependency_detail = _workflow_dependency_detail(workload_class)

    if dependency_detail is not None:
        snapshot = get_scheduler_snapshot()
        receipt_ref, checkpoint_ref = _record_workflow_submission(
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status="rejected",
            reason_code=ReasonCode.DEPENDENCY_MISSING.value,
        )
        receipt = build_escalation_receipt(
            project=adapter.project_name,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            task_class=task_class,
            run_id=manifest.run_id,
            stage="preflight",
            local_model=model,
            reason_code=ReasonCode.DEPENDENCY_MISSING,
            reason_detail=dependency_detail,
            resource_snapshot=snapshot.model_dump(),
            checkpoint_ref=checkpoint_ref,
        )
        return _workflow_submission_result(
            project=adapter.project_name,
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status="rejected",
            scheduler=snapshot,
            model=model,
            reason_code=ReasonCode.DEPENDENCY_MISSING.value,
            queue_depth=snapshot.queue_depth,
            escalation_receipt=receipt.model_dump(mode="json"),
            receipt_ref=receipt_ref,
            checkpoint_ref=checkpoint_ref,
            owning_lane=_route_submission(
                status="rejected",
                reason_code=ReasonCode.DEPENDENCY_MISSING.value,
                step_id=step.step_id,
            )[0],
            next_stage=_route_submission(
                status="rejected",
                reason_code=ReasonCode.DEPENDENCY_MISSING.value,
                step_id=step.step_id,
            )[1],
            handoff_required=_route_submission(
                status="rejected",
                reason_code=ReasonCode.DEPENDENCY_MISSING.value,
                step_id=step.step_id,
            )[2],
        )

    try:
        _assert_model_not_reserved(model, context="workflow")
        effective_model = model or resolve_selection(workload_class)
        _assert_model_not_reserved(effective_model, context="workflow")
    except SelectionResolutionError as exc:
        reason_code = ReasonCode(exc.reason_code)
        receipt_ref, checkpoint_ref = _record_workflow_submission(
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status="rejected",
            reason_code=exc.reason_code,
        )
        receipt = build_escalation_receipt(
            project=adapter.project_name,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            task_class=task_class,
            run_id=manifest.run_id,
            stage="preflight",
            local_model=model,
            reason_code=reason_code,
            reason_detail=exc.detail,
            resource_snapshot=get_scheduler_snapshot().model_dump(),
            checkpoint_ref=checkpoint_ref,
        )
        snapshot = get_scheduler_snapshot()
        owning_lane, next_stage, handoff_required = _route_submission(
            status="rejected",
            reason_code=exc.reason_code,
            step_id=step.step_id,
        )
        return _workflow_submission_result(
            project=adapter.project_name,
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status="rejected",
            scheduler=snapshot,
            model=model,
            reason_code=exc.reason_code,
            queue_depth=snapshot.queue_depth,
            escalation_receipt=receipt.model_dump(mode="json"),
            receipt_ref=receipt_ref,
            checkpoint_ref=checkpoint_ref,
            owning_lane=owning_lane,
            next_stage=next_stage,
            handoff_required=handoff_required,
        )

    request = JobRequest(
        project=adapter.project_name,
        lane=Lane.WORKFLOW_EXECUTION_QUEUE,
        model=effective_model,
    )
    preview = _scheduler.preview(request)
    if preview.reason_code is not None:
        reason_code = ReasonCode(preview.reason_code)
        receipt_ref, checkpoint_ref = _record_workflow_submission(
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status="rejected",
            reason_code=reason_code.value,
        )
        receipt = build_escalation_receipt(
            project=adapter.project_name,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            task_class=task_class,
            run_id=manifest.run_id,
            stage="preflight",
            local_model=effective_model,
            reason_code=reason_code,
            reason_detail=preview.reason_detail or reason_code.value,
            resource_snapshot=preview.scheduler.model_dump(),
            checkpoint_ref=checkpoint_ref,
        )
        owning_lane, next_stage, handoff_required = _route_submission(
            status="rejected",
            reason_code=reason_code.value,
            step_id=step.step_id,
        )
        return _workflow_submission_result(
            project=adapter.project_name,
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status="rejected",
            scheduler=preview.scheduler,
            model=effective_model,
            reason_code=reason_code.value,
            queue_depth=preview.queue_depth,
            escalation_receipt=receipt.model_dump(mode="json"),
            receipt_ref=receipt_ref,
            checkpoint_ref=checkpoint_ref,
            owning_lane=owning_lane,
            next_stage=next_stage,
            handoff_required=handoff_required,
        )

    if preview.status == "queued" and not wait_for_available:
        owning_lane, next_stage, handoff_required = _route_submission(
            status="queued",
            reason_code=None,
            step_id=step.step_id,
        )
        receipt_ref, checkpoint_ref = _record_workflow_submission(
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status="queued",
            reason_code=None,
        )
        return _workflow_submission_result(
            project=adapter.project_name,
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status="queued",
            scheduler=preview.scheduler,
            model=effective_model,
            reason_code=None,
            queue_depth=preview.queue_depth,
            escalation_receipt=None,
            receipt_ref=receipt_ref,
            checkpoint_ref=checkpoint_ref,
            owning_lane=owning_lane,
            next_stage=next_stage,
            handoff_required=handoff_required,
        )

    def _admit_workflow() -> WorkflowSubmissionResult:
        snapshot = _scheduler.snapshot()
        owning_lane, next_stage, handoff_required = _route_submission(
            status="accepted",
            reason_code=None,
            step_id=step.step_id,
        )
        receipt_ref, checkpoint_ref = _record_workflow_submission(
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status="accepted",
            reason_code=None,
        )
        return _workflow_submission_result(
            project=adapter.project_name,
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status="accepted",
            scheduler=snapshot,
            model=effective_model,
            reason_code=None,
            queue_depth=snapshot.queue_depth,
            escalation_receipt=None,
            receipt_ref=receipt_ref,
            checkpoint_ref=checkpoint_ref,
            owning_lane=owning_lane,
            next_stage=next_stage,
            handoff_required=handoff_required,
        )

    if not wait_for_available:
        wf_result = _admit_workflow()
        idem_store_wf.put(idem_key_wf, wf_result.model_dump_json(), transport="http", ttl_hours=24)
        return wf_result

    try:
        raw = _scheduler.submit(
            request,
            lambda _lease: _admit_workflow(),
            queue_timeout_s=queue_timeout_s,
        )
        result = WorkflowSubmissionResult.model_validate(raw)
        idem_store_wf.put(idem_key_wf, result.model_dump_json(), transport="http", ttl_hours=24)
        return result
    except SchedulerAdmissionError as exc:
        reason_code = ReasonCode(exc.reason_code)
        receipt_ref, checkpoint_ref = _record_workflow_submission(
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status="timed_out" if reason_code == ReasonCode.QUEUE_TIMEOUT else "rejected",
            reason_code=reason_code.value,
        )
        receipt = build_escalation_receipt(
            project=adapter.project_name,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            task_class=task_class,
            run_id=manifest.run_id,
            stage="preflight",
            local_model=effective_model,
            reason_code=reason_code,
            reason_detail=exc.detail,
            resource_snapshot=exc.snapshot.model_dump(),
            checkpoint_ref=checkpoint_ref,
        )
        status = "timed_out" if reason_code == ReasonCode.QUEUE_TIMEOUT else "rejected"
        owning_lane, next_stage, handoff_required = _route_submission(
            status=status,
            reason_code=reason_code.value,
            step_id=step.step_id,
        )
        return _workflow_submission_result(
            project=adapter.project_name,
            manifest=manifest,
            step=step,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE.value,
            status=status,
            scheduler=exc.snapshot,
            model=effective_model,
            reason_code=reason_code.value,
            queue_depth=exc.snapshot.queue_depth,
            escalation_receipt=receipt.model_dump(mode="json"),
            receipt_ref=receipt_ref,
            checkpoint_ref=checkpoint_ref,
            owning_lane=owning_lane,
            next_stage=next_stage,
            handoff_required=handoff_required,
        )


def generate_escalation(
    run_id: str | None = None,
    results_dir: pathlib.Path = pathlib.Path("results"),
) -> EscalationResult:
    """Generate escalation items from autopilot results.

    Raises:
        FileNotFoundError: If no autopilot results found.
    """
    # Find autopilot result files
    autopilot_files = sorted(results_dir.glob("autopilot-*.json"))
    if not autopilot_files:
        raise FileNotFoundError("No autopilot results found in results/ directory")

    if run_id:
        matching = [f for f in autopilot_files if run_id in f.name]
        if not matching:
            raise FileNotFoundError(f"No autopilot results matching run-id: {run_id}")
        latest = matching[-1]
    else:
        latest = autopilot_files[-1]

    # Load report(s)
    raw = orjson.loads(latest.read_bytes())

    # Normalize: fleet reports are lists, single-project reports are dicts
    if isinstance(raw, list):
        reports_data = raw
    else:
        reports_data = [raw]

    # Collect escalation-needed assets across all reports
    escalation_items: list[dict] = []
    for report_data in reports_data:
        project_name = report_data.get("project_name", "unknown")
        for result_data in report_data.get("results", []):
            if result_data.get("escalation_needed", False):
                escalation_items.append({
                    "asset_path": result_data["asset_path"],
                    "asset_type": result_data["asset_type"],
                    "task_tier": result_data["task_tier"],
                    "last_model_tried": result_data["model_used"],
                    "stdout": result_data.get("stdout", ""),
                    "stderr": result_data.get("stderr", ""),
                    "project_name": project_name,
                })

    return EscalationResult(
        items=escalation_items,
        count=len(escalation_items),
    )


def find_on_mac(
    query: str,
    namespace_prefix: str | None = None,
    *,
    max_results: int | None = None,
    config_path=None,
):
    """Search macfind index. Raises ValueError(MACFIND_NOT_CONFIGURED) if not configured."""
    from ollarma.macfind_config import load_macfind_config
    from ollarma.macfind_searcher import MacFindSearcher
    from ollarma.guardrail import GuardrailGate
    from ollarma.fleet import AdapterConfig
    config = load_macfind_config(config_path)
    adapter = AdapterConfig(project_name="__system__", project_root="/")
    gate = GuardrailGate(adapter)
    searcher = MacFindSearcher()
    return searcher.search(
        query,
        namespace=namespace_prefix or "__unscoped__",
        max_results=max_results or config.max_results,
        confidence_threshold=config.confidence_threshold,
        gate=gate,
    )


def reindex_macfind(*, config_path=None) -> dict:
    """Rebuild the macfind index. Raises ValueError(MACFIND_NOT_CONFIGURED) if not configured."""
    from ollarma.macfind_config import load_macfind_config
    from ollarma.macfind_indexer import MacFindIndexer
    config = load_macfind_config(config_path)
    indexer = MacFindIndexer()
    return indexer.build_index(config)


# ---------------------------------------------------------------------------
# Pipeline control service functions (Plan 33-02)
# ---------------------------------------------------------------------------


def get_pipeline_status() -> "ModelStatusSnapshot":
    """Return current model pipeline status snapshot."""
    from ollarma.pipeline_control import get_pipeline_controller, ModelStatusSnapshot  # noqa: F401
    return get_pipeline_controller().get_status()


def pipeline_warmup(model: str) -> "PipelineReceipt":
    """Warm up a model. Raises ValueError on SWAP_DEGRADED or BENCHMARK_ACTIVE."""
    _assert_model_not_reserved(model, context="pipeline warmup")
    from ollarma.pipeline_control import get_pipeline_controller, PipelineReceipt  # noqa: F401
    return get_pipeline_controller().warmup(model)


def pipeline_pin(model: str) -> "PipelineReceipt":
    """Pin a model (increment refcount). Raises ValueError on BENCHMARK_ACTIVE."""
    _assert_model_not_reserved(model, context="pipeline pin")
    from ollarma.pipeline_control import get_pipeline_controller, PipelineReceipt  # noqa: F401
    return get_pipeline_controller().pin(model)


def pipeline_evict(model: str) -> "PipelineReceipt":
    """Evict a model. Raises ValueError on PIPELINE_MODEL_PINNED or BENCHMARK_ACTIVE."""
    from ollarma.pipeline_control import get_pipeline_controller, PipelineReceipt  # noqa: F401
    _assert_model_not_reserved(model, context="pipeline evict")
    return get_pipeline_controller().evict(model)


def pipeline_drain_swap(old_model: str, new_model: str, *, timeout_s: float = 30.0) -> "PipelineReceipt":
    """Soft-drain in-flight requests then swap old_model -> new_model.

    Raises ValueError on BENCHMARK_ACTIVE or SWAP_FAILED.
    """
    _assert_model_not_reserved(old_model, context="pipeline drain")
    _assert_model_not_reserved(new_model, context="pipeline drain")
    from ollarma.pipeline_control import get_pipeline_controller  # noqa: F401
    return get_pipeline_controller().drain_and_swap(old_model, new_model, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# Embedding surface (RTB-REQ-25): pinned embed model + bounded /embed
# ---------------------------------------------------------------------------

def embed_text(text: str) -> "EmbedResult":
    """Embed text via the pinned local embed model (keep_alive=-1).

    Returns an EmbedResult; failures are LOUD degraded status, never a silent
    zero vector.
    """
    from ollarma.embeddings import embed_text as _embed_text, EmbedResult  # noqa: F401,PLC0415
    return _embed_text(text)


def embed_status() -> "EmbedStatus":
    """Report the embed model's current posture (available/resident/pinned)."""
    from ollarma.embeddings import embed_status as _embed_status, EmbedStatus  # noqa: F401,PLC0415
    return _embed_status()


def pipeline_pin_embed() -> "EmbedStatus":
    """Pin the embed model resident (keep_alive=-1) + refcount. Idempotent."""
    from ollarma.embeddings import pin_embed_model, EmbedStatus  # noqa: F401,PLC0415
    return pin_embed_model()


# ---------------------------------------------------------------------------
# GPU residency policy service functions (Phase 52 GPU-01..04, OBS-01)
# ---------------------------------------------------------------------------


def get_residency_decision() -> "ResidencyDecision":
    """Return the current residency decision using live telemetry + controller state.

    Always returns a valid ResidencyDecision — errors in telemetry probes are
    absorbed and result in a "hold" decision so the caller can proceed safely.
    """
    from ollarma.residency import decide, ResidencyDecision  # noqa: PLC0415
    from ollarma.guards import collect_runtime_telemetry, RuntimeTelemetry  # noqa: PLC0415
    from ollarma.pipeline_control import get_pipeline_controller  # noqa: PLC0415
    from ollarma.residency import RESCUE_MODEL, OPTIONAL_STRONGER  # noqa: PLC0415

    try:
        telemetry = collect_runtime_telemetry()
    except Exception:  # noqa: BLE001
        from ollarma.guards import RuntimeTelemetry  # noqa: PLC0415 – already imported above, kept for clarity
        telemetry = RuntimeTelemetry()

    controller = get_pipeline_controller()
    benchmark_active = controller._is_benchmark_active()  # noqa: SLF001

    try:
        return decide(
            telemetry=telemetry,
            controller=controller,
            benchmark_active=benchmark_active,
        )
    except Exception:  # noqa: BLE001
        # Fallback: safe hold if decide() itself raises
        from ollarma.residency import ResidencyDecision  # noqa: PLC0415 – re-import for Pylance
        return ResidencyDecision(
            state="degraded",
            rescue_target=RESCUE_MODEL,
            rescue_resident=False,
            opportunistic_target=OPTIONAL_STRONGER,
            opportunistic_resident=False,
            swap_used_mb=None,
            reason_code="DECIDE_ERROR",
            next_action="hold",
        )


def apply_residency_policy_once() -> "ResidencyApplyReceipt":
    """Decide + apply the residency policy once.

    Called from the lifespan hook at startup (best-effort; errors swallowed by caller).
    Also callable manually for testing or operator use.
    """
    from ollarma.residency import apply  # noqa: PLC0415
    from ollarma.pipeline_control import get_pipeline_controller  # noqa: PLC0415

    decision = get_residency_decision()
    controller = get_pipeline_controller()
    receipt = apply(decision, controller)

    # RTB-REQ-25: co-pin the embed model (keep_alive=-1) on the same loop that
    # manages residency, so big-model pulls cannot starve antigen-bank / calibration
    # embeddings. Best-effort and LOUD-degraded internally — never blocks residency.
    try:
        from ollarma.embeddings import pin_embed_model  # noqa: PLC0415
        pin_embed_model(controller)
    except Exception:  # noqa: BLE001 — embed pin is best-effort on this loop
        pass

    return receipt


# ---------------------------------------------------------------------------
# Type alias for public callers importing from service
# ---------------------------------------------------------------------------
try:
    from ollarma.residency import ResidencyDecision, ResidencyApplyReceipt  # noqa: F401
except ImportError:
    pass  # Module not yet created — graceful for partial installs

# Phase 53: resolve the LadderDecision forward reference used in RouteResult.ladder.
# model_rebuild() must receive the type in _types_namespace so Pydantic can
# evaluate the "LadderDecision | None" annotation in RouteResult.ladder.
try:
    from ollarma.routing_ladder import LadderDecision  # noqa: F401
    RouteResult.model_rebuild(_types_namespace={"LadderDecision": LadderDecision})
except Exception:  # noqa: BLE001
    pass  # Graceful for partial installs or circular-import edge cases


# ---------------------------------------------------------------------------
# Typed agent service function (Plan 35-02)
# ---------------------------------------------------------------------------


def run_agent(
    name: str,
    prompt: str,
    *,
    project: str | None = None,
    namespace: str | None = None,
    tool_name: str | None = None,
    tool_args: dict | None = None,
) -> "AgentReceipt":
    """Run a named typed agent and return an AgentReceipt."""
    # v4.3 ADMIT-04 + SCRIBE-01..03: recovery admission + scribe hooks.
    from ollarma.admission import check_recovery  # noqa: PLC0415
    from ollarma.scribe_hooks import pre_dispatch, end_of_run  # noqa: PLC0415
    check_recovery(entrypoint="run_agent")
    pre_dispatch("run_agent", project=project or "", task=name)
    _ok = False
    try:
        result = _run_agent_body(
            name=name, prompt=prompt, project=project, namespace=namespace,
            tool_name=tool_name, tool_args=tool_args,
        )
        _ok = True
        return result
    except BaseException as exc:
        end_of_run(
            "run_agent", state="blocked",
            project=project or "", task=name,
            notes=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        if _ok:
            end_of_run("run_agent", state="completed", project=project or "", task=name)


def _run_agent_body(
    name: str,
    prompt: str,
    *,
    project: str | None,
    namespace: str | None,
    tool_name: str | None,
    tool_args: dict | None,
) -> "AgentReceipt":
    import pathlib as _pathlib
    from ollarma.agents import AgentInput, AgentReceipt, get_agent
    from ollarma.guardrail import GuardrailGate
    from ollarma.fleet import AdapterConfig
    from ollarma.overwatch_adapter import OverwatchAdapter
    from ollarma.governance_store import GovernanceStore
    from ollarma.run_ledger import RunReceipt, append_run_receipt

    adapter = AdapterConfig(project_name="__system__", project_root="/")
    gate = GuardrailGate(adapter)
    agent = get_agent(name, gate)
    agent_input = AgentInput(
        prompt=prompt,
        project=project,
        namespace=namespace,
        tool_name=tool_name or None,
        tool_args=tool_args or {},
    )
    base_receipt = agent.run(agent_input)

    # SAFE-02/SAFE-03: OverwatchAdapter tri-state
    overwatch_state = OverwatchAdapter().attach()
    receipt_payload = base_receipt.model_dump()
    receipt_payload["overwatch_state"] = overwatch_state
    receipt = AgentReceipt(
        **receipt_payload,
    )

    # SAFE-04/SAFE-07: GovernanceStore + RunReceipt
    digest = GovernanceStore().store(receipt.model_dump())
    run_receipt = RunReceipt(
        run_id=f"agent-{name}-{receipt.run_at}",
        stage="agent_run",
        step_id=name,
        task_or_command=f"run_agent:{name}",
        lane="agent",
        status="completed",
        governance_refs=(digest,),
        namespace=namespace or "",
    )
    append_run_receipt(
        repo_root=_pathlib.Path("."),
        receipts_path=_pathlib.Path(".ollarma/agent_receipts.json"),
        receipt=run_receipt,
    )

    return receipt


# ---------------------------------------------------------------------------
# Scribe: token-loss resilience progress capture
# ---------------------------------------------------------------------------


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

    Called by Claude/ChatGPT agents during work — each call is an append-only
    write to {project_root}/.ollarma/session-log.jsonl plus a regenerated
    RESUME.md. Survives any token limit crash.
    """
    from ollarma.scribe import ScribeEntry, scribe_progress as _scribe

    entry = ScribeEntry(
        project=project,
        phase=phase,
        task=task,
        state=state,
        artifacts=artifacts or [],
        notes=notes,
        decisions=decisions or [],
        next_action=next_action,
    )
    result = _scribe(project_root=project_root, entry=entry)
    return result.model_dump()


def read_resume(project_root: str) -> str:
    """Read the current RESUME.md for a project. Returns empty string if none."""
    from ollarma.scribe import read_resume as _read_resume
    return _read_resume(project_root=project_root)


# ---------------------------------------------------------------------------
# Recovery (v4.3)
# ---------------------------------------------------------------------------

def recover_scan(
    project_root: Optional[str] = None,
    source: str = "scan",
    base_branch: str = "main",
    persist: bool = True,
) -> dict:
    """Run a recovery scan and (by default) persist a deterministic packet.

    Returns the recovery packet as a plain dict so CLI/MCP/HTTP surfaces can
    serialize it uniformly without importing the Pydantic model.
    """
    from ollarma import recovery  # noqa: PLC0415 -- lazy per repo convention

    root = pathlib.Path(project_root) if project_root else pathlib.Path.cwd()
    if persist:
        state, _ = recovery.scan_and_persist(
            root, source=source, base_branch=base_branch,
        )
    else:
        state = recovery.scan(root, source=source, base_branch=base_branch)
    return state.model_dump(mode="json")


def recover_latest(project_root: Optional[str] = None) -> Optional[dict]:
    """Return the latest persisted recovery packet as a dict, or ``None``."""
    from ollarma import recovery  # noqa: PLC0415

    root = pathlib.Path(project_root) if project_root else pathlib.Path.cwd()
    state = recovery.read_latest_packet(root)
    if state is None:
        return None
    return state.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Gateway (Phase 57-03) -- thin wrapper around gateway primitives, config-gated.
#
# The gateway is opt-in (I-01 + GATE-06). Config defaults to enabled=false; a
# missing or malformed config falls back to enabled=false (conservative per
# DEBT-10 posture). Dry-run takes precedence over the enabled gate per D-15.
#
# In Phase 57-03 the 4-cell matrix is:
#   (enabled=False, dry_run=False) -> status="disabled"    + GATEWAY_DISABLED  (D-10)
#   (enabled=False, dry_run=True ) -> status="dry_run"     + DRY_RUN           (D-15)
#   (enabled=True , dry_run=False) -> raises NotImplementedError (Phase 59)
#   (enabled=True , dry_run=True ) -> status="dry_run"     + DRY_RUN
#
# Phase 59 replaces the NotImplementedError branch with a real provider call.
# ---------------------------------------------------------------------------

import logging as _logging  # noqa: E402,PLC0415 -- gateway-only, keep local
from decimal import Decimal as _Decimal  # noqa: E402
from uuid import uuid4 as _uuid4  # noqa: E402

import pydantic as _pydantic  # noqa: E402

_GATEWAY_CONFIG_PATH = pathlib.Path(".planning/config.json")
_GATEWAY_DEFAULT_PROVIDER = "anthropic"  # placeholder; Phase 59 expands
_GATEWAY_DEFAULT_MODEL = "claude-3-5-sonnet-20241022"  # placeholder; Phase 59 expands
_GATEWAY_LOGGER = _logging.getLogger("ollarma.gateway")


def _load_gateway_enabled(config_path: pathlib.Path | None = None) -> bool:
    """Return ``features.gateway.enabled`` from config; fail-loud conservative default.

    Returns ``False`` (and logs a warning) when any of:
      - config file is missing
      - config JSON is invalid
      - ``features`` / ``features.gateway`` keys are absent
      - ``enabled`` is anything other than the exact boolean ``True``

    Never truthy-coerces ("yes"/1/"true" are rejected). DEBT-10 posture.
    """
    path = config_path if config_path is not None else _GATEWAY_CONFIG_PATH
    try:
        data = orjson.loads(path.read_bytes())
    except OSError as exc:
        # WR-04: catch the full OSError family (FileNotFoundError,
        # PermissionError, IsADirectoryError, generic I/O errors) rather than
        # FileNotFoundError alone. Fail-conservative default per DEBT-10:
        # degraded state surfaces via structured warning; never raises out.
        _GATEWAY_LOGGER.warning(
            "gateway: config unreadable at %s (%s: %s); default enabled=False",
            path, type(exc).__name__, exc,
        )
        return False
    except orjson.JSONDecodeError as exc:
        _GATEWAY_LOGGER.warning(
            "gateway: config JSON invalid (%s); default enabled=False", exc,
        )
        return False
    if not isinstance(data, dict):
        _GATEWAY_LOGGER.warning(
            "gateway: config root is not an object; default enabled=False",
        )
        return False
    features = data.get("features")
    if not isinstance(features, dict):
        _GATEWAY_LOGGER.info(
            "gateway: features key missing; default enabled=False",
        )
        return False
    gw = features.get("gateway")
    if not isinstance(gw, dict):
        _GATEWAY_LOGGER.info(
            "gateway: features.gateway missing; default enabled=False",
        )
        return False
    enabled = gw.get("enabled")
    if enabled is not True:  # strict -- no truthy coercion
        if enabled not in (None, False):
            _GATEWAY_LOGGER.warning(
                "gateway: features.gateway.enabled=%r is not boolean True; "
                "default enabled=False",
                enabled,
            )
        return False
    return True


def _synthesize_gateway_receipt(
    *,
    escalation_receipt,  # EscalationReceipt
    repo_root: pathlib.Path,
    provider: str,
    model_id: str,
    outcome: str,  # "disabled" | "dry_run"
    reason_code: str,  # GatewayReasonCode value
    reason_detail: str,
    status: str,  # "disabled" | "dry_run"
    dry_run_flag: bool,
):
    """Synthesize a FrontierReceipt (+ admission entry) for the disabled or dry_run
    postures. Writes to both split streams so GATE-05 audit invariant holds.

    Deliberately does NOT estimate tokens/cost (D-14): honest zero > plausible guess.
    Import of gateway primitives is lazy so collection-time imports stay cheap and
    the merge with 57-02 (parallel worktree) stays clean.
    """
    # Lazy imports -- per repo convention for optional/peer subsystems.
    from ollarma.evidence import canonical_hash as _canonical_hash  # noqa: PLC0415
    from ollarma.gateway import (  # noqa: PLC0415
        FrontierReceipt,
        GatewayAdmissionEntry,
        GatewayReceiptStore,
    )

    store = GatewayReceiptStore(repo_root)
    er_dump = escalation_receipt.model_dump(mode="json")
    er_hash = _canonical_hash(er_dump)
    er_id = f"er-{er_hash[:16]}"

    admission = store.append_admission(
        GatewayAdmissionEntry(
            admission_id=f"adm-{_uuid4().hex}",
            escalation_receipt_id=er_id,
            escalation_receipt_content_hash=er_hash,
            outcome=outcome,
            reason_code=reason_code,
            reason_detail=reason_detail,
            project=escalation_receipt.project,
        )
    )
    frontier = FrontierReceipt(
        escalation_receipt_id=er_id,
        admission_receipt_hash=admission.receipt_hash,
        provider=provider,
        model_id=model_id,
        prompt_tokens=0,
        response_tokens=0,
        cost_usd=_Decimal("0"),
        latency_ms=0,
        status=status,
        reason_code=reason_code,
        dry_run=dry_run_flag,
    )
    return store.append_receipt(frontier)


def _write_gateway_reject_admission(
    *,
    repo_root: pathlib.Path,
    project: str,
    reason_detail: str,
) -> None:
    """Persist an admission entry with outcome='reject' + REJECT_INVALID_RECEIPT.

    Called BEFORE raising ``GatewayInputError`` so the audit trail exists even
    when the caller's body fails pydantic validation (D-03, GATE-05).
    """
    from ollarma.gateway import (  # noqa: PLC0415
        GatewayAdmissionEntry,
        GatewayReasonCode,
        GatewayReceiptStore,
    )

    store = GatewayReceiptStore(repo_root)
    store.append_admission(
        GatewayAdmissionEntry(
            admission_id=f"adm-{_uuid4().hex}",
            escalation_receipt_id="unknown",
            escalation_receipt_content_hash="0" * 64,
            outcome="reject",
            reason_code=GatewayReasonCode.REJECT_INVALID_RECEIPT.value,
            reason_detail=reason_detail[:500],
            project=project,
        )
    )


_GATEWAY_DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-latest"


def submit_gateway_request(
    body: dict,
    *,
    dry_run_override: bool | None = None,
    repo_root: pathlib.Path | None = None,
    virtual_key_id: str | None = None,
) -> dict:
    """Phase 57-03: thin wrapper behind ``POST /gateway/submit``.

    Parses ``body["escalation_receipt"]`` into an ``EscalationReceipt``,
    synthesizes (or raises for) the appropriate FrontierReceipt per the
    4-cell matrix documented at the top of the gateway section.

    Parameters
    ----------
    body :
        The request body. Must be a dict containing an ``escalation_receipt``
        sub-object. A top-level ``dry_run: bool`` key is accepted but the
        ``dry_run_override`` parameter (set from the query string) takes
        precedence when both are present.
    dry_run_override :
        Explicit dry_run intent resolved by the HTTP handler (?dry_run=...
        query param). ``None`` means "not supplied"; ``True``/``False`` are
        honored as the operator's intent.
    repo_root :
        Repository root for the gateway receipt store. Defaults to cwd
        (matches ``recover_scan``). Tests pass ``tmp_path`` for isolation.

    Returns
    -------
    dict
        JSON-dumped FrontierReceipt.

    Raises
    ------
    GatewayInputError
        Body missing ``escalation_receipt`` key or payload fails pydantic
        validation. A reject admission entry is persisted before the
        exception propagates (GATE-05 + D-03).
    NotImplementedError
        Feature enabled + dry_run not requested. Phase 59 replaces with a
        real provider call; Phase 57 keeps the boundary loud (no silent stub).
    """
    # Lazy imports so test-time collection doesn't pull the whole gateway
    # subsystem when the tests don't exercise it, and so the Phase 57-02
    # merge (parallel worktree) stays conflict-free.
    from ollarma.escalation import EscalationReceipt  # noqa: PLC0415
    from ollarma.gateway import GatewayReasonCode  # noqa: PLC0415

    root = repo_root if repo_root is not None else pathlib.Path.cwd()

    if not isinstance(body, dict):
        # Body wasn't even a JSON object. Persist a reject without a project
        # (we have nothing to key on) and raise -- HTTP handler maps to 400.
        _write_gateway_reject_admission(
            repo_root=root,
            project="unknown",
            reason_detail="request body is not a JSON object",
        )
        raise GatewayInputError("request body is not a JSON object")

    # Resolve dry_run precedence: explicit override (?dry_run=) wins; else
    # body.dry_run if it's a bool; else None (not requested).
    if dry_run_override is None:
        body_dry_run = body.get("dry_run")
        if isinstance(body_dry_run, bool):
            dry_run_override = body_dry_run

    er_payload = body.get("escalation_receipt")
    if er_payload is None:
        project_hint = "unknown"
        _write_gateway_reject_admission(
            repo_root=root,
            project=project_hint,
            reason_detail="request body missing 'escalation_receipt' key",
        )
        raise GatewayInputError(
            "request body missing 'escalation_receipt' key"
        )

    try:
        escalation_receipt = EscalationReceipt.model_validate(er_payload)
    except _pydantic.ValidationError as exc:
        project_hint = "unknown"
        if isinstance(er_payload, dict):
            project_field = er_payload.get("project")
            if isinstance(project_field, str) and project_field:
                project_hint = project_field
        # Take a compact summary of the pydantic errors for the admission trail.
        first_err = exc.errors()[0] if exc.errors() else {}
        summary = first_err.get("msg", "validation failed")
        _write_gateway_reject_admission(
            repo_root=root,
            project=project_hint,
            reason_detail=(
                f"escalation_receipt payload failed pydantic validation: {summary}"
            ),
        )
        raise GatewayInputError(
            f"escalation_receipt validation failed: {exc.errors()}"
        ) from exc

    # WR-05: anchor config read at repo_root (same anchor as the receipt
    # store). Previously this called _load_gateway_enabled() with no args,
    # which resolved _GATEWAY_CONFIG_PATH relative to CWD -- an operator
    # running from a subdirectory (or a test passing repo_root != cwd) would
    # silently get enabled=False even with a truthy config. Fail-conservative
    # direction but surprising; fix by threading the root through.
    enabled = _load_gateway_enabled(root / ".planning" / "config.json")

    # Dry-run explicitly requested -> synthesize dry_run receipt regardless of
    # enabled flag (D-15). Phase 58-02: dry-run still exercises the admission
    # path as an observability concern — counts against the req/min cap so
    # operators see the same rate-limit behavior with or without dry_run.
    if dry_run_override is True:
        materialized = _synthesize_gateway_receipt(
            escalation_receipt=escalation_receipt,
            repo_root=root,
            provider=_GATEWAY_DEFAULT_PROVIDER,
            model_id=_GATEWAY_DEFAULT_MODEL,
            outcome="dry_run",
            reason_code=GatewayReasonCode.DRY_RUN.value,
            reason_detail="dry_run=true; no provider call executed",
            status="dry_run",
            dry_run_flag=True,
        )
        # Best-effort rate-cap record on dry-run: only when a gateway config
        # exists (no-op otherwise). Failure to persist state must not break
        # dry-run's 200-status contract (D-15) -- narrow OSError catch.
        try:
            from ollarma.gateway_admission import (  # noqa: PLC0415
                RateCapEnforcer,
                _load_gateway_config,
            )
            _gateway_config_for_dry_run = _load_gateway_config(
                root / ".planning" / "config.json"
            )
            if _gateway_config_for_dry_run:
                _dry_run_enforcer = RateCapEnforcer(
                    _gateway_config_for_dry_run,
                    root / ".ollarma" / "gateway" / "rate_state.json",
                )
                _dry_run_enforcer.record(
                    escalation_receipt.project, actual_tokens=0
                )
        except OSError:
            pass
        return materialized.model_dump(mode="json")

    # Feature disabled (the ship default) -> synthesize disabled receipt (D-10).
    if not enabled:
        materialized = _synthesize_gateway_receipt(
            escalation_receipt=escalation_receipt,
            repo_root=root,
            provider=_GATEWAY_DEFAULT_PROVIDER,
            model_id=_GATEWAY_DEFAULT_MODEL,
            outcome="disabled",
            reason_code=GatewayReasonCode.GATEWAY_DISABLED.value,
            reason_detail="gateway.enabled=false; no provider call executed",
            status="disabled",
            dry_run_flag=False,
        )
        return materialized.model_dump(mode="json")

    # Enabled + no dry-run requested -> Phase 58-01 admission pipeline runs
    # BEFORE the Phase 59 boundary. Allowlist + virtual-key registry + Keychain
    # reachability are enforced here; any rejection is surfaced as a
    # FrontierReceipt with status="failed" + the admission reason_code, and
    # the HTTP layer maps the reason_code to the appropriate status code
    # (403 / 400 per D-58-05).
    from ollarma.gateway_admission import (  # noqa: PLC0415
        AdmissionPolicy,
        RateCapEnforcer,
        _load_gateway_config,
    )

    gateway_config = _load_gateway_config(root / ".planning" / "config.json")
    policy = AdmissionPolicy(gateway_config)
    rate_state_path = root / ".ollarma" / "gateway" / "rate_state.json"
    enforcer = RateCapEnforcer(gateway_config, rate_state_path)
    policy.attach_rate_cap_enforcer(enforcer)
    decision = policy.precheck(
        escalation_receipt.project, virtual_key_id, estimated_tokens=0
    )
    if not decision.approved and decision.reason_code is not None:
        materialized = _synthesize_gateway_reject(
            escalation_receipt=escalation_receipt,
            repo_root=root,
            reason_code=decision.reason_code.value,
            reason_detail=decision.reason_detail,
        )
        payload = materialized.model_dump(mode="json")
        # Surface retry_after_seconds (non-receipt field) for HTTP 429 header
        # mapping. Not persisted on FrontierReceipt (frozen model); computed
        # by the enforcer per-rejection.
        if decision.retry_after_seconds is not None:
            payload["retry_after_seconds"] = int(decision.retry_after_seconds)
        return payload

    # Phase 59: admission approved -> dispatch to the provider adapter.
    # Extract prompt + model from the request body. Prompt is REQUIRED for
    # non-dry-run submissions (D-59-02 payload contract). Model defaults to
    # the Anthropic haiku model when the vk resolves to the anthropic provider.
    prompt_raw = body.get("prompt")
    if not isinstance(prompt_raw, str) or not prompt_raw:
        # Record no tokens -- request never reached the provider.
        enforcer.record(escalation_receipt.project, actual_tokens=0)
        raise GatewayInputError(
            "non-dry-run gateway submission requires a non-empty 'prompt' field"
        )

    # Resolve vk registry entry -> provider name + keychain_service.
    vk_entry = None
    for candidate in gateway_config.get("virtual_keys", []):
        if isinstance(candidate, dict) and candidate.get("id") == virtual_key_id:
            vk_entry = candidate
            break
    # Admission already validated this, but defend against a race where the
    # config changed between precheck and dispatch.
    if vk_entry is None:
        enforcer.record(escalation_receipt.project, actual_tokens=0)
        materialized = _synthesize_gateway_reject(
            escalation_receipt=escalation_receipt,
            repo_root=root,
            reason_code=GatewayReasonCode.VIRTUAL_KEY_UNKNOWN.value,
            reason_detail=(
                f"virtual_key_id {virtual_key_id!r} vanished from registry "
                "between precheck and dispatch"
            ),
        )
        return materialized.model_dump(mode="json")

    provider_name = vk_entry.get("provider")
    if not isinstance(provider_name, str) or not provider_name:
        enforcer.record(escalation_receipt.project, actual_tokens=0)
        materialized = _synthesize_gateway_reject(
            escalation_receipt=escalation_receipt,
            repo_root=root,
            reason_code=GatewayReasonCode.VIRTUAL_KEY_UNKNOWN.value,
            reason_detail=(
                f"virtual_key_id {virtual_key_id!r} registry entry has no "
                "'provider' field"
            ),
        )
        return materialized.model_dump(mode="json")

    keychain_service = vk_entry.get("keychain_service")
    if not isinstance(keychain_service, str) or not keychain_service:
        enforcer.record(escalation_receipt.project, actual_tokens=0)
        materialized = _synthesize_gateway_reject(
            escalation_receipt=escalation_receipt,
            repo_root=root,
            reason_code=GatewayReasonCode.VIRTUAL_KEY_KEYCHAIN_MISS.value,
            reason_detail=(
                f"virtual_key_id {virtual_key_id!r} registry entry has no "
                "'keychain_service' field"
            ),
        )
        return materialized.model_dump(mode="json")

    # Resolve bytes. Env-var opt-in: keychain_service="env:VAR_NAME" reads
    # from os.environ (D-59-03 env override).
    from ollarma.gateway_admission import resolve_virtual_key  # noqa: PLC0415

    if keychain_service.startswith("env:"):
        import os as _os  # noqa: PLC0415
        env_var_name = keychain_service[len("env:"):]
        raw_env = _os.environ.get(env_var_name)
        key_bytes = raw_env.encode("utf-8") if raw_env else None
    else:
        key_bytes = resolve_virtual_key(keychain_service)

    if key_bytes is None:
        enforcer.record(escalation_receipt.project, actual_tokens=0)
        materialized = _synthesize_gateway_reject(
            escalation_receipt=escalation_receipt,
            repo_root=root,
            reason_code=GatewayReasonCode.VIRTUAL_KEY_KEYCHAIN_MISS.value,
            reason_detail=(
                f"virtual_key_id {virtual_key_id!r} Keychain lookup returned None"
            ),
        )
        return materialized.model_dump(mode="json")

    # Resolve provider adapter. Unknown provider -> PROVIDER_AUTH_FAILED
    # receipt (operator-config error).
    from ollarma.providers import get_provider  # noqa: PLC0415

    try:
        adapter = get_provider(provider_name)
    except KeyError as exc:
        # Clear the key reference explicitly before returning.
        del key_bytes
        enforcer.record(escalation_receipt.project, actual_tokens=0)
        materialized = _synthesize_gateway_reject(
            escalation_receipt=escalation_receipt,
            repo_root=root,
            reason_code=GatewayReasonCode.PROVIDER_AUTH_FAILED.value,
            reason_detail=f"unknown provider {provider_name!r}: {exc}",
        )
        return materialized.model_dump(mode="json")

    # Model selection. Body's ``model`` wins; otherwise pick a per-provider
    # default.
    model_raw = body.get("model")
    if isinstance(model_raw, str) and model_raw:
        model = model_raw
    elif provider_name == "anthropic":
        model = _GATEWAY_DEFAULT_ANTHROPIC_MODEL
    else:
        model = _GATEWAY_DEFAULT_MODEL

    from ollarma.gateway_client import GatewayClient  # noqa: PLC0415

    client = GatewayClient(root)
    frontier = client.submit(
        escalation_receipt,
        dry_run=False,
        provider=provider_name,
        model_id=model,
        prompt=prompt_raw,
        provider_adapter=adapter,
        virtual_key_bytes=key_bytes,
    )
    # Defensive: clear local reference to the key bytes.
    del key_bytes

    # Record actual tokens now that we have them from the provider.
    enforcer.record(
        escalation_receipt.project,
        actual_tokens=int(frontier.prompt_tokens) + int(frontier.response_tokens),
    )
    return frontier.model_dump(mode="json")


def _synthesize_gateway_reject(
    *,
    escalation_receipt,  # EscalationReceipt
    repo_root: pathlib.Path,
    reason_code: str,  # GatewayReasonCode value
    reason_detail: str,
):
    """Write admission + FrontierReceipt for a Phase 58-01 admission reject.

    Mirrors ``_synthesize_gateway_receipt`` but:
      - admission outcome is ``"reject"`` (D-03 audit trail)
      - FrontierReceipt status is ``"failed"`` with empty provider/model
        (no routing decision was reached because admission stopped the call).

    The materialized receipt carries the specific reason_code so the HTTP
    layer can map it to the per-D-58-05 HTTP status code without re-parsing.
    """
    from ollarma.evidence import canonical_hash as _canonical_hash  # noqa: PLC0415
    from ollarma.gateway import (  # noqa: PLC0415
        FrontierReceipt,
        GatewayAdmissionEntry,
        GatewayReceiptStore,
    )

    store = GatewayReceiptStore(repo_root)
    er_dump = escalation_receipt.model_dump(mode="json")
    er_hash = _canonical_hash(er_dump)
    er_id = f"er-{er_hash[:16]}"

    admission = store.append_admission(
        GatewayAdmissionEntry(
            admission_id=f"adm-{_uuid4().hex}",
            escalation_receipt_id=er_id,
            escalation_receipt_content_hash=er_hash,
            outcome="reject",
            reason_code=reason_code,
            reason_detail=reason_detail[:500],
            project=escalation_receipt.project,
        )
    )
    frontier = FrontierReceipt(
        escalation_receipt_id=er_id,
        admission_receipt_hash=admission.receipt_hash,
        provider="",
        model_id="",
        prompt_tokens=0,
        response_tokens=0,
        cost_usd=_Decimal("0"),
        latency_ms=0,
        status="failed",
        reason_code=reason_code,
        dry_run=False,
    )
    return store.append_receipt(frontier)
