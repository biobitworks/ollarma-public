"""Plan 63-01 (OBS-58) — dashboard gateway panel tests.

Covers:
  1. Disabled gateway renders cleanly (no error markup).
  2. Counts show up when admissions/receipts are present (tail last 5).
  3. Missing ``.ollarma/gateway/*.jsonl`` files render zeros + no-activity text.
  4. vk ID strings NEVER appear in the rendered panel HTML (grep assertion).

These tests exercise the server-rendered HTML panel (no JavaScript), matching
the existing dashboard_html convention (``_render_*`` helpers + ``render_dashboard``).
"""
from __future__ import annotations

import pathlib

import orjson

from ollarma.dashboard_html import render_dashboard
from ollarma.fleet import AdapterConfig
from ollarma.scheduler import RuntimeSnapshot


def _mock_runtime_health(*args, **kwargs):
    from ollarma.service import HelperModelResolution, RuntimeHealthResult, SelectionHealth

    return RuntimeHealthResult(
        status="ok",
        helper_chat=HelperModelResolution(
            effective_model="qwen3:1.7b",
            status="ready",
            reason_code=None,
            detail="",
            recovery_commands=(),
        ),
        chat_selection=SelectionHealth(
            workload_class="chat",
            status="ready",
            model="qwen3:1.7b",
        ),
        route_selection=SelectionHealth(
            workload_class="route_prompt",
            status="ready",
            model="qwen3-coder:7b",
        ),
        project_count=0,
    )


def _mock_scheduler_snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        active_job_id=None,
        active_lane=None,
        active_model=None,
        active_project=None,
        queue_depth=0,
        queue_depth_by_lane={"workflow_execution_queue": 0},
        active_read_only=0,
        degraded_mode=False,
        resource_reason=None,
        swap_used_mb=0.0,
        loaded_models=(),
        telemetry_source="test",
    )


def _install_empty_registry(monkeypatch) -> None:
    from ollarma import service

    monkeypatch.setattr(service, "list_projects", lambda **kwargs: {})
    monkeypatch.setattr(service, "get_scheduler_snapshot", _mock_scheduler_snapshot)
    monkeypatch.setattr(service, "get_runtime_health", _mock_runtime_health)
    monkeypatch.setattr(
        service,
        "_probe_installed_model_names",
        lambda: (("qwen3:1.7b", "gemma3:4b-it-qat", "smollm2:latest"), None),
    )


def test_dashboard_panel_shows_disabled_when_gateway_off(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Gateway disabled (default — no config.json) => panel shows Disabled cleanly."""
    from ollarma import service

    _install_empty_registry(monkeypatch)

    overview = service.get_dashboard_overview(repo_root=tmp_path)

    assert overview.gateway is not None
    assert overview.gateway.enabled is False
    assert overview.gateway.admissions_today == 0
    assert overview.gateway.receipts_today == 0
    assert overview.recent_gateway_receipts == ()
    assert overview.recent_gateway_admissions == ()

    html = render_dashboard(overview)
    assert 'id="gateway-panel"' in html
    assert "Gateway:" in html
    assert "Disabled" in html
    assert "No FrontierReceipts have been recorded yet." in html
    assert "No gateway admission decisions have been recorded yet." in html


def test_dashboard_panel_shows_counts_when_admissions_present(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Admissions + receipts present => panel surfaces counts + last-entry summary."""
    from ollarma import service

    _install_empty_registry(monkeypatch)

    # Write a config.json enabling the gateway + an allowlist + virtual_keys.
    # The virtual-key ID string here is the signature we'll grep for in the
    # redaction test — make it distinctive so accidental leakage stands out.
    planning_dir = tmp_path / ".planning"
    planning_dir.mkdir()
    (planning_dir / "config.json").write_bytes(
        orjson.dumps(
            {
                "features": {
                    "gateway": {
                        "enabled": True,
                        "allowlist": ["anthropic:claude-3-5-sonnet"],
                        "virtual_keys": [
                            {"id": "vk_test_DO_NOT_LEAK_abc123"},
                            {"id": "vk_test_DO_NOT_LEAK_def456"},
                        ],
                    },
                },
            },
        ),
    )

    gateway_dir = tmp_path / ".ollarma" / "gateway"
    gateway_dir.mkdir(parents=True)

    # Today-dated admissions (2 accept, 1 reject) so the rejection rate is non-zero.
    import datetime as _dt

    today_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    admissions_lines = [
        orjson.dumps(
            {
                "admission_id": "adm-001",
                "escalation_receipt_id": "er-001",
                "escalation_receipt_content_hash": "hash-1",
                "outcome": "accept",
                "reason_code": None,
                "reason_detail": "",
                "project": "alpha",
                "schema_version": 1,
                "created_at": today_iso,
                "parent_hash": "0" * 64,
                "receipt_hash": "h1",
            },
        ),
        orjson.dumps(
            {
                "admission_id": "adm-002",
                "escalation_receipt_id": "er-002",
                "escalation_receipt_content_hash": "hash-2",
                "outcome": "reject",
                "reason_code": "GATEWAY_NOT_ALLOWED",
                "reason_detail": "",
                "project": "beta",
                "schema_version": 1,
                "created_at": today_iso,
                "parent_hash": "h1",
                "receipt_hash": "h2",
            },
        ),
        orjson.dumps(
            {
                "admission_id": "adm-003",
                "escalation_receipt_id": "er-003",
                "escalation_receipt_content_hash": "hash-3",
                "outcome": "accept",
                "reason_code": None,
                "reason_detail": "",
                "project": "gamma",
                "schema_version": 1,
                "created_at": today_iso,
                "parent_hash": "h2",
                "receipt_hash": "h3",
            },
        ),
    ]
    (gateway_dir / "admissions.jsonl").write_bytes(b"\n".join(admissions_lines) + b"\n")

    receipts_lines = [
        orjson.dumps(
            {
                "escalation_receipt_id": "er-001",
                "admission_receipt_hash": "h1",
                "provider": "anthropic",
                "model_id": "claude-3-5-sonnet",
                "prompt_tokens": 100,
                "response_tokens": 200,
                "cost_usd": "0.0125",
                "latency_ms": 842,
                "status": "succeeded",
                "reason_code": None,
                "context_truncated": False,
                "schema_version": 1,
                "created_at": today_iso,
                "parent_hash": "0" * 64,
                "receipt_hash": "rh1",
                "dry_run": False,
            },
        ),
    ]
    (gateway_dir / "receipts.jsonl").write_bytes(b"\n".join(receipts_lines) + b"\n")

    overview = service.get_dashboard_overview(repo_root=tmp_path)

    assert overview.gateway is not None
    assert overview.gateway.enabled is True
    assert overview.gateway.allowlist_size == 1
    assert overview.gateway.virtual_keys_configured == 2
    assert overview.gateway.admissions_today == 3
    assert overview.gateway.receipts_today == 1
    assert len(overview.recent_gateway_receipts) == 1
    assert overview.recent_gateway_receipts[0].model_id == "claude-3-5-sonnet"
    assert overview.recent_gateway_receipts[0].status == "succeeded"
    assert overview.recent_gateway_receipts[0].cost_usd == "0.0125"
    assert len(overview.recent_gateway_admissions) == 3
    assert overview.recent_gateway_admissions[-1].outcome == "accept"

    html = render_dashboard(overview)
    assert 'id="gateway-panel"' in html
    assert "Enabled" in html
    assert "claude-3-5-sonnet" in html
    assert "0.0125" in html
    assert "succeeded" in html
    # Rejection rate: 1 of 3 = 33.3%
    assert "33.3%" in html


def test_dashboard_panel_handles_missing_gateway_files_gracefully(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Missing .ollarma/gateway/*.jsonl => zeros + no-activity placeholders, no errors."""
    from ollarma import service

    _install_empty_registry(monkeypatch)

    # Enable gateway, but intentionally do NOT create .ollarma/gateway/.
    planning_dir = tmp_path / ".planning"
    planning_dir.mkdir()
    (planning_dir / "config.json").write_bytes(
        orjson.dumps(
            {"features": {"gateway": {"enabled": True, "allowlist": [], "virtual_keys": []}}},
        ),
    )

    overview = service.get_dashboard_overview(repo_root=tmp_path)

    assert overview.gateway is not None
    assert overview.gateway.enabled is True
    assert overview.gateway.admissions_today == 0
    assert overview.gateway.receipts_today == 0
    assert overview.recent_gateway_receipts == ()
    assert overview.recent_gateway_admissions == ()

    html = render_dashboard(overview)
    # Panel still rendered, no crash; placeholders present.
    assert 'id="gateway-panel"' in html
    assert "No FrontierReceipts have been recorded yet." in html
    assert "No gateway admission decisions have been recorded yet." in html
    # Rejection rate "n/a" when there are no admissions to divide by.
    assert "n/a" in html


def test_dashboard_panel_redacts_vk_ids(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """vk ID strings must NEVER appear in the rendered panel HTML (D-57.1-02)."""
    from ollarma import service

    _install_empty_registry(monkeypatch)

    # Plant a distinctive vk ID that would only appear if redaction is broken.
    sentinel_vk_id = "vk_test_DO_NOT_LEAK_abc123"
    planning_dir = tmp_path / ".planning"
    planning_dir.mkdir()
    (planning_dir / "config.json").write_bytes(
        orjson.dumps(
            {
                "features": {
                    "gateway": {
                        "enabled": True,
                        "allowlist": ["anthropic:claude-3-5-sonnet"],
                        "virtual_keys": [{"id": sentinel_vk_id}],
                    },
                },
            },
        ),
    )

    overview = service.get_dashboard_overview(repo_root=tmp_path)

    # Model-level redaction contract: posture carries a COUNT only.
    assert overview.gateway is not None
    assert overview.gateway.virtual_keys_configured == 1

    html = render_dashboard(overview)

    # Panel is present and renders the count, not the identifier.
    assert 'id="gateway-panel"' in html
    assert "virtual keys configured" in html
    # The actual grep assertion (the heart of this test): the vk ID never leaks.
    assert sentinel_vk_id not in html
    assert "DO_NOT_LEAK" not in html
