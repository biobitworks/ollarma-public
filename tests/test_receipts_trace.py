"""test_receipts_trace.py -- Phase 57.1-03 (OBS-59) chain-walk CLI tests.

Covers success criteria 1-6 from `.planning/phases/57.1-substrate-legibility/
57.1-03-PLAN.md`:

  1. Happy chain -> exit 0, all MATCH
  2. External escalation (no local ER record) -> exit 0, EXTERNAL_ESCALATION
  3. Hash mismatch -> exit 1
  4. Missing admission -> exit 1
  5. Disabled-status chain still valid -> exit 0
  6. Reject chain (admission reject + frontier failed) -> exit 0
  7. (bonus) --repo-root override works

All tests exercise the REAL GatewayClient + REAL GatewayReceiptStore + REAL
canonical_hash. Fixtures write genuine receipts to ``tmp_path/.ollarma/``.
"""
from __future__ import annotations

import json
import os
import pathlib
from decimal import Decimal

import orjson
import pytest
from typer.testing import CliRunner

from ollarma.cli import app
from ollarma.escalation import ReasonCode, build_escalation_receipt
from ollarma.evidence import canonical_hash
from ollarma.gateway import (
    FrontierReceipt,
    GatewayAdmissionEntry,
    GatewayReasonCode,
    GatewayReceiptStore,
)
from ollarma.gateway_client import GatewayClient
from ollarma.receipts_trace import render_table, trace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_escalation(project: str = "trace-proj"):
    return build_escalation_receipt(
        project=project,
        lane="ollarma-default",
        task_class="chat",
        reason_code=ReasonCode.SWAP_DEGRADED,
        reason_detail="swap pressure",
        next_action="frontier_or_human",
    )


def _write_escalation_locally(
    repo_root: pathlib.Path, er_payload: dict, er_id: str,
) -> None:
    """Persist an escalation receipt under the well-known escalations/ path.

    receipts_trace scans .ollarma/escalations/<id>.json first before session-log
    and incidents, so this is the canonical happy-path location for the test.
    """
    esc_dir = repo_root / ".ollarma" / "escalations"
    esc_dir.mkdir(parents=True, exist_ok=True)
    (esc_dir / f"{er_id}.json").write_bytes(
        orjson.dumps(er_payload, option=orjson.OPT_SORT_KEYS)
    )


def _submit_dry_run(repo_root: pathlib.Path, project: str = "trace-proj"):
    """Run a real dry-run submit and also persist the escalation locally."""
    client = GatewayClient(repo_root=repo_root)
    er = _make_escalation(project=project)
    er_payload = er.model_dump(mode="json")
    er_hash = canonical_hash(er_payload)
    er_id = f"er-{er_hash[:16]}"
    _write_escalation_locally(repo_root, er_payload, er_id)
    frontier = client.submit(er, dry_run=True)
    return er, er_id, er_hash, frontier


# ---------------------------------------------------------------------------
# 1. Happy chain — all MATCH
# ---------------------------------------------------------------------------

def test_trace_happy_chain_all_hashes_match(tmp_path: pathlib.Path):
    _er, er_id, er_hash, _frontier = _submit_dry_run(tmp_path)

    result = trace(er_id, tmp_path)

    assert result.exit_code == 0
    assert result.chain_intact is True
    stages = [r.stage for r in result.rows]
    assert stages == ["escalation", "admission", "frontier"]

    esc_row, adm_row, front_row = result.rows
    assert esc_row.linkage_status == "N/A"
    assert esc_row.hash == er_hash
    assert adm_row.linkage_status == "MATCH"
    assert front_row.linkage_status == "MATCH"
    # Admission receipt_hash referenced by frontier is in admission row's hash field.
    assert adm_row.hash != ""
    assert front_row.hash != ""


# ---------------------------------------------------------------------------
# 2. External escalation — not present locally, chain still valid
# ---------------------------------------------------------------------------

def test_trace_external_escalation_returns_exit_zero(tmp_path: pathlib.Path):
    client = GatewayClient(repo_root=tmp_path)
    er = _make_escalation()
    er_hash = canonical_hash(er.model_dump(mode="json"))
    er_id = f"er-{er_hash[:16]}"
    # Deliberately do NOT persist the escalation locally.
    client.submit(er, dry_run=True)

    result = trace(er_id, tmp_path)

    assert result.exit_code == 0
    assert result.chain_intact is True
    esc_row, adm_row, front_row = result.rows
    assert esc_row.linkage_status == "EXTERNAL_ESCALATION"
    assert esc_row.timestamp == "(external)"
    # Admission row cannot compute a MATCH without local ER; declared EXTERNAL.
    assert adm_row.linkage_status == "EXTERNAL_ESCALATION"
    # Frontier still binds cleanly to admission via receipt_hash.
    assert front_row.linkage_status == "MATCH"


# ---------------------------------------------------------------------------
# 3. Hash mismatch — exit 1
# ---------------------------------------------------------------------------

def test_trace_hash_mismatch_returns_exit_one(tmp_path: pathlib.Path):
    _er, er_id, _er_hash, _frontier = _submit_dry_run(tmp_path)

    # Forge the local escalation file to a different payload that keeps the
    # same id lookup (we rewrite the file under the er_id filename with
    # tampered content). The admission's recorded content_hash no longer
    # matches the on-disk payload's canonical_hash.
    esc_file = tmp_path / ".ollarma" / "escalations" / f"{er_id}.json"
    tampered = orjson.loads(esc_file.read_bytes())
    tampered["reason_detail"] = "TAMPERED PAYLOAD — different bytes"
    esc_file.write_bytes(orjson.dumps(tampered, option=orjson.OPT_SORT_KEYS))

    result = trace(er_id, tmp_path)

    assert result.exit_code == 1
    assert result.chain_intact is False
    # escalation row computes hash from tampered payload; admission row flags MISMATCH.
    adm_row = result.rows[1]
    assert adm_row.linkage_status == "MISMATCH"
    assert "HASH_MISMATCH" in adm_row.linkage_detail


# ---------------------------------------------------------------------------
# 4. Missing admission — exit 1
# ---------------------------------------------------------------------------

def test_trace_missing_admission_returns_exit_one(tmp_path: pathlib.Path):
    # Hand-craft a frontier receipt with no matching admission entry.
    gateway_dir = tmp_path / ".ollarma" / "gateway"
    gateway_dir.mkdir(parents=True)
    er = _make_escalation()
    er_payload = er.model_dump(mode="json")
    er_hash = canonical_hash(er_payload)
    er_id = f"er-{er_hash[:16]}"
    _write_escalation_locally(tmp_path, er_payload, er_id)

    # Write a frontier receipt directly via the store (chain-valid within its
    # own stream) but never write any admission entry.
    store = GatewayReceiptStore(tmp_path)
    frontier = FrontierReceipt(
        escalation_receipt_id=er_id,
        admission_receipt_hash="a" * 64,  # fake — no admission backs it
        provider="anthropic",
        model_id="claude-test",
        status="failed",
        reason_code=GatewayReasonCode.PROVIDER_AUTH_FAILED.value,
    )
    store.append_receipt(frontier)

    result = trace(er_id, tmp_path)

    assert result.exit_code == 1
    assert result.chain_intact is False
    adm_row = result.rows[1]
    front_row = result.rows[2]
    assert adm_row.linkage_status == "MISSING_ADMISSION"
    assert front_row.linkage_status == "MISSING_ADMISSION"


# ---------------------------------------------------------------------------
# 5. Disabled-status receipt — still a valid chain
# ---------------------------------------------------------------------------

def test_trace_disabled_status_is_valid_chain(tmp_path: pathlib.Path):
    er = _make_escalation()
    er_payload = er.model_dump(mode="json")
    er_hash = canonical_hash(er_payload)
    er_id = f"er-{er_hash[:16]}"
    _write_escalation_locally(tmp_path, er_payload, er_id)

    store = GatewayReceiptStore(tmp_path)
    admission = store.append_admission(
        GatewayAdmissionEntry(
            admission_id="adm-disabled-001",
            escalation_receipt_id=er_id,
            escalation_receipt_content_hash=er_hash,
            outcome="disabled",
            reason_code=GatewayReasonCode.GATEWAY_DISABLED.value,
            reason_detail="gateway disabled by operator",
            project=er.project,
        )
    )
    store.append_receipt(
        FrontierReceipt(
            escalation_receipt_id=er_id,
            admission_receipt_hash=admission.receipt_hash,
            provider="anthropic",
            model_id="claude-disabled",
            status="disabled",
            reason_code=GatewayReasonCode.GATEWAY_DISABLED.value,
        )
    )

    result = trace(er_id, tmp_path)

    assert result.exit_code == 0
    assert result.chain_intact is True
    assert result.rows[1].linkage_status == "MATCH"
    assert result.rows[2].linkage_status == "MATCH"


# ---------------------------------------------------------------------------
# 6. Reject chain — admission reject + frontier failed with shared reason_code
# ---------------------------------------------------------------------------

def test_trace_reject_chain_exits_zero(tmp_path: pathlib.Path):
    er = _make_escalation()
    er_payload = er.model_dump(mode="json")
    er_hash = canonical_hash(er_payload)
    er_id = f"er-{er_hash[:16]}"
    _write_escalation_locally(tmp_path, er_payload, er_id)

    store = GatewayReceiptStore(tmp_path)
    admission = store.append_admission(
        GatewayAdmissionEntry(
            admission_id="adm-reject-001",
            escalation_receipt_id=er_id,
            escalation_receipt_content_hash=er_hash,
            outcome="reject",
            reason_code=GatewayReasonCode.PROJECT_NOT_ALLOWED.value,
            reason_detail="project not in allowlist",
            project=er.project,
        )
    )
    store.append_receipt(
        FrontierReceipt(
            escalation_receipt_id=er_id,
            admission_receipt_hash=admission.receipt_hash,
            provider="",
            model_id="",
            status="failed",
            reason_code=GatewayReasonCode.PROJECT_NOT_ALLOWED.value,
        )
    )

    result = trace(er_id, tmp_path)

    assert result.exit_code == 0
    assert result.chain_intact is True
    # All three rows MATCH (reject is a legitimate chain state).
    assert result.rows[1].linkage_status == "MATCH"
    assert result.rows[2].linkage_status == "MATCH"


# ---------------------------------------------------------------------------
# 7. (bonus) --repo-root override + plain-text fallback via capsys
# ---------------------------------------------------------------------------

def test_trace_respects_repo_root_override(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture,
):
    # Build a chain under an "external" repo root -- simulates sibling project.
    external_root = tmp_path / "sibling-project"
    external_root.mkdir()
    _er, er_id, _er_hash, _frontier = _submit_dry_run(external_root)

    # CliRunner does not connect a TTY -> render_table uses plain-ASCII path.
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["receipts", "trace", er_id, "--repo-root", str(external_root)],
    )

    assert res.exit_code == 0, res.stdout
    # Plain-text path uses ASCII borders (`+`, `|`, `-`) -- no Rich box chars.
    assert "|" in res.stdout
    assert "Stage" in res.stdout
    assert "Linkage" in res.stdout
    assert er_id in res.stdout or "er-" in res.stdout
    assert "chain_intact=True" in res.stdout
