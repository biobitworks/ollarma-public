"""dashboard.py -- Typed read models for the local ollarma operator dashboard."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from ollarma.scheduler import RuntimeSnapshot


class DashboardReceiptPreview(BaseModel):
    """Redacted receipt preview for dashboard drill-downs."""

    model_config = ConfigDict(frozen=True)

    stage: str
    step_id: str
    lane: str
    status: str
    retry_count: int = 0
    reason_code: str | None = None
    created_at: str
    checkpoint_ref: dict[str, str] | None = None


class DashboardCheckpointSummary(BaseModel):
    """Restart-safe checkpoint summary for one run."""

    model_config = ConfigDict(frozen=True)

    current_stage: str
    last_validated_stage: str | None = None
    retry_budget_remaining: int
    resume_from_step: str | None = None
    last_receipt_hash: str


class DashboardRunSummary(BaseModel):
    """Compact operator-facing run summary."""

    model_config = ConfigDict(frozen=True)

    project: str
    run_id: str
    run_kind: str
    current_stage: str | None = None
    last_validated_stage: str | None = None
    status: str | None = None
    reason_code: str | None = None
    step_id: str | None = None
    updated_at: str | None = None
    receipt_count: int = 0
    receipt_ref: dict[str, str] | None = None
    checkpoint_ref: dict[str, str] | None = None


class DashboardRunDetail(BaseModel):
    """Detailed operator-facing view for one workflow or autopilot run."""

    model_config = ConfigDict(frozen=True)

    project: str
    run_id: str
    run_kind: str
    current_stage: str | None = None
    last_validated_stage: str | None = None
    status: str | None = None
    reason_code: str | None = None
    step_id: str | None = None
    updated_at: str | None = None
    receipt_count: int = 0
    receipt_ref: dict[str, str] | None = None
    checkpoint_ref: dict[str, str] | None = None
    checkpoint: DashboardCheckpointSummary | None = None
    receipts: tuple[DashboardReceiptPreview, ...] = ()


class DashboardBoundaryInfo(BaseModel):
    """Operator-readable boundary and future integration contract."""

    model_config = ConfigDict(frozen=True)

    dashboard_owner: str
    read_only: bool
    interactive: bool = False
    dashboard_path: str
    overview_path: str
    run_detail_path_template: str
    review_gate: str
    integration_contract: str
    portfolio_dashboard_dependency: bool
    helper_surfaces: tuple[str, ...]
    deterministic_execution_surfaces: tuple[str, ...]
    safe_for: tuple[str, ...]
    unsafe_for: tuple[str, ...]
    fallback_policy: dict[str, str]


class DashboardKBStatus(BaseModel):
    """Per-project KB status summary for the operator dashboard."""

    model_config = ConfigDict(frozen=True)

    project: str
    status: str
    reason_code: str | None = None
    source_count: int = 0
    document_count: int = 0
    chunk_count: int = 0
    freshness_hours: int
    stale_behavior: str
    built_at: str | None = None
    artifact_root: str
    search_db_path: str


class DashboardRouteReceipt(BaseModel):
    """Recent retrieval-first route receipt summary."""

    model_config = ConfigDict(frozen=True)

    project: str
    lane: str
    reason_code: str
    query_class: str
    kb_status: str | None = None
    evidence_count: int = 0
    next_action: str
    selected_model: str | None = None
    prompt_preview: str | None = None
    created_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


class DashboardCitation(BaseModel):
    """One operator-facing citation/path reference."""

    model_config = ConfigDict(frozen=True)

    label: str
    repo_relative: str


class DashboardCommandHint(BaseModel):
    """One concrete command/operators hint."""

    model_config = ConfigDict(frozen=True)

    label: str
    command: str
    purpose: str


class DashboardWorkflowStep(BaseModel):
    """One runnable workflow step shown in the dashboard."""

    model_config = ConfigDict(frozen=True)

    step_id: str
    stage: str
    task_type: str


class DashboardWorkflowManifest(BaseModel):
    """One discovered workflow manifest plus portable metadata."""

    model_config = ConfigDict(frozen=True)

    manifest_ref: dict[str, str]
    manifest_digest: str
    run_id: str
    consumer_repo: str | None = None
    steps: tuple[DashboardWorkflowStep, ...] = ()


class DashboardWorkflowCatalog(BaseModel):
    """Discovered runnable workflow modules for one project."""

    model_config = ConfigDict(frozen=True)

    project: str
    manifests: tuple[DashboardWorkflowManifest, ...] = ()


class DashboardModelOption(BaseModel):
    """One dashboard model-picker option."""

    model_config = ConfigDict(frozen=True)

    name: str
    label: str
    description: str | None = None
    installed: bool = False
    source: str


class DashboardResource(BaseModel):
    """Curated operator KB resource for the dashboard."""

    model_config = ConfigDict(frozen=True)

    slug: str
    title: str
    category: str
    summary: str
    why_it_matters: str
    suggested_questions: tuple[str, ...] = ()
    citations: tuple[DashboardCitation, ...] = ()
    commands: tuple[DashboardCommandHint, ...] = ()


class DashboardReadinessItem(BaseModel):
    """One operator-facing readiness or missing-setup signal."""

    model_config = ConfigDict(frozen=True)

    severity: str
    title: str
    detail: str
    action_label: str
    action_command: str | None = None


class DashboardGatewayPosture(BaseModel):
    """Gateway posture mirror for the dashboard layer (Plan 63-01).

    Mirrors the ``GatewayPosture`` model owned by ``ollarma.service`` but lives
    here so ``dashboard.py`` stays free of a circular import back to service.
    ``virtual_keys_configured`` is a COUNT only — vk identifier strings never
    cross into the dashboard payload, consistent with the 57.1-02 readiness
    redaction rule (D-57.1-02).
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    allowlist_size: int = 0
    virtual_keys_configured: int = 0
    rate_cap_state: str = "not_configured"
    admissions_today: int = 0
    receipts_today: int = 0


class DashboardGatewayReceipt(BaseModel):
    """Redaction-safe summary of one ``FrontierReceipt`` for the dashboard.

    Only fields safe to render to the operator panel are surfaced. No raw key
    bytes, no ``admission_receipt_hash``/``parent_hash``/``receipt_hash`` chain
    fields, no provider request IDs. Virtual-key identifiers are intentionally
    absent — FrontierReceipt does not carry a vk ID field, so this model has
    nothing to redact beyond omission of the chain fields (Plan 63-01, D-57.1-02).
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model_id: str
    status: str
    reason_code: str | None = None
    cost_usd: str = "0"
    latency_ms: int = 0
    prompt_tokens: int = 0
    response_tokens: int = 0
    created_at: str


class DashboardGatewayAdmission(BaseModel):
    """Redaction-safe summary of one ``GatewayAdmissionEntry`` for the dashboard.

    Surfaces outcome + reason + project only. Admission/escalation receipt IDs,
    content hashes, and chain hashes are intentionally omitted so the rendered
    panel never leaks receipt-ID strings or vk identifiers (Plan 63-01).
    """

    model_config = ConfigDict(frozen=True)

    outcome: str
    reason_code: str | None = None
    project: str
    created_at: str


class DashboardOverview(BaseModel):
    """Top-level dashboard overview payload."""

    model_config = ConfigDict(frozen=True)

    generated_at: str
    scheduler: RuntimeSnapshot
    project_count: int
    projects: tuple[str, ...] = ()
    kb_status: tuple[DashboardKBStatus, ...] = ()
    model_options: tuple[DashboardModelOption, ...] = ()
    readiness: tuple[DashboardReadinessItem, ...] = ()
    operator_resources: tuple[DashboardResource, ...] = ()
    recent_route_receipts: tuple[DashboardRouteReceipt, ...] = ()
    workflow_runs: tuple[DashboardRunSummary, ...] = ()
    autopilot_runs: tuple[DashboardRunSummary, ...] = ()
    # Plan 63-01 (OBS-58): optional gateway panel payload. Default None so that
    # pre-63 callers and gateway-disabled deployments keep serializing cleanly.
    gateway: DashboardGatewayPosture | None = None
    recent_gateway_receipts: tuple[DashboardGatewayReceipt, ...] = ()
    recent_gateway_admissions: tuple[DashboardGatewayAdmission, ...] = ()
    boundary: DashboardBoundaryInfo
