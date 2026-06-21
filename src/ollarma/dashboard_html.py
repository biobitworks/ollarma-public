"""dashboard_html.py -- Server-rendered HTML surface for the ollarma dashboard."""
from __future__ import annotations

from html import escape

import orjson

from ollarma.dashboard import (
    DashboardGatewayAdmission,
    DashboardGatewayPosture,
    DashboardGatewayReceipt,
    DashboardKBStatus,
    DashboardModelOption,
    DashboardOverview,
    DashboardReadinessItem,
    DashboardResource,
    DashboardRouteReceipt,
    DashboardRunSummary,
)


def _render_run_rows(runs: tuple[DashboardRunSummary, ...], empty_label: str) -> str:
    if not runs:
        return f"<tr><td colspan='7' class='empty'>{escape(empty_label)}</td></tr>"

    rows: list[str] = []
    for run in runs:
        run_link = f"/dashboard/runs/{escape(run.run_id)}"
        rows.append(
            "<tr>"
            f"<td>{escape(run.project)}</td>"
            f"<td><a href='{run_link}'>{escape(run.run_id)}</a></td>"
            f"<td>{escape(run.current_stage or '-')}</td>"
            f"<td>{escape(run.status or '-')}</td>"
            f"<td>{escape(run.reason_code or '-')}</td>"
            f"<td>{run.receipt_count}</td>"
            f"<td>{escape(run.updated_at or '-')}</td>"
            "</tr>"
        )
    return "".join(rows)


def _render_kb_rows(items: tuple[DashboardKBStatus, ...]) -> str:
    if not items:
        return "<tr><td colspan='8' class='empty'>No KB status available for registered projects.</td></tr>"

    rows: list[str] = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{escape(item.project)}</td>"
            f"<td>{escape(item.status)}</td>"
            f"<td>{escape(item.reason_code or '-')}</td>"
            f"<td>{item.source_count}</td>"
            f"<td>{item.document_count}</td>"
            f"<td>{item.chunk_count}</td>"
            f"<td>{escape(item.built_at or '-')}</td>"
            f"<td><code>{escape(item.search_db_path)}</code></td>"
            "</tr>"
        )
    return "".join(rows)


def _render_model_options(items: tuple[DashboardModelOption, ...]) -> str:
    options = ["<option value=''>Automatic (recommended)</option>"]
    for item in items:
        title = escape(item.description or item.label)
        options.append(
            f"<option value='{escape(item.name)}' title='{title}'>{escape(item.label)}</option>"
        )
    return "".join(options)


def _render_route_receipts(items: tuple[DashboardRouteReceipt, ...]) -> str:
    if not items:
        return "<tr><td colspan='7' class='empty'>No recent route receipts recorded.</td></tr>"

    rows: list[str] = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{escape(item.project)}</td>"
            f"<td>{escape(item.lane)}</td>"
            f"<td>{escape(item.reason_code)}</td>"
            f"<td>{escape(item.query_class)}</td>"
            f"<td>{item.evidence_count}</td>"
            f"<td>{escape(item.prompt_preview or '-')}</td>"
            f"<td>{escape(item.created_at)}</td>"
            "</tr>"
        )
    return "".join(rows)


def _render_gateway_panel(
    posture: DashboardGatewayPosture | None,
    recent_receipts: tuple[DashboardGatewayReceipt, ...],
    recent_admissions: tuple[DashboardGatewayAdmission, ...],
) -> str:
    """Render the Plan 63-01 gateway observability panel (OBS-58).

    Server-rendered HTML only (no JavaScript). ``html.escape`` guards every
    user-provided string. Virtual-key identifiers never appear — the posture
    carries a COUNT only (``virtual_keys_configured``) and the receipt/admission
    summaries surface only redaction-safe fields.
    """
    if posture is None:
        return (
            "<section id=\"gateway-panel\" class=\"panel\" style=\"margin-bottom:18px\">"
            "<div class=\"eyebrow\">gateway</div>"
            "<h2>Gateway: Disabled</h2>"
            "<p>Gateway posture is not available for this deployment.</p>"
            "</section>"
        )

    enabled_label = "Enabled" if posture.enabled else "Disabled"
    enabled_pill = "pill-ok" if posture.enabled else "pill-info"

    admissions_today = posture.admissions_today
    receipts_today = posture.receipts_today
    # Rejection rate: share of admission decisions whose outcome is "reject".
    # Computed from the rolling tail of admissions (not a daily rollup) — this
    # is the signal operators actually want: "of the last N decisions, how many
    # were rejected". Zero denominator shows "n/a" cleanly.
    rejection_count = sum(1 for entry in recent_admissions if entry.outcome == "reject")
    denominator = len(recent_admissions)
    if denominator == 0:
        rejection_rate_label = "n/a"
    else:
        rate_pct = (rejection_count * 100.0) / denominator
        rejection_rate_label = f"{rate_pct:.1f}%"

    # Last FrontierReceipt summary (most recent tail entry, if any).
    if recent_receipts:
        last_receipt = recent_receipts[-1]
        last_receipt_block = (
            "<div class=\"readiness-item\">"
            f"<div class=\"pill pill-info\">last receipt</div>"
            f"<h3>{escape(last_receipt.model_id or '-')}</h3>"
            f"<p>status: <strong>{escape(last_receipt.status or '-')}</strong>"
            f" · reason: {escape(last_receipt.reason_code or '-')}"
            f" · cost_usd: {escape(last_receipt.cost_usd)}"
            f" · latency_ms: {last_receipt.latency_ms}</p>"
            f"<small>provider: {escape(last_receipt.provider or '-')}"
            f" · created: {escape(last_receipt.created_at or '-')}</small>"
            "</div>"
        )
    else:
        last_receipt_block = (
            "<div class=\"readiness-item\">"
            "<div class=\"pill pill-info\">last receipt</div>"
            "<h3>No recent activity</h3>"
            "<p>No FrontierReceipts have been recorded yet.</p>"
            "</div>"
        )

    # Last admission summary (most recent tail entry, if any).
    if recent_admissions:
        last_admission = recent_admissions[-1]
        last_admission_block = (
            "<div class=\"readiness-item\">"
            f"<div class=\"pill pill-info\">last admission</div>"
            f"<h3>outcome: {escape(last_admission.outcome or '-')}</h3>"
            f"<p>reason: {escape(last_admission.reason_code or '-')}"
            f" · project: {escape(last_admission.project or '-')}</p>"
            f"<small>created: {escape(last_admission.created_at or '-')}</small>"
            "</div>"
        )
    else:
        last_admission_block = (
            "<div class=\"readiness-item\">"
            "<div class=\"pill pill-info\">last admission</div>"
            "<h3>No recent activity</h3>"
            "<p>No gateway admission decisions have been recorded yet.</p>"
            "</div>"
        )

    return (
        "<section id=\"gateway-panel\" class=\"panel\" style=\"margin-bottom:18px\">"
        "<div class=\"eyebrow\">frontier gateway</div>"
        f"<h2>Gateway: <span class=\"pill {enabled_pill}\">{escape(enabled_label)}</span></h2>"
        "<p>Cross-project frontier-call substrate. Counts are operator-facing;"
        " virtual-key identifiers are intentionally never rendered.</p>"
        "<div class=\"stats\">"
        f"<div class=\"stat\"><span class=\"eyebrow\">admissions today</span><strong>{admissions_today}</strong></div>"
        f"<div class=\"stat\"><span class=\"eyebrow\">receipts today</span><strong>{receipts_today}</strong></div>"
        f"<div class=\"stat\"><span class=\"eyebrow\">rejection rate</span><strong>{escape(rejection_rate_label)}</strong></div>"
        f"<div class=\"stat\"><span class=\"eyebrow\">rate cap state</span><strong style=\"font-size:18px\">{escape(posture.rate_cap_state)}</strong></div>"
        "</div>"
        "<div class=\"stats\">"
        f"<div class=\"stat\"><span class=\"eyebrow\">allowlist size</span><strong>{posture.allowlist_size}</strong></div>"
        f"<div class=\"stat\"><span class=\"eyebrow\">virtual keys configured</span><strong>{posture.virtual_keys_configured}</strong></div>"
        "</div>"
        "<div class=\"grid\" style=\"margin-top:14px\">"
        f"{last_receipt_block}"
        f"{last_admission_block}"
        "</div>"
        "</section>"
    )


def _render_readiness(items: tuple[DashboardReadinessItem, ...]) -> str:
    if not items:
        return "<div class='empty-note'>No readiness notes.</div>"

    blocks: list[str] = []
    for item in items:
        command = (
            f"<code>{escape(item.action_command)}</code>"
            if item.action_command
            else ""
        )
        blocks.append(
            "<article class='readiness-item'>"
            f"<div class='pill pill-{escape(item.severity)}'>{escape(item.severity)}</div>"
            f"<h3>{escape(item.title)}</h3>"
            f"<p>{escape(item.detail)}</p>"
            f"<p class='readiness-action'><strong>{escape(item.action_label)}</strong></p>"
            f"{command}"
            "</article>"
        )
    return "".join(blocks)


def _render_resource_cards(items: tuple[DashboardResource, ...]) -> str:
    if not items:
        return "<div class='empty-note'>No operator resources are configured.</div>"

    cards: list[str] = []
    for item in items:
        search_blob = " ".join(
            [
                item.title,
                item.category,
                item.summary,
                item.why_it_matters,
                *item.suggested_questions,
                *(citation.repo_relative for citation in item.citations),
                *(command.command for command in item.commands),
            ]
        ).lower()
        questions = "".join(
            f"<button type='button' class='ghost-button suggested-question' data-question='{escape(question)}'>{escape(question)}</button>"
            for question in item.suggested_questions
        )
        commands = "".join(
            "<li>"
            f"<strong>{escape(command.label)}</strong>"
            f"<code>{escape(command.command)}</code>"
            f"<span>{escape(command.purpose)}</span>"
            "</li>"
            for command in item.commands
        )
        citations = "".join(
            "<li>"
            f"<span>{escape(citation.label)}</span>"
            f"<code>{escape(citation.repo_relative)}</code>"
            "</li>"
            for citation in item.citations
        )
        cards.append(
            f"<article class='resource-card' data-search='{escape(search_blob)}'>"
            f"<div class='eyebrow'>{escape(item.category)}</div>"
            f"<h3>{escape(item.title)}</h3>"
            f"<p>{escape(item.summary)}</p>"
            f"<p class='resource-why'>{escape(item.why_it_matters)}</p>"
            "<div class='resource-group'>"
            "<h4>Suggested Questions</h4>"
            f"<div class='question-list'>{questions or '<span class=\"empty-note\">No suggested questions.</span>'}</div>"
            "</div>"
            "<div class='resource-group'>"
            "<h4>Commands</h4>"
            f"<ul class='command-list'>{commands or '<li class=\"empty-note\">No command hints.</li>'}</ul>"
            "</div>"
            "<div class='resource-group'>"
            "<h4>Citations</h4>"
            f"<ul class='citation-list'>{citations or '<li class=\"empty-note\">No citations.</li>'}</ul>"
            "</div>"
            "</article>"
        )
    return "".join(cards)


def _json_for_script(payload: dict) -> str:
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS).decode("utf-8").replace("</", "<\\/")


def render_dashboard(overview: DashboardOverview) -> str:
    """Render the interactive operator dashboard."""
    helper_surfaces = "".join(
        f"<li>{escape(surface)}</li>" for surface in overview.boundary.helper_surfaces
    )
    execution_surfaces = "".join(
        f"<li>{escape(surface)}</li>"
        for surface in overview.boundary.deterministic_execution_surfaces
    )
    safe_for = "".join(f"<li>{escape(item)}</li>" for item in overview.boundary.safe_for)
    unsafe_for = "".join(f"<li>{escape(item)}</li>" for item in overview.boundary.unsafe_for)
    project_options = "".join(
        f"<option value='{escape(project)}'>{escape(project)}</option>"
        for project in overview.projects
    )

    workflow_rows = _render_run_rows(
        overview.workflow_runs,
        "No workflow runs discovered in registered project execution roots.",
    )
    autopilot_rows = _render_run_rows(
        overview.autopilot_runs,
        "No autopilot runs discovered in registered project execution roots.",
    )
    kb_rows = _render_kb_rows(overview.kb_status)
    model_options = _render_model_options(overview.model_options)
    route_receipt_rows = _render_route_receipts(overview.recent_route_receipts)
    readiness_blocks = _render_readiness(overview.readiness)
    gateway_panel_html = _render_gateway_panel(
        overview.gateway,
        overview.recent_gateway_receipts,
        overview.recent_gateway_admissions,
    )
    resource_cards = _render_resource_cards(overview.operator_resources)
    dashboard_data = _json_for_script(
        {
            "projects": overview.projects,
            "interactive": overview.boundary.interactive,
            "readOnly": overview.boundary.read_only,
            "modelCount": len(overview.model_options),
        }
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>ollarma Dashboard</title>
  <style>
    :root {{
      --bg: #f3efe4;
      --panel: rgba(255, 251, 243, 0.94);
      --panel-strong: rgba(255, 251, 243, 0.98);
      --ink: #1d2b22;
      --muted: #55635a;
      --line: rgba(29, 43, 34, 0.12);
      --accent: #b74b2a;
      --accent-soft: rgba(183, 75, 42, 0.12);
      --ok: #275c45;
      --warn: #9a5a00;
      --info: #305e72;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(183, 75, 42, 0.18), transparent 32%),
        linear-gradient(135deg, #f7f1e6 0%, #ebe2d2 55%, #e6ddcd 100%);
      font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
    }}
    main {{
      max-width: 1240px;
      margin: 0 auto;
      padding: 32px 20px 56px;
    }}
    h1, h2, h3, h4 {{
      margin: 0 0 12px;
      font-weight: 600;
      letter-spacing: 0.02em;
    }}
    p {{ margin: 0; color: var(--muted); line-height: 1.5; }}
    code {{
      display: inline-block;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      background: rgba(29, 43, 34, 0.06);
      border-radius: 8px;
      padding: 4px 8px;
      margin-top: 6px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    small {{
      display: block;
      color: var(--muted);
      line-height: 1.4;
      margin-top: 4px;
    }}
    .hero {{
      display: grid;
      gap: 18px;
      grid-template-columns: 1.9fr 1fr;
      margin-bottom: 22px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 20px;
      box-shadow: 0 14px 32px rgba(29, 43, 34, 0.08);
      backdrop-filter: blur(12px);
    }}
    .panel-strong {{
      background: var(--panel-strong);
    }}
    .eyebrow {{
      text-transform: uppercase;
      letter-spacing: 0.14em;
      font-size: 12px;
      color: var(--accent);
      margin-bottom: 10px;
    }}
    .stats {{
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      margin-top: 18px;
    }}
    .stat {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
      background: rgba(255, 255, 255, 0.55);
    }}
    .stat strong {{
      display: block;
      font-size: 28px;
      color: var(--ink);
    }}
    .grid {{
      display: grid;
      gap: 18px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      margin-bottom: 18px;
    }}
    .grid-3 {{
      display: grid;
      gap: 18px;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      margin-bottom: 18px;
    }}
    .interaction-grid {{
      display: grid;
      gap: 18px;
      grid-template-columns: 1.5fr 1fr;
      margin-bottom: 18px;
    }}
    ul {{
      margin: 10px 0 0;
      padding-left: 18px;
      color: var(--muted);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      text-align: left;
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--muted);
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
    }}
    a:hover {{ text-decoration: underline; }}
    .empty, .empty-note {{
      color: var(--muted);
      text-align: center;
      font-style: italic;
    }}
    .empty-note {{
      display: inline-block;
    }}
    .badge, .pill {{
      display: inline-block;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .badge {{
      background: var(--accent-soft);
      color: var(--accent);
    }}
    .pill-ok {{
      background: rgba(39, 92, 69, 0.12);
      color: var(--ok);
    }}
    .pill-warning {{
      background: rgba(154, 90, 0, 0.12);
      color: var(--warn);
    }}
    .pill-info {{
      background: rgba(48, 94, 114, 0.12);
      color: var(--info);
    }}
    .ok {{
      color: var(--ok);
    }}
    .helper-note {{
      margin-top: 12px;
      padding: 12px 14px;
      border-left: 3px solid var(--accent);
      background: rgba(183, 75, 42, 0.08);
      border-radius: 10px;
    }}
    .form-grid {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      margin-bottom: 12px;
    }}
    label {{
      display: block;
      font-size: 13px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    select, input, textarea, button {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
      font: inherit;
      color: var(--ink);
      background: rgba(255, 255, 255, 0.8);
    }}
    textarea {{
      min-height: 120px;
      resize: vertical;
    }}
    button {{
      cursor: pointer;
      background: var(--ink);
      color: #fff7ed;
      border: none;
    }}
    button:hover {{
      opacity: 0.92;
    }}
    .button-row {{
      display: flex;
      gap: 10px;
      margin-top: 12px;
    }}
    .button-row button {{
      width: auto;
      min-width: 120px;
    }}
    .ghost-button {{
      width: auto;
      background: transparent;
      border: 1px solid var(--line);
      color: var(--accent);
      padding: 8px 12px;
    }}
    .result-shell {{
      margin-top: 16px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.56);
      padding: 16px;
    }}
    .result-meta {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }}
    .result-text {{
      white-space: pre-wrap;
      line-height: 1.55;
      color: var(--ink);
    }}
    .citation-list, .command-list {{
      list-style: none;
      padding: 0;
      margin: 10px 0 0;
    }}
    .citation-list li, .command-list li {{
      padding: 10px 0;
      border-top: 1px solid var(--line);
    }}
    .citation-list li:first-child, .command-list li:first-child {{
      border-top: 0;
      padding-top: 0;
    }}
    .command-list span {{
      display: block;
      color: var(--muted);
      margin-top: 4px;
    }}
    .resource-search {{
      margin-bottom: 14px;
    }}
    .resource-card {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      background: rgba(255, 255, 255, 0.6);
    }}
    .resource-card[hidden] {{
      display: none;
    }}
    .resource-grid {{
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .resource-group {{
      margin-top: 14px;
    }}
    .resource-why {{
      margin-top: 10px;
      color: var(--ink);
    }}
    .question-list {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .readiness-item {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background: rgba(255, 255, 255, 0.6);
      margin-bottom: 12px;
    }}
    .readiness-item:last-child {{
      margin-bottom: 0;
    }}
    .readiness-action {{
      margin-top: 10px;
    }}
    .mode-note {{
      margin-top: 8px;
      color: var(--muted);
    }}
    @media (max-width: 1000px) {{
      .hero, .interaction-grid, .grid, .grid-3, .resource-grid {{
        grid-template-columns: 1fr;
      }}
    }}
    @media (max-width: 760px) {{
      .stats, .form-grid {{
        grid-template-columns: 1fr;
      }}
    }}
    @media (max-width: 560px) {{
      main {{
        padding: 18px 14px 40px;
      }}
      .button-row {{
        flex-direction: column;
      }}
      .button-row button {{
        width: 100%;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <div class="panel panel-strong">
        <div class="eyebrow">ollarma operator dashboard</div>
        <h1>Chat, inspect, and ground your next move in one place.</h1>
        <p>
          This dashboard is now an <strong>interactive helper and bounded execution surface</strong> for
          `ollarma`: generic local chat, project-routed grounded help, guarded
          workflow submission, bounded autopilot, and a curated operator KB.
        </p>
        <div class="stats">
          <div class="stat"><span class="eyebrow">projects</span><strong>{overview.project_count}</strong></div>
          <div class="stat"><span class="eyebrow">queue depth</span><strong>{overview.scheduler.queue_depth}</strong></div>
          <div class="stat"><span class="eyebrow">interactive</span><strong>{'yes' if overview.boundary.interactive else 'no'}</strong></div>
          <div class="stat"><span class="eyebrow">generated</span><strong style="font-size:18px">{escape(overview.generated_at)}</strong></div>
        </div>
        <div class="helper-note">
          Generic chat is useful for broad questions, but it is uncited. Use
          project-routed help when you want grounded answers with KB evidence.
          Use workflow or autopilot when you need deterministic local execution.
        </div>
      </div>
      <div class="panel">
        <div class="eyebrow">boundary</div>
        <h2>Still bounded on purpose</h2>
        <p>{escape(overview.boundary.integration_contract.replace('_', ' '))}</p>
        <p style="margin-top:12px">Review gate: <span class="ok">{escape(overview.boundary.review_gate)}</span></p>
        <p style="margin-top:12px">Dashboard path: <code>{escape(overview.boundary.dashboard_path)}</code></p>
        <p style="margin-top:12px">Read-only mutation boundary: <strong>{'yes' if overview.boundary.read_only else 'no'}</strong></p>
        <p style="margin-top:12px">No runtime dependency on portfolio-dashboard.</p>
      </div>
    </section>

    <section class="interaction-grid">
      <div class="panel panel-strong">
        <div class="eyebrow">interactive helper</div>
        <h2>Chat With ollarma</h2>
        <p>
          Choose the lane explicitly. General chat uses the local model directly.
          Project-routed help uses retrieval-first routing and can return evidence citations.
        </p>
        <form id="dashboard-chat-form">
          <div class="form-grid">
            <div>
              <label for="chat-mode">Mode</label>
              <select id="chat-mode" name="chat-mode">
                <option value="general_chat">General chat</option>
                <option value="project_route">Project-routed help</option>
              </select>
              <p class="mode-note" id="chat-mode-note">Uncited local model chat for broad questions.</p>
            </div>
            <div id="project-field" hidden>
              <label for="chat-project">Project</label>
              <select id="chat-project" name="chat-project">
                <option value="">Select a project</option>
                {project_options}
              </select>
            </div>
          </div>
          <div class="form-grid">
            <div>
              <label for="chat-model">Model selection</label>
              <select id="chat-model" name="chat-model">
                {model_options}
              </select>
              <small id="chat-model-note">
                Leave this on automatic to keep current selection and routing behavior.
                Pick a model only when you want to force a specific local tag.
              </small>
            </div>
            <div>
              <label for="resource-filter">Operator KB filter</label>
              <input id="resource-filter" class="resource-search" type="search" placeholder="filter docs, commands, citations">
            </div>
          </div>
          <div>
            <label for="chat-prompt">Prompt</label>
            <textarea id="chat-prompt" name="chat-prompt" placeholder="Ask how to use ollarma, inspect what is missing, or route a grounded project question."></textarea>
          </div>
          <div class="button-row">
            <button type="submit">Ask ollarma</button>
            <button type="button" class="ghost-button" id="clear-chat">Clear</button>
          </div>
        </form>
        <div class="result-shell" id="chat-result">
          <div class="result-meta">
            <span class="badge">ready</span>
            <span class="badge">general chat or project route</span>
          </div>
          <div class="result-text">Ask a question above. Routed answers will show lane metadata and citations here.</div>
          <div id="chat-citations"></div>
        </div>
      </div>
      <div class="panel">
        <div class="eyebrow">what you are missing</div>
        <h2>Readiness</h2>
        <p>These findings come from the current dashboard state, not from a guessed model answer.</p>
        <div style="margin-top:14px">{readiness_blocks}</div>
      </div>
    </section>

    <section class="interaction-grid">
      <div class="panel panel-strong">
        <div class="eyebrow">bounded execution</div>
        <h2>Run With Guardrails</h2>
        <p>
          Use <strong>workflow</strong> for validated manifest-backed modules.
          Use <strong>autopilot</strong> when you need bounded asset discovery
          or a policy-driven local run. If the task needs broader coding,
          planning, or cross-repo mutation, hand it off to Watchtower or a
          frontier lane.
        </p>
        <form id="dashboard-execution-form">
          <div class="form-grid">
            <div>
              <label for="execution-mode">Mode</label>
              <select id="execution-mode" name="execution-mode">
                <option value="workflow">Workflow module</option>
                <option value="autopilot">Autopilot</option>
              </select>
              <p class="mode-note" id="execution-mode-note">Submit one validated manifest step through the workflow lane.</p>
            </div>
            <div>
              <label for="execution-project">Project</label>
              <select id="execution-project" name="execution-project">
                <option value="">Select a project</option>
                {project_options}
              </select>
            </div>
          </div>
          <div id="workflow-fields">
            <div class="form-grid">
              <div>
                <label for="workflow-manifest">Workflow module</label>
                <select id="workflow-manifest" name="workflow-manifest">
                  <option value="">Select a manifest</option>
                </select>
                <small id="workflow-manifest-note">Discovered from repo-local manifest roots for the selected project.</small>
              </div>
              <div>
                <label for="workflow-step">Step</label>
                <select id="workflow-step" name="workflow-step">
                  <option value="">Select a step</option>
                </select>
                <small id="workflow-step-note">Only validated manifest steps are exposed here.</small>
              </div>
            </div>
          </div>
          <div id="autopilot-fields" hidden>
            <div class="form-grid">
              <div>
                <label for="autopilot-run-assets">Autopilot mode</label>
                <select id="autopilot-run-assets" name="autopilot-run-assets">
                  <option value="inventory">Inventory only</option>
                  <option value="run">Run discovered assets</option>
                </select>
                <small>Inventory mode stays non-executing and returns the bounded asset set for review.</small>
              </div>
              <div>
                <label for="execution-model">Execution model</label>
                <select id="execution-model" name="execution-model">
                  {model_options}
                </select>
                <small id="execution-model-note">Automatic preserves current workflow selection behavior. Autopilot stays policy-driven.</small>
              </div>
            </div>
          </div>
          <div class="button-row">
            <button type="submit">Run bounded execution</button>
            <button type="button" class="ghost-button" id="refresh-workflows">Refresh modules</button>
          </div>
        </form>
        <div class="result-shell" id="execution-result">
          <div class="result-meta">
            <span class="badge">ready</span>
            <span class="badge">workflow or autopilot</span>
          </div>
          <div class="result-text">Select a project, then submit a validated workflow step or a bounded autopilot run.</div>
        </div>
      </div>
      <div class="panel">
        <div class="eyebrow">execution boundary</div>
        <h2>What This Does Not Expose</h2>
        <p>
          The dashboard can submit existing guarded lanes, but it does not
          expose raw shell, KB build, git mutation, edit loops, or arbitrary
          repo access. Normal UI paths also keep adapter overrides and absolute
          project roots hidden.
        </p>
        <div style="margin-top:14px" class="readiness-item">
          <div class="pill pill-info">handoff</div>
          <h3>Use Watchtower for broader orchestration</h3>
          <p>Watchtower owns cross-project reasoning, broader planning, and higher-authority orchestration. The dashboard owns deterministic local helper and execution surfaces only.</p>
        </div>
      </div>
    </section>

    <section class="panel" style="margin-bottom:18px">
      <div class="eyebrow">operator knowledge base</div>
      <h2>Important Resources, Commands, And Citations</h2>
      <p>
        This is the deterministic operator KB for the dashboard: curated docs,
        commands, and file-path citations for learning how to use `ollarma`
        without guessing where the important materials live.
      </p>
      <div class="resource-grid" id="resource-grid" style="margin-top:16px">{resource_cards}</div>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Helper Surfaces</h2>
        <ul>{helper_surfaces}</ul>
      </div>
      <div class="panel">
        <h2>Deterministic Execution Surfaces</h2>
        <ul>{execution_surfaces}</ul>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <span class="badge">safe for</span>
        <ul>{safe_for}</ul>
      </div>
      <div class="panel">
        <span class="badge">unsafe for</span>
        <ul>{unsafe_for}</ul>
      </div>
    </section>

    <section class="panel" style="margin-bottom:18px">
      <div class="eyebrow">file search</div>
      <h2>File Search</h2>
      <p>
        Semantically search user-approved Mac directories via the macfind index.
        Use <code>ollarma macfind query &lt;text&gt;</code> from the CLI, or POST to the endpoints below.
        Run <code>ollarma macfind reindex</code> to rebuild the index after adding new directories.
      </p>
      <div class="grid" style="margin-top:14px">
        <div class="readiness-item">
          <div class="pill pill-info">query</div>
          <h3>Search the Index</h3>
          <p>POST <code>/macfind/query</code> with <code>{{"query": "...", "namespace_prefix": null}}</code></p>
          <p style="margin-top:8px">Returns a <code>MacFindReceipt</code> with ranked file hits, snippets, and hybrid scores.</p>
        </div>
        <div class="readiness-item">
          <div class="pill pill-warning">reindex</div>
          <h3>Rebuild the Index</h3>
          <p>POST <code>/macfind/reindex</code> (no body required)</p>
          <p style="margin-top:8px">Crawls approved directories, chunks files, embeds chunks via Ollama, and writes the SQLite index.</p>
        </div>
      </div>
    </section>

    <section class="panel" style="margin-bottom:18px">
      <h2>KB Status</h2>
      <table>
        <thead>
          <tr>
            <th>Project</th>
            <th>Status</th>
            <th>Reason</th>
            <th>Sources</th>
            <th>Documents</th>
            <th>Chunks</th>
            <th>Built</th>
            <th>Search DB</th>
          </tr>
        </thead>
        <tbody>{kb_rows}</tbody>
      </table>
    </section>

    {gateway_panel_html}

    <section class="panel" style="margin-bottom:18px">
      <h2>Recent Route Receipts</h2>
      <table>
        <thead>
          <tr>
            <th>Project</th>
            <th>Lane</th>
            <th>Reason</th>
            <th>Query Class</th>
            <th>Evidence</th>
            <th>Prompt Preview</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>{route_receipt_rows}</tbody>
      </table>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Recent Workflow Runs</h2>
        <table>
          <thead>
            <tr>
              <th>Project</th>
              <th>Run</th>
              <th>Stage</th>
              <th>Status</th>
              <th>Reason</th>
              <th>Receipts</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>{workflow_rows}</tbody>
        </table>
      </div>
      <div class="panel">
        <h2>Recent Autopilot Runs</h2>
        <table>
          <thead>
            <tr>
              <th>Project</th>
              <th>Run</th>
              <th>Stage</th>
              <th>Status</th>
              <th>Reason</th>
              <th>Receipts</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>{autopilot_rows}</tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    const DASHBOARD_DATA = {dashboard_data};
    const form = document.getElementById("dashboard-chat-form");
    const modeEl = document.getElementById("chat-mode");
    const modeNoteEl = document.getElementById("chat-mode-note");
    const projectField = document.getElementById("project-field");
    const projectEl = document.getElementById("chat-project");
    const modelEl = document.getElementById("chat-model");
    const modelNoteEl = document.getElementById("chat-model-note");
    const promptEl = document.getElementById("chat-prompt");
    const resultEl = document.getElementById("chat-result");
    const executionForm = document.getElementById("dashboard-execution-form");
    const executionModeEl = document.getElementById("execution-mode");
    const executionModeNoteEl = document.getElementById("execution-mode-note");
    const executionProjectEl = document.getElementById("execution-project");
    const workflowFieldsEl = document.getElementById("workflow-fields");
    const workflowManifestEl = document.getElementById("workflow-manifest");
    const workflowManifestNoteEl = document.getElementById("workflow-manifest-note");
    const workflowStepEl = document.getElementById("workflow-step");
    const autopilotFieldsEl = document.getElementById("autopilot-fields");
    const autopilotRunAssetsEl = document.getElementById("autopilot-run-assets");
    const executionModelEl = document.getElementById("execution-model");
    const executionModelNoteEl = document.getElementById("execution-model-note");
    const executionResultEl = document.getElementById("execution-result");
    const refreshWorkflowsButton = document.getElementById("refresh-workflows");
    const clearButton = document.getElementById("clear-chat");
    const filterEl = document.getElementById("resource-filter");
    const resourceCards = Array.from(document.querySelectorAll(".resource-card"));
    let workflowCatalog = null;

    function escapeHtml(text) {{
      return String(text)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }}

    function setModeUi() {{
      const mode = modeEl.value;
      const routed = mode === "project_route";
      projectField.hidden = !routed;
      modeNoteEl.textContent = routed
        ? "Grounded project help with retrieval-first routing and evidence refs."
        : "Uncited local model chat for broad questions.";
      modelNoteEl.textContent = routed
        ? "Automatic uses retrieval-first routing plus the validated route model. Pick a model only to force a specific local tag."
        : "Automatic uses the validated chat selection when available, then the bounded fallback path if needed.";
    }}

    function setExecutionModeUi() {{
      const workflowMode = executionModeEl.value === "workflow";
      workflowFieldsEl.hidden = !workflowMode;
      autopilotFieldsEl.hidden = workflowMode;
      executionModeNoteEl.textContent = workflowMode
        ? "Submit one validated manifest step through the workflow lane."
        : "Run bounded asset discovery or a policy-driven local autopilot execution.";
      executionModelNoteEl.textContent = workflowMode
        ? "Automatic preserves current workflow selection behavior. Pick a model only when you want to force a specific local tag."
        : "Autopilot stays policy-driven. Model override is ignored for autopilot runs.";
    }}

    function renderCitations(citations) {{
      const citationsEl = document.getElementById("chat-citations");
      if (!citationsEl) {{
        return;
      }}
      if (!citations || citations.length === 0) {{
        citationsEl.innerHTML = "";
        return;
      }}
      const items = citations.map((item) => {{
        const path = item.path || item.repo_relative || "unknown";
        const score = item.score !== undefined ? " score=" + item.score : "";
        const chunk = item.chunk_id ? " chunk=" + item.chunk_id : "";
        return "<li><code>" + escapeHtml(path) + "</code><small>" + escapeHtml((item.authority || "") + (chunk || "") + (score || "")) + "</small></li>";
      }}).join("");
      citationsEl.innerHTML = "<div class=\\"resource-group\\"><h4>Citations</h4><ul class=\\"citation-list\\">" + items + "</ul></div>";
    }}

    function renderRecovery(detail, commands) {{
      const detailHtml = detail
        ? "<div class=\\"resource-group\\"><h4>Why This Happened</h4><p>" + escapeHtml(detail) + "</p></div>"
        : "";
      const commandHtml = commands && commands.length
        ? "<div class=\\"resource-group\\"><h4>Recovery</h4><code>" + escapeHtml(commands.join("\\n")) + "</code></div>"
        : "";
      return detailHtml + commandHtml;
    }}

    function setResult(state) {{
      const meta = state.meta.map((item) => "<span class=\\"badge\\">" + escapeHtml(item) + "</span>").join("");
      resultEl.innerHTML =
        "<div class=\\"result-meta\\">" + meta + "</div>"
        + "<div class=\\"result-text\\">" + escapeHtml(state.text) + "</div>"
        + renderRecovery(state.detail, state.commands)
        + "<div id=\\"chat-citations\\"></div>";
      renderCitations(state.citations || []);
    }}

    function setExecutionResult(state) {{
      const meta = state.meta.map((item) => "<span class=\\"badge\\">" + escapeHtml(item) + "</span>").join("");
      executionResultEl.innerHTML =
        "<div class=\\"result-meta\\">" + meta + "</div>"
        + "<div class=\\"result-text\\">" + escapeHtml(state.text) + "</div>"
        + renderRecovery(state.detail, state.commands);
    }}

    function resetWorkflowSelectors(message) {{
      workflowCatalog = null;
      workflowManifestEl.innerHTML = "<option value=''>" + escapeHtml(message || "Select a manifest") + "</option>";
      workflowStepEl.innerHTML = "<option value=''>Select a step</option>";
    }}

    function selectedManifest() {{
      if (!workflowCatalog || !workflowManifestEl.value) {{
        return null;
      }}
      return (workflowCatalog.manifests || []).find((item) => item.manifest_digest === workflowManifestEl.value) || null;
    }}

    function populateWorkflowSteps() {{
      const manifest = selectedManifest();
      if (!manifest) {{
        workflowStepEl.innerHTML = "<option value=''>Select a step</option>";
        return;
      }}
      const options = ["<option value=''>Select a step</option>"];
      (manifest.steps || []).forEach((step) => {{
        const label = step.step_id + " (" + step.task_type + ")";
        options.push("<option value='" + escapeHtml(step.step_id) + "'>" + escapeHtml(label) + "</option>");
      }});
      workflowStepEl.innerHTML = options.join("");
    }}

    function populateWorkflowCatalog(catalog) {{
      workflowCatalog = catalog;
      const manifests = catalog.manifests || [];
      if (manifests.length === 0) {{
        resetWorkflowSelectors("No manifests discovered");
        workflowManifestNoteEl.textContent = "No portable manifest files were discovered for this project yet.";
        return;
      }}

      const options = ["<option value=''>Select a manifest</option>"];
      manifests.forEach((manifest) => {{
        const ref = manifest.manifest_ref || {{}};
        const label = ref.repo_relative || manifest.consumer_repo || manifest.run_id || manifest.manifest_digest;
        options.push("<option value='" + escapeHtml(manifest.manifest_digest) + "'>" + escapeHtml(label) + "</option>");
      }});
      workflowManifestEl.innerHTML = options.join("");
      workflowManifestNoteEl.textContent = "Discovered " + manifests.length + " manifest module(s) for the selected project.";
      populateWorkflowSteps();
    }}

    async function loadWorkflowCatalog() {{
      const project = executionProjectEl.value;
      if (!project) {{
        resetWorkflowSelectors("Select a project first");
        workflowManifestNoteEl.textContent = "Discovered from repo-local manifest roots for the selected project.";
        return;
      }}

      resetWorkflowSelectors("Loading manifests...");
      workflowManifestNoteEl.textContent = "Inspecting repo-local manifest roots for " + project + ".";
      try {{
        const response = await fetch("/dashboard/workflows/" + encodeURIComponent(project));
        const data = await response.json();
        if (!response.ok) {{
          resetWorkflowSelectors("Manifest discovery blocked");
          workflowManifestNoteEl.textContent = data.error || "Manifest discovery failed.";
          return;
        }}
        populateWorkflowCatalog(data);
      }} catch (error) {{
        resetWorkflowSelectors("Manifest discovery failed");
        workflowManifestNoteEl.textContent = error instanceof Error ? error.message : "Unknown error.";
      }}
    }}

    async function submitQuery(event) {{
      event.preventDefault();
      const mode = modeEl.value;
      const prompt = promptEl.value.trim();
      const model = modelEl.value.trim();
      if (!prompt) {{
        setResult({{ meta: ["error"], text: "Prompt is required.", citations: [] }});
        return;
      }}
      if (mode === "project_route" && !projectEl.value) {{
        setResult({{ meta: ["error"], text: "Select a project for project-routed help.", citations: [] }});
        return;
      }}

      setResult({{ meta: ["working", mode], text: "Waiting for ollarma…", citations: [] }});

      const endpoint = mode === "project_route" ? "/route" : "/chat";
      const payload = mode === "project_route"
        ? {{ prompt, project: projectEl.value }}
        : {{ message: prompt }};
      if (model) {{
        payload.model = model;
      }}

      try {{
        const response = await fetch(endpoint, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(payload),
        }});
        const data = await response.json();
        if (!response.ok) {{
          const helper = data.helper_chat || null;
          const meta = ["error", mode];
          if (helper && helper.status) {{
            meta.push("helper " + helper.status);
          }}
          if (helper && helper.effective_model) {{
            meta.push("model " + helper.effective_model);
          }}
          setResult({{
            meta,
            text: data.error || "Request failed.",
            detail: helper ? helper.detail : "",
            commands: helper ? helper.recovery_commands || [] : [],
            citations: [],
          }});
          return;
        }}
        if (mode === "project_route") {{
          const meta = [
            "lane " + (data.lane || "unknown"),
            data.reason_code || "project route",
            data.kb_status ? "kb " + data.kb_status : "kb n/a",
            data.model ? "model " + data.model : "cited route",
          ];
          setResult({{
            meta,
            text: data.final_response || "",
            citations: data.evidence_refs || [],
          }});
          return;
        }}
        const meta = [
          data.status || "answered",
          data.model ? "model " + data.model : "general chat",
          "uncited",
        ];
        if (data.reason_code) {{
          meta.push(data.reason_code);
        }}
        setResult({{
          meta,
          text: data.response || "",
          detail: data.detail || "",
          commands: data.recovery_commands || [],
          citations: [],
        }});
      }} catch (error) {{
        setResult({{
          meta: ["error", mode],
          text: error instanceof Error ? error.message : "Unknown error.",
          citations: [],
        }});
      }}
    }}

    async function submitExecution(event) {{
      event.preventDefault();
      const mode = executionModeEl.value;
      const project = executionProjectEl.value;
      if (!project) {{
        setExecutionResult({{ meta: ["error", mode], text: "Select a project before submitting execution.", commands: [] }});
        return;
      }}

      if (mode === "workflow") {{
        const manifest = selectedManifest();
        const stepId = workflowStepEl.value;
        if (!manifest) {{
          setExecutionResult({{ meta: ["error", mode], text: "Select a workflow manifest.", commands: [] }});
          return;
        }}
        if (!stepId) {{
          setExecutionResult({{ meta: ["error", mode], text: "Select a workflow step.", commands: [] }});
          return;
        }}

        setExecutionResult({{ meta: ["working", mode], text: "Submitting bounded workflow step…", commands: [] }});
        const payload = {{
          project,
          manifest_ref: manifest.manifest_ref,
          step_id: stepId,
        }};
        if (executionModelEl.value.trim()) {{
          payload.model = executionModelEl.value.trim();
        }}
        try {{
          const response = await fetch("/workflow", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify(payload),
          }});
          const data = await response.json();
          if (!response.ok) {{
            setExecutionResult({{
              meta: ["error", mode],
              text: data.error || "Workflow submission failed.",
              detail: data.detail || "",
              commands: data.recovery_commands || [],
            }});
            return;
          }}
          const meta = [
            data.status || "accepted",
            data.lane || "workflow",
            data.model ? "model " + data.model : "automatic",
          ];
          if (data.next_stage) {{
            meta.push("next " + data.next_stage);
          }}
          const refs = [];
          if (data.receipt_ref && data.receipt_ref.repo_relative) {{
            refs.push("receipt " + data.receipt_ref.repo_relative);
          }}
          if (data.checkpoint_ref && data.checkpoint_ref.repo_relative) {{
            refs.push("checkpoint " + data.checkpoint_ref.repo_relative);
          }}
          setExecutionResult({{
            meta,
            text: "Workflow step " + stepId + " submitted for " + project + ".",
            detail: refs.join(" | "),
            commands: [],
          }});
        }} catch (error) {{
          setExecutionResult({{
            meta: ["error", mode],
            text: error instanceof Error ? error.message : "Unknown error.",
            commands: [],
          }});
        }}
        return;
      }}

      setExecutionResult({{ meta: ["working", mode], text: "Running bounded autopilot…", commands: [] }});
      const autopilotPayload = {{
        project,
        run_assets: autopilotRunAssetsEl.value === "run",
      }};
      try {{
        const response = await fetch("/autopilot", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(autopilotPayload),
        }});
        const data = await response.json();
        if (!response.ok) {{
          setExecutionResult({{
            meta: ["error", mode],
            text: data.error || "Autopilot request failed.",
            detail: data.detail || "",
            commands: data.recovery_commands || [],
          }});
          return;
        }}
        setExecutionResult({{
          meta: [
            data.run_executed ? "executed" : "inventory",
            "assets " + String(data.total_assets ?? 0),
            "failed " + String(data.failed ?? 0),
          ],
          text: "Autopilot completed for " + (data.project_name || project) + ".",
          detail: "passed=" + String(data.passed ?? 0) + " | escalation_needed=" + String(data.escalation_needed ?? 0),
          commands: [],
        }});
      }} catch (error) {{
        setExecutionResult({{
          meta: ["error", mode],
          text: error instanceof Error ? error.message : "Unknown error.",
          commands: [],
        }});
      }}
    }}

    function clearChat() {{
      promptEl.value = "";
      setResult({{
        meta: ["ready", "general chat or project route"],
        text: "Ask a question above. Routed answers will show lane metadata and citations here.",
        citations: [],
      }});
    }}

    function filterResources() {{
      const query = filterEl.value.trim().toLowerCase();
      resourceCards.forEach((card) => {{
        const haystack = card.dataset.search || "";
        card.hidden = query !== "" && !haystack.includes(query);
      }});
    }}

    modeEl.addEventListener("change", setModeUi);
    form.addEventListener("submit", submitQuery);
    executionModeEl.addEventListener("change", setExecutionModeUi);
    executionProjectEl.addEventListener("change", loadWorkflowCatalog);
    workflowManifestEl.addEventListener("change", populateWorkflowSteps);
    executionForm.addEventListener("submit", submitExecution);
    refreshWorkflowsButton.addEventListener("click", loadWorkflowCatalog);
    clearButton.addEventListener("click", clearChat);
    filterEl.addEventListener("input", filterResources);
    document.querySelectorAll(".suggested-question").forEach((button) => {{
      button.addEventListener("click", () => {{
        promptEl.value = button.dataset.question || "";
        promptEl.focus();
      }});
    }});

    setModeUi();
    setExecutionModeUi();
  </script>
</body>
</html>"""
