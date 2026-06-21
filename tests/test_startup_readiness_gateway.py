"""Tests for Plan 57.1-02: /startup/readiness gateway block (OBS-57, F-05).

Additive GatewayPosture sub-payload; StartupReadinessPayload.schema_version stays
at 1. Virtual-key IDs must NEVER appear in the serialized readiness output
(counts only, per D-57.1-02).
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib

import pytest

from ollarma import service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(repo_root: pathlib.Path, gateway: dict | None) -> None:
    planning_dir = repo_root / ".planning"
    planning_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {"features": {"gateway": gateway}} if gateway is not None else {"features": {}}
    (planning_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_admission_line(
    repo_root: pathlib.Path, created_at: str, admission_id: str = "adm_fake"
) -> None:
    p = repo_root / ".ollarma" / "gateway" / "admissions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "admission_id": admission_id,
        "escalation_receipt_id": "er_fake",
        "escalation_receipt_content_hash": "sha256:" + "0" * 64,
        "outcome": "accept",
        "reason_code": None,
        "reason_detail": "",
        "project": "demo",
        "schema_version": 1,
        "created_at": created_at,
        "parent_hash": "genesis",
        "receipt_hash": "sha256:" + "a" * 64,
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _write_receipt_line(repo_root: pathlib.Path, created_at: str) -> None:
    p = repo_root / ".ollarma" / "gateway" / "receipts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "escalation_receipt_id": "er_fake",
        "admission_receipt_hash": "sha256:" + "a" * 64,
        "provider": "synth",
        "model_id": "synth-m",
        "provider_request_id": None,
        "prompt_tokens": 0,
        "response_tokens": 0,
        "cost_usd": "0",
        "latency_ms": 0,
        "status": "dry_run",
        "reason_code": None,
        "context_truncated": False,
        "original_tokens": None,
        "truncated_tokens": None,
        "schema_version": 1,
        "created_at": created_at,
        "parent_hash": "genesis",
        "receipt_hash": "sha256:" + "b" * 64,
        "dry_run": True,
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _today_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _yesterday_iso() -> str:
    y = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
    return y.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_readiness_gateway_block_shape_when_disabled(tmp_path: pathlib.Path) -> None:
    """Empty repo (no config, no streams) yields disabled-posture defaults."""
    posture = service._build_gateway_posture(tmp_path)
    assert posture.enabled is False
    assert posture.allowlist_size == 0
    assert posture.virtual_keys_configured == 0
    assert posture.rate_cap_state == "not_configured"
    assert posture.admissions_today == 0
    assert posture.receipts_today == 0


def test_readiness_gateway_block_with_config(tmp_path: pathlib.Path) -> None:
    """Populated allowlist + vk registry yields accurate counts."""
    _write_config(
        tmp_path,
        {
            "enabled": True,
            "allowlist": ["project_a", "project_b"],
            "virtual_keys": [
                {"id": "vk_one", "keychain_service": "svc1"},
                {"id": "vk_two", "keychain_service": "svc2"},
                {"id": "vk_three", "keychain_service": "svc3"},
            ],
        },
    )
    posture = service._build_gateway_posture(tmp_path)
    assert posture.enabled is True
    assert posture.allowlist_size == 2
    assert posture.virtual_keys_configured == 3
    assert posture.rate_cap_state == "not_configured"
    assert posture.admissions_today == 0
    assert posture.receipts_today == 0


def test_readiness_gateway_block_counts_today_admissions(tmp_path: pathlib.Path) -> None:
    """5 admissions written today + 3 receipts today yield matching counts."""
    today = _today_iso()
    for i in range(5):
        _write_admission_line(tmp_path, today, admission_id=f"adm_{i}")
    for _ in range(3):
        _write_receipt_line(tmp_path, today)

    posture = service._build_gateway_posture(tmp_path)
    assert posture.admissions_today == 5
    assert posture.receipts_today == 3


def test_readiness_gateway_block_ignores_yesterday(tmp_path: pathlib.Path) -> None:
    """Entries dated yesterday are excluded from today's counts (UTC boundary)."""
    yesterday = _yesterday_iso()
    today = _today_iso()
    _write_admission_line(tmp_path, yesterday, admission_id="adm_y1")
    _write_admission_line(tmp_path, yesterday, admission_id="adm_y2")
    _write_admission_line(tmp_path, today, admission_id="adm_t1")
    _write_admission_line(tmp_path, today, admission_id="adm_t2")
    _write_admission_line(tmp_path, today, admission_id="adm_t3")

    posture = service._build_gateway_posture(tmp_path)
    assert posture.admissions_today == 3
    assert posture.receipts_today == 0


def test_readiness_gateway_block_redacts_vk_ids(tmp_path: pathlib.Path) -> None:
    """vk ID strings MUST NEVER appear in serialized readiness payload."""
    sensitive_vk_ids = [
        "vk_project_science",
        "vk_anthropic_production",
        "vk_sibling_secret",
    ]
    _write_config(
        tmp_path,
        {
            "enabled": True,
            "allowlist": ["demo"],
            "virtual_keys": [
                {"id": vkid, "keychain_service": f"svc_{vkid}"}
                for vkid in sensitive_vk_ids
            ],
        },
    )
    posture = service._build_gateway_posture(tmp_path)
    # Count still correct
    assert posture.virtual_keys_configured == 3
    # Serialize the GatewayPosture to JSON and assert no vk ID leaks.
    dumped = json.dumps(posture.model_dump(mode="json"))
    for vkid in sensitive_vk_ids:
        assert vkid not in dumped, f"vk id {vkid!r} leaked into readiness payload"
    # Also check the keychain_service strings are not present.
    assert "keychain_service" not in dumped
    assert "svc_vk_project_science" not in dumped
