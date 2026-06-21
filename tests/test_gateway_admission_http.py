"""test_gateway_admission_http.py -- Phase 58-01 HTTP integration.

Covers the full ``POST /gateway/submit`` pipeline with admission enabled:

  1. Rejected project -> HTTP 403 + admission entry persisted BEFORE response
     + FrontierReceipt persisted + raw key bytes never appear in any
     admission entry, receipt, response body, or captured stdout/stderr.
  2. Unknown virtual_key -> HTTP 400 + admission entry + FrontierReceipt
     + audit chain intact across both streams.

Exercises the real Starlette app, real ``GatewayReceiptStore``, real config
loader. Monkeypatching is limited to ``_KEYCHAIN_LOOKUP`` (a deliberate test
seam per D-58-07) and ``monkeypatch.chdir(tmp_path)`` for repo isolation.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from starlette.testclient import TestClient

from ollarma import gateway_admission
from ollarma.escalation import ReasonCode, build_escalation_receipt
from ollarma.gateway import GatewayReceiptStore


_FAKE_SECRET = b"fake-secret-bytes"


def _valid_escalation_receipt_dict(project: str = "overwatch") -> dict:
    er = build_escalation_receipt(
        project=project,
        lane="local",
        task_class="code",
        reason_code=ReasonCode.SELECTION_MISSING,
        reason_detail="selection artifact is missing for test",
        next_action="frontier_or_human",
    )
    return json.loads(er.model_dump_json())


@pytest.fixture
def _enabled_repo(tmp_path, monkeypatch):
    """Isolated repo with gateway enabled + allowlist + vk registry."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".planning").mkdir()
    (repo / ".planning" / "config.json").write_text(
        json.dumps(
            {
                "features": {
                    "gateway": {
                        "enabled": True,
                        "allowlist": ["overwatch"],
                        "virtual_keys": [
                            {
                                "id": "vk_overwatch",
                                "keychain_service": "ollarma-test-overwatch",
                                "provider": "anthropic",
                            },
                        ],
                    },
                },
            }
        )
    )
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def _keychain_hit(monkeypatch):
    def _lookup(service_name: str) -> bytes:
        return _FAKE_SECRET
    monkeypatch.setattr(gateway_admission, "_KEYCHAIN_LOOKUP", _lookup)
    return _lookup


def _read_lines(path: pathlib.Path) -> list[bytes]:
    if not path.exists():
        return []
    return [line.rstrip(b"\n") for line in path.read_bytes().split(b"\n") if line]


class TestAdmissionHttpRejections:
    def test_project_not_in_allowlist_returns_403_with_audit_trail(
        self,
        _enabled_repo: pathlib.Path,
        _keychain_hit,
        capsys,
    ) -> None:
        from ollarma.http_api import app  # noqa: PLC0415

        body = {
            "escalation_receipt": _valid_escalation_receipt_dict(project="intruder"),
            "virtual_key_id": "vk_overwatch",
        }
        with TestClient(app) as client:
            resp = client.post("/gateway/submit", json=body)

        # Status code
        assert resp.status_code == 403
        payload = resp.json()
        assert payload["status"] == "failed"
        assert payload["reason_code"] == "PROJECT_NOT_ALLOWED"
        # Provider/model left empty (no routing decision was reached).
        assert payload["provider"] == ""
        assert payload["model_id"] == ""

        # Admission entry persisted BEFORE the response returned -- the file
        # must exist and contain exactly one line with outcome=reject.
        admissions_path = _enabled_repo / ".ollarma" / "gateway" / "admissions.jsonl"
        receipts_path = _enabled_repo / ".ollarma" / "gateway" / "receipts.jsonl"
        assert admissions_path.exists()
        assert receipts_path.exists()
        admission_lines = _read_lines(admissions_path)
        receipt_lines = _read_lines(receipts_path)
        assert len(admission_lines) == 1
        assert len(receipt_lines) == 1
        adm_record = json.loads(admission_lines[0])
        assert adm_record["outcome"] == "reject"
        assert adm_record["reason_code"] == "PROJECT_NOT_ALLOWED"
        assert adm_record["project"] == "intruder"

        # Raw-key isolation (I-06): the mocked secret must not appear in any
        # persisted byte, in the response body, or in pytest-captured I/O.
        secret_str = _FAKE_SECRET.decode()
        for raw in admission_lines + receipt_lines:
            assert _FAKE_SECRET not in raw
            assert secret_str.encode() not in raw
        assert secret_str not in resp.text
        captured = capsys.readouterr()
        assert secret_str not in captured.out
        assert secret_str not in captured.err

        # Chain integrity: receipts.jsonl line 1 links to admissions.jsonl
        # line 1's receipt_hash via admission_receipt_hash.
        rec = json.loads(receipt_lines[0])
        assert rec["admission_receipt_hash"] == adm_record["receipt_hash"]

        # Verify the receipt store's chain-walker agrees.
        store = GatewayReceiptStore(_enabled_repo)
        store.verify_chain("admissions")
        store.verify_chain("receipts")

    def test_unknown_virtual_key_returns_400_with_audit_trail(
        self,
        _enabled_repo: pathlib.Path,
        _keychain_hit,
    ) -> None:
        from ollarma.http_api import app  # noqa: PLC0415

        body = {
            "escalation_receipt": _valid_escalation_receipt_dict(project="overwatch"),
            "virtual_key_id": "vk_not_registered",
        }
        with TestClient(app) as client:
            resp = client.post("/gateway/submit", json=body)

        assert resp.status_code == 400
        payload = resp.json()
        assert payload["status"] == "failed"
        assert payload["reason_code"] == "VIRTUAL_KEY_UNKNOWN"

        admissions_path = _enabled_repo / ".ollarma" / "gateway" / "admissions.jsonl"
        receipts_path = _enabled_repo / ".ollarma" / "gateway" / "receipts.jsonl"
        admission_lines = _read_lines(admissions_path)
        receipt_lines = _read_lines(receipts_path)
        assert len(admission_lines) == 1
        assert len(receipt_lines) == 1
        adm = json.loads(admission_lines[0])
        assert adm["outcome"] == "reject"
        assert adm["reason_code"] == "VIRTUAL_KEY_UNKNOWN"
        assert adm["project"] == "overwatch"

        # Cross-stream linkage preserved.
        rec = json.loads(receipt_lines[0])
        assert rec["admission_receipt_hash"] == adm["receipt_hash"]

        # Hash chains still verify.
        store = GatewayReceiptStore(_enabled_repo)
        store.verify_chain("admissions")
        store.verify_chain("receipts")
