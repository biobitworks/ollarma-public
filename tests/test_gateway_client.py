"""test_gateway_client.py -- GatewayClient unit tests (Phase 57-02).

Task 1 scope:
- submit(valid_escalation_receipt, dry_run=True) returns FrontierReceipt with status="dry_run".
- Input validation: non-EscalationReceipt raises GatewayInputError (distinct from
  bare Exception) and writes a reject admission entry to the audit stream.
- Non-dry-run branch raises NotImplementedError loudly (Phase 57 boundary to 59).
- Narrow exception taxonomy: GatewayError base, GatewayInputError + GatewayDisabledError.
- Zero `except Exception` in the module (DEBT-10 structural check).

All tests exercise the REAL GatewayClient + REAL GatewayReceiptStore + REAL file I/O
via tmp_path. No monkeypatching of internals — this is the anti-pattern Phase 55 WR-01
explicitly corrected.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from ollarma.escalation import ReasonCode, build_escalation_receipt
from ollarma.gateway import (
    FrontierReceipt,
    GatewayAdmissionEntry,
    GatewayReasonCode,
    GatewayReceiptStore,
)
from ollarma.gateway_client import (
    GatewayClient,
    GatewayDisabledError,
    GatewayError,
    GatewayInputError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def valid_escalation_receipt():
    return build_escalation_receipt(
        project="test-proj",
        lane="ollarma-default",
        task_class="chat",
        reason_code=ReasonCode.SWAP_DEGRADED,
        reason_detail="swap 2100 MB > 512 MB threshold",
        next_action="frontier_or_human",
    )


@pytest.fixture
def client(tmp_path: pathlib.Path) -> GatewayClient:
    return GatewayClient(repo_root=tmp_path)


# ---------------------------------------------------------------------------
# Exception taxonomy (narrow types — DEBT-10)
# ---------------------------------------------------------------------------

def test_gateway_error_is_base_class():
    assert issubclass(GatewayInputError, GatewayError)
    assert issubclass(GatewayDisabledError, GatewayError)
    assert issubclass(GatewayError, Exception)
    assert not issubclass(GatewayError, ValueError)
    assert not issubclass(GatewayError, TypeError)


def test_gateway_input_error_and_disabled_error_are_distinct():
    assert GatewayInputError is not GatewayDisabledError
    assert not issubclass(GatewayInputError, GatewayDisabledError)
    assert not issubclass(GatewayDisabledError, GatewayInputError)


def test_no_bare_except_exception_in_module():
    """Structural check: zero `except Exception` in gateway_client.py."""
    src = pathlib.Path("src/ollarma/gateway_client.py").read_text()
    pattern = re.compile(r"^\s*except\s+Exception\b", re.MULTILINE)
    matches = pattern.findall(src)
    assert matches == [], f"Found bare `except Exception` in gateway_client.py: {matches}"


def test_gateway_input_error_is_single_class():
    """WR-02 regression: GatewayInputError must be the SAME class whether
    imported from ollarma.service or ollarma.gateway_client. A cross-module
    catch in downstream consumers (e.g. substrata) silently fails if the
    service-side class is a separate Exception subclass.
    """
    from ollarma.service import GatewayInputError as service_err
    from ollarma.gateway_client import GatewayInputError as client_err
    assert service_err is client_err, (
        "WR-02: GatewayInputError must be the same class across both import "
        "paths. service.py should re-export from gateway_client."
    )


# ---------------------------------------------------------------------------
# Dry-run happy path
# ---------------------------------------------------------------------------

def test_submit_dry_run_returns_populated_receipt(
    client: GatewayClient, valid_escalation_receipt
):
    receipt = client.submit(valid_escalation_receipt, dry_run=True)
    assert isinstance(receipt, FrontierReceipt)
    assert receipt.status == "dry_run"
    assert receipt.reason_code == GatewayReasonCode.DRY_RUN.value
    assert receipt.dry_run is True
    assert receipt.prompt_tokens == 0
    assert receipt.response_tokens == 0
    assert receipt.latency_ms == 0
    assert receipt.receipt_hash != ""
    assert len(receipt.receipt_hash) == 64


def test_submit_dry_run_default_flag_is_true(
    client: GatewayClient, valid_escalation_receipt
):
    receipt = client.submit(valid_escalation_receipt)
    assert receipt.status == "dry_run"
    assert receipt.dry_run is True


# ---------------------------------------------------------------------------
# Input validation -- GatewayInputError
# ---------------------------------------------------------------------------

def test_submit_none_raises_gateway_input_error(client: GatewayClient):
    with pytest.raises(GatewayInputError) as exc_info:
        client.submit(None, dry_run=True)  # type: ignore[arg-type]
    msg = str(exc_info.value)
    assert "EscalationReceipt" in msg
    assert "NoneType" in msg


def test_submit_dict_raises_gateway_input_error(client: GatewayClient):
    with pytest.raises(GatewayInputError) as exc_info:
        client.submit({"not": "a receipt"}, dry_run=True)  # type: ignore[arg-type]
    assert "EscalationReceipt" in str(exc_info.value)
    assert "dict" in str(exc_info.value)


def test_submit_bare_string_raises_gateway_input_error(client: GatewayClient):
    with pytest.raises(GatewayInputError) as exc_info:
        client.submit("bare string prompt", dry_run=True)  # type: ignore[arg-type]
    assert "EscalationReceipt" in str(exc_info.value)


def test_gateway_input_error_is_not_type_error_or_value_error(client: GatewayClient):
    with pytest.raises(GatewayInputError):
        client.submit(None, dry_run=True)  # type: ignore[arg-type]
    try:
        client.submit(None, dry_run=True)  # type: ignore[arg-type]
    except Exception as e:
        assert isinstance(e, GatewayInputError)
        assert isinstance(e, GatewayError)


def test_rejected_submission_writes_admission_entry_not_receipt(
    tmp_path: pathlib.Path,
):
    client = GatewayClient(repo_root=tmp_path)
    store = GatewayReceiptStore(tmp_path)

    with pytest.raises(GatewayInputError):
        client.submit(None, dry_run=True)  # type: ignore[arg-type]

    admissions = store.load_admissions()
    assert len(admissions) == 1
    entry = admissions[0]
    assert isinstance(entry, GatewayAdmissionEntry)
    assert entry.outcome == "reject"
    assert entry.reason_code == GatewayReasonCode.REJECT_INVALID_RECEIPT.value
    assert "NoneType" in entry.reason_detail
    assert entry.admission_id.startswith("adm-")

    receipts = store.load_receipts()
    assert receipts == []


def test_multiple_rejects_produce_distinct_admission_ids(
    tmp_path: pathlib.Path,
):
    client = GatewayClient(repo_root=tmp_path)
    store = GatewayReceiptStore(tmp_path)

    for bad in (None, {"not": "a receipt"}, "bare string"):
        with pytest.raises(GatewayInputError):
            client.submit(bad, dry_run=True)  # type: ignore[arg-type]

    admissions = store.load_admissions()
    assert len(admissions) == 3
    admission_ids = {a.admission_id for a in admissions}
    assert len(admission_ids) == 3, "admission_ids must be per-call unique"
    for entry in admissions:
        assert entry.outcome == "reject"
        assert entry.reason_code == GatewayReasonCode.REJECT_INVALID_RECEIPT.value


# ---------------------------------------------------------------------------
# Real-provider branch: dispatch requires prompt + virtual_key_bytes (Phase 59)
# ---------------------------------------------------------------------------

def test_submit_dry_run_false_without_prompt_raises_input_error(
    client: GatewayClient, valid_escalation_receipt
):
    """Phase 59: the previous NotImplementedError boundary is replaced by a
    GatewayInputError when the required 'prompt' parameter is missing.
    """
    from ollarma.gateway_client import GatewayInputError  # noqa: PLC0415
    with pytest.raises(GatewayInputError) as exc_info:
        client.submit(valid_escalation_receipt, dry_run=False)
    assert "prompt" in str(exc_info.value)


def test_submit_dry_run_false_without_prompt_does_not_write_receipt(
    tmp_path: pathlib.Path, valid_escalation_receipt
):
    from ollarma.gateway_client import GatewayInputError  # noqa: PLC0415
    client = GatewayClient(repo_root=tmp_path)
    store = GatewayReceiptStore(tmp_path)
    with pytest.raises(GatewayInputError):
        client.submit(valid_escalation_receipt, dry_run=False)
    assert store.load_admissions() == []
    assert store.load_receipts() == []


# ---------------------------------------------------------------------------
# Store injection + default construction
# ---------------------------------------------------------------------------

def test_client_accepts_explicit_store_instance(
    tmp_path: pathlib.Path, valid_escalation_receipt
):
    store = GatewayReceiptStore(tmp_path)
    client = GatewayClient(repo_root=tmp_path, store=store)
    receipt = client.submit(valid_escalation_receipt, dry_run=True)
    assert receipt.status == "dry_run"
    assert len(store.load_admissions()) == 1
    assert len(store.load_receipts()) == 1


def test_client_default_store_uses_repo_root(
    tmp_path: pathlib.Path, valid_escalation_receipt
):
    client = GatewayClient(repo_root=tmp_path)
    client.submit(valid_escalation_receipt, dry_run=True)
    assert (tmp_path / ".ollarma" / "gateway" / "admissions.jsonl").exists()
    assert (tmp_path / ".ollarma" / "gateway" / "receipts.jsonl").exists()
