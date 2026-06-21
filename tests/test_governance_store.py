"""Tests for GovernanceStore (Phase 37 SAFE-04)."""
import pytest
from ollarma.governance_store import (
    GovernanceStore,
    GovernancePayloadTooLargeError,
    MAX_PAYLOAD_BYTES,
)


def test_store_returns_digest(tmp_path):
    """GovernanceStore.store returns a content-addressed digest string."""
    store = GovernanceStore(tmp_path / "governance")
    digest = store.store({"run_id": "test-01", "status": "ok"})
    assert isinstance(digest, str)
    assert len(digest) == 64  # SHA-256 hex


def test_store_idempotent(tmp_path):
    """Storing the same payload twice returns the same digest."""
    store = GovernanceStore(tmp_path / "governance")
    payload = {"run_id": "test-02", "status": "ok"}
    d1 = store.store(payload)
    d2 = store.store(payload)
    assert d1 == d2


def test_store_creates_file(tmp_path):
    """GovernanceStore creates payload.json at correct path."""
    base = tmp_path / "governance"
    store = GovernanceStore(base)
    digest = store.store({"x": 1})
    assert (base / digest / "payload.json").exists()


def test_load_returns_payload(tmp_path):
    """GovernanceStore.load retrieves stored payload."""
    store = GovernanceStore(tmp_path / "governance")
    payload = {"run_id": "test-03", "value": 42}
    digest = store.store(payload)
    loaded = store.load(digest)
    assert loaded["run_id"] == "test-03"
    assert loaded["value"] == 42


def test_exists_true_after_store(tmp_path):
    """GovernanceStore.exists returns True after storing."""
    store = GovernanceStore(tmp_path / "governance")
    digest = store.store({"y": 2})
    assert store.exists(digest) is True


def test_exists_false_for_unknown(tmp_path):
    """GovernanceStore.exists returns False for unknown digest."""
    store = GovernanceStore(tmp_path / "governance")
    assert store.exists("0" * 64) is False


def test_payload_too_large_raises(tmp_path):
    """GovernanceStore.store raises GovernancePayloadTooLargeError for >64KB payload."""
    store = GovernanceStore(tmp_path / "governance")
    large_payload = {"data": "x" * (MAX_PAYLOAD_BYTES + 1)}
    with pytest.raises(GovernancePayloadTooLargeError):
        store.store(large_payload)


def test_run_receipt_has_governance_refs_field():
    """RunReceipt has governance_refs field defaulting to empty tuple."""
    from ollarma.run_ledger import RunReceipt
    receipt = RunReceipt(
        run_id="r1", stage="execute", step_id="s1",
        task_or_command="cmd", lane="test", status="ok",
    )
    assert receipt.governance_refs == ()
