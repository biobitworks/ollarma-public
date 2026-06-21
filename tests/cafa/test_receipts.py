"""Tests for D6 verdict-provenance receipts (hash-chained, deterministic)."""
from pathlib import Path

import pytest

from ollarma.cafa.go_ontology import GoDag
from ollarma.cafa import Prediction, run_all, propagate_to_root
from ollarma.cafa.receipts import (
    build_receipts,
    verify_chain,
    receipts_to_jsonl,
    GENESIS,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mini_go.obo"


@pytest.fixture(scope="module")
def dag():
    return GoDag.from_obo(FIXTURE)


def _preds(dag):
    return propagate_to_root([Prediction("P1", "GO:0044237", 0.9)], dag)


def test_receipt_per_verdict_and_chain_valid(dag):
    preds = _preds(dag)
    verdicts = run_all(preds, dag)
    recs = build_receipts(target="P1", preds=preds, verdicts=verdicts)
    assert len(recs) == len(verdicts)
    assert recs[0].prev_hash == GENESIS
    assert all(r.receipt_hash.startswith("sha256:") for r in recs)
    assert verify_chain(recs) is True


def test_chain_links_prev_to_receipt(dag):
    preds = _preds(dag)
    recs = build_receipts(target="P1", preds=preds, verdicts=run_all(preds, dag))
    for i in range(1, len(recs)):
        assert recs[i].prev_hash == recs[i - 1].receipt_hash


def test_tamper_detected(dag):
    preds = _preds(dag)
    recs = build_receipts(target="P1", preds=preds, verdicts=run_all(preds, dag))
    recs[1].verdict = "block"  # tamper to a different value without recomputing hash
    assert verify_chain(recs) is False


def test_deterministic_same_input_same_hash(dag):
    preds = _preds(dag)
    a = build_receipts(target="P1", preds=preds, verdicts=run_all(preds, dag))
    b = build_receipts(target="P1", preds=preds, verdicts=run_all(preds, dag))
    assert [r.receipt_hash for r in a] == [r.receipt_hash for r in b]


def test_open_calibration_downgrades_block_authority(dag):
    # a block verdict with closed calibration stays trusted; the helper downgrades
    # open/unknown — exercise via the authority field on a blocking antibody.
    from ollarma.cafa.go_guardrails import go_score_range
    bad = [Prediction("P1", "GO:0005488", 0.0)]  # score=0 -> block
    v = go_score_range(bad)
    assert v.status == "block"
    recs = build_receipts(target="P1", preds=bad, verdicts=[v])
    # rule_floor is deterministic/closed -> a real block is authoritative
    assert recs[0].authority == "trusted"


def test_jsonl_roundtrip_shape(dag):
    preds = _preds(dag)
    recs = build_receipts(target="P1", preds=preds, verdicts=run_all(preds, dag))
    lines = receipts_to_jsonl(recs).splitlines()
    assert len(lines) == len(recs)
    import json
    row = json.loads(lines[0])
    assert {"target", "antibody", "verdict", "authority", "input_hash",
            "receipt_hash", "prev_hash"} <= set(row)
