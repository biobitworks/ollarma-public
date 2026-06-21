"""test_gateway_receipt_store.py -- GatewayReceiptStore contract tests (Phase 57-01).

Covers:
- Split-stream hash-chain integrity (admissions + receipts independent).
- End-to-end verify_chain walk.
- Tamper detection (mutating line 1 breaks the chain).
- Forward-compat: unknown top-level field in a stored record loads cleanly.
- Missing required field rejected with GatewayStoreError on load.
- Atomic append: line-aligned invariant on disk; concurrent-thread interleave safety.
- Streams are genuinely independent (writing admissions doesn't touch receipts).

These tests exercise the REAL code path — no monkeypatching of canonical_hash
or internal methods (per v4.5 retro-review + Phase 55 WR-01 anti-pattern).
"""
from __future__ import annotations

import pathlib
import threading
from decimal import Decimal

import orjson
import pytest

from ollarma.evidence import GENESIS_PARENT_HASH
from ollarma.gateway import (
    FrontierReceipt,
    GatewayAdmissionEntry,
    GatewayReasonCode,
    GatewayReceiptStore,
    GatewayStoreError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_admission(
    n: int,
    *,
    outcome: str = "accept",
    reason_code: str | None = None,
) -> GatewayAdmissionEntry:
    return GatewayAdmissionEntry(
        admission_id=f"adm-{n}",
        escalation_receipt_id=f"esc-{n}",
        escalation_receipt_content_hash="a" * 64,
        outcome=outcome,
        reason_code=reason_code,
        reason_detail="",
        project="test-project",
    )


def _make_receipt(n: int, *, status: str = "succeeded") -> FrontierReceipt:
    return FrontierReceipt(
        escalation_receipt_id=f"esc-{n}",
        admission_receipt_hash="b" * 64,
        provider="anthropic",
        model_id="claude-opus-4",
        status=status,
        prompt_tokens=10,
        response_tokens=20,
        cost_usd=Decimal("0.0032"),
        latency_ms=150,
    )


# ---------------------------------------------------------------------------
# Chain integrity
# ---------------------------------------------------------------------------

def test_admissions_chain_verifies(tmp_path: pathlib.Path):
    store = GatewayReceiptStore(repo_root=tmp_path)
    first = store.append_admission(_make_admission(1))
    second = store.append_admission(_make_admission(2))

    assert first.parent_hash == GENESIS_PARENT_HASH
    assert first.receipt_hash != ""
    assert second.parent_hash == first.receipt_hash
    assert second.receipt_hash != first.receipt_hash

    tail = store.verify_chain("admissions")
    assert tail == second.receipt_hash


def test_receipts_chain_verifies(tmp_path: pathlib.Path):
    store = GatewayReceiptStore(repo_root=tmp_path)
    first = store.append_receipt(_make_receipt(1))
    second = store.append_receipt(_make_receipt(2))

    assert first.parent_hash == GENESIS_PARENT_HASH
    assert second.parent_hash == first.receipt_hash
    assert store.verify_chain("receipts") == second.receipt_hash


def test_streams_are_independent(tmp_path: pathlib.Path):
    """Admissions chain and receipts chain do not share state."""
    store = GatewayReceiptStore(repo_root=tmp_path)

    # Write two admissions first
    a1 = store.append_admission(_make_admission(1))
    a2 = store.append_admission(_make_admission(2))

    # Receipts chain starts fresh (GENESIS), not at admissions tail
    r1 = store.append_receipt(_make_receipt(1))
    assert r1.parent_hash == GENESIS_PARENT_HASH
    assert r1.parent_hash != a2.receipt_hash

    r2 = store.append_receipt(_make_receipt(2))
    assert r2.parent_hash == r1.receipt_hash

    assert store.verify_chain("admissions") == a2.receipt_hash
    assert store.verify_chain("receipts") == r2.receipt_hash


def test_write_to_one_stream_does_not_touch_other(tmp_path: pathlib.Path):
    store = GatewayReceiptStore(repo_root=tmp_path)
    store.append_admission(_make_admission(1))
    admissions_path = tmp_path / ".ollarma" / "gateway" / "admissions.jsonl"
    receipts_path = tmp_path / ".ollarma" / "gateway" / "receipts.jsonl"
    assert admissions_path.exists()
    assert not receipts_path.exists()

    store.append_receipt(_make_receipt(1))
    assert receipts_path.exists()
    # Admissions byte-length unchanged
    admissions_size_after_first = admissions_path.stat().st_size
    store.append_receipt(_make_receipt(2))
    assert admissions_path.stat().st_size == admissions_size_after_first


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------

def test_tampering_with_parent_hash_breaks_chain(tmp_path: pathlib.Path):
    store = GatewayReceiptStore(repo_root=tmp_path)
    store.append_admission(_make_admission(1))
    store.append_admission(_make_admission(2))

    path = tmp_path / ".ollarma" / "gateway" / "admissions.jsonl"
    lines = path.read_bytes().splitlines()
    assert len(lines) == 2
    # Tamper line 1's parent_hash
    first = orjson.loads(lines[0])
    first["parent_hash"] = "f" * 64
    lines[0] = orjson.dumps(first, option=orjson.OPT_SORT_KEYS)
    path.write_bytes(b"\n".join(lines) + b"\n")

    with pytest.raises(GatewayStoreError):
        store.verify_chain("admissions")


def test_tampering_with_receipt_body_breaks_chain(tmp_path: pathlib.Path):
    store = GatewayReceiptStore(repo_root=tmp_path)
    store.append_receipt(_make_receipt(1))

    path = tmp_path / ".ollarma" / "gateway" / "receipts.jsonl"
    lines = path.read_bytes().splitlines()
    first = orjson.loads(lines[0])
    # Mutate a body field -- receipt_hash no longer matches recomputation
    first["latency_ms"] = 999999
    lines[0] = orjson.dumps(first, option=orjson.OPT_SORT_KEYS)
    path.write_bytes(b"\n".join(lines) + b"\n")

    with pytest.raises(GatewayStoreError):
        store.verify_chain("receipts")


# ---------------------------------------------------------------------------
# Forward-compat + failure modes
# ---------------------------------------------------------------------------

def test_unknown_field_tolerated_on_load(tmp_path: pathlib.Path):
    """Forward-compat: an unknown top-level field doesn't crash loaders."""
    store = GatewayReceiptStore(repo_root=tmp_path)
    entry = store.append_admission(_make_admission(1))

    path = tmp_path / ".ollarma" / "gateway" / "admissions.jsonl"
    # Rewrite the file with a future_field added.
    record = entry.model_dump(mode="json")
    record["future_field_x"] = 1
    path.write_bytes(
        orjson.dumps(record, option=orjson.OPT_SORT_KEYS) + b"\n"
    )
    # Load should succeed, unknown field simply ignored.
    loaded = store.load_admissions()
    assert len(loaded) == 1
    assert loaded[0].admission_id == "adm-1"
    # Chain also verifies because unknown fields are IGNORED in canonical hash
    # recomputation only if they weren't in the original hash. Since we rewrote
    # with a new field, the recomputed hash WILL differ. That's by design --
    # tamper detection. So verify_chain should detect the mutation.
    with pytest.raises(GatewayStoreError):
        store.verify_chain("admissions")


def test_missing_required_field_rejected_on_load(tmp_path: pathlib.Path):
    store = GatewayReceiptStore(repo_root=tmp_path)
    store.append_admission(_make_admission(1))
    path = tmp_path / ".ollarma" / "gateway" / "admissions.jsonl"

    # Rewrite without the required escalation_receipt_id field
    bad = {
        "admission_id": "adm-1",
        # escalation_receipt_id missing
        "escalation_receipt_content_hash": "a" * 64,
        "outcome": "accept",
        "reason_code": None,
        "reason_detail": "",
        "project": "p",
        "schema_version": 1,
        "created_at": "2026-04-19T00:00:00Z",
        "parent_hash": GENESIS_PARENT_HASH,
        "receipt_hash": "x" * 64,
    }
    path.write_bytes(orjson.dumps(bad, option=orjson.OPT_SORT_KEYS) + b"\n")

    with pytest.raises(GatewayStoreError):
        store.load_admissions()


def test_corrupt_json_line_rejected(tmp_path: pathlib.Path):
    store = GatewayReceiptStore(repo_root=tmp_path)
    path = tmp_path / ".ollarma" / "gateway" / "admissions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not valid json\n")

    with pytest.raises(GatewayStoreError):
        store.load_admissions()


def test_empty_stream_returns_genesis(tmp_path: pathlib.Path):
    store = GatewayReceiptStore(repo_root=tmp_path)
    assert store.verify_chain("admissions") == GENESIS_PARENT_HASH
    assert store.verify_chain("receipts") == GENESIS_PARENT_HASH
    assert store.load_admissions() == []
    assert store.load_receipts() == []


def test_unknown_stream_name_rejected(tmp_path: pathlib.Path):
    store = GatewayReceiptStore(repo_root=tmp_path)
    with pytest.raises(GatewayStoreError):
        store.verify_chain("other")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Atomic append + concurrency
# ---------------------------------------------------------------------------

def test_file_is_line_aligned_no_embedded_newlines(tmp_path: pathlib.Path):
    """Every record terminates in \\n and contains no mid-record newline."""
    store = GatewayReceiptStore(repo_root=tmp_path)
    for i in range(1, 11):
        store.append_receipt(_make_receipt(i))

    path = tmp_path / ".ollarma" / "gateway" / "receipts.jsonl"
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.split(b"\n")
    # Trailing split produces an empty element after final \n
    assert lines[-1] == b""
    payload_lines = lines[:-1]
    assert len(payload_lines) == 10
    for line in payload_lines:
        assert b"\n" not in line
        # Each is valid JSON
        orjson.loads(line)


def test_concurrent_append_preserves_chain(tmp_path: pathlib.Path):
    """Two threads writing concurrently produce a valid chain with correct
    line count and no interleaving corruption."""
    store = GatewayReceiptStore(repo_root=tmp_path)
    per_thread = 20
    threads_count = 4
    total_expected = per_thread * threads_count

    def worker(thread_idx: int) -> None:
        for i in range(per_thread):
            store.append_admission(
                _make_admission(thread_idx * 1000 + i)
            )

    threads = [
        threading.Thread(target=worker, args=(t,)) for t in range(threads_count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    path = tmp_path / ".ollarma" / "gateway" / "admissions.jsonl"
    lines = path.read_bytes().splitlines()
    assert len(lines) == total_expected

    # Chain must verify end-to-end — proves the lock serialized parent_hash
    # reads with writes.
    tail = store.verify_chain("admissions")
    assert tail != GENESIS_PARENT_HASH
    # Every line is valid JSON with required fields
    loaded = store.load_admissions()
    assert len(loaded) == total_expected


def test_embedded_newline_rejected_by_append(tmp_path: pathlib.Path):
    """Defensive: canonical JSON never contains raw newlines, but if it did,
    the append path should fail fast rather than corrupt the file."""
    store = GatewayReceiptStore(repo_root=tmp_path)
    # Use the internal helper to confirm the guard fires. Legitimate callers
    # never produce embedded \n via model_dump + orjson.dumps.
    path = tmp_path / ".ollarma" / "gateway" / "admissions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(GatewayStoreError):
        store._atomic_append_line(path, b"line1\nline2")


# ---------------------------------------------------------------------------
# Returned materialized entry shape
# ---------------------------------------------------------------------------

def test_append_returns_materialized_entry(tmp_path: pathlib.Path):
    store = GatewayReceiptStore(repo_root=tmp_path)
    admission = store.append_admission(
        _make_admission(1, outcome="reject", reason_code=GatewayReasonCode.REJECT_INVALID_RECEIPT.value)
    )
    assert admission.receipt_hash != ""
    assert admission.parent_hash == GENESIS_PARENT_HASH
    assert admission.outcome == "reject"
    assert admission.reason_code == "REJECT_INVALID_RECEIPT"


def test_load_returns_materialized_with_hashes(tmp_path: pathlib.Path):
    store = GatewayReceiptStore(repo_root=tmp_path)
    appended = store.append_receipt(_make_receipt(1))
    loaded = store.load_receipts()
    assert len(loaded) == 1
    assert loaded[0].receipt_hash == appended.receipt_hash
    assert loaded[0].parent_hash == GENESIS_PARENT_HASH
    # Decimal round-trips
    assert loaded[0].cost_usd == Decimal("0.0032")
