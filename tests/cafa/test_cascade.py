"""Tests for D4 cascade orchestration — local-first routing, zero frontier."""
from pathlib import Path

import pytest

from ollarma.cafa.go_ontology import GoDag
from ollarma.cafa import Prediction, verify_go_submission, screen_text_claim
from ollarma.cafa.receipts import verify_chain

FIXTURE = Path(__file__).parent / "fixtures" / "mini_go.obo"


@pytest.fixture(scope="module")
def dag():
    return GoDag.from_obo(FIXTURE)


def test_go_submission_clean_path_no_network(dag):
    # already-propagated, in-range -> clean
    preds = [Prediction("P1", "GO:0005488", 0.7)]  # binding (child of MF root only)
    res = verify_go_submission("P1", preds, dag, auto_propagate=False)
    assert res.status == "clean"
    assert res.network_calls == 0 and res.frontier_calls == 0
    assert verify_chain(res.receipts) is True


def test_go_submission_auto_propagates_and_marks_fixed(dag):
    preds = [Prediction("P1", "GO:0044237", 0.9)]  # deep -> needs ancestors
    res = verify_go_submission("P1", preds, dag)
    assert res.status == "fixed"
    assert len(res.propagated) > 1
    assert all(v.status in ("pass", "flag") for v in res.verdicts)


def test_go_submission_blocked_on_bad_score(dag):
    preds = [Prediction("P1", "GO:0005488", 1.5)]  # out of range
    res = verify_go_submission("P1", preds, dag)
    assert res.status == "blocked"
    assert any(v.antibody == "go_score_range" and v.status == "block"
               for v in res.verdicts)


def test_text_format_block_stops_no_escalation(dag):
    res = screen_text_claim("P1", "bad\ttext with tab")
    assert any(v.antibody == "text_format_contract" and v.status == "block"
               for v in res.floor_verdicts)
    assert res.needs_escalation is False
    assert res.escalation_request is None


def test_text_passes_floor_emits_local_escalation_request(dag):
    text = ("P53 is a transcription factor that induces apoptosis upon DNA "
            "damage (PMID: 1234567).")
    res = screen_text_claim("P04637", text)
    assert res.needs_escalation is True
    req = res.escalation_request
    assert req["to_lane"] == "antigence"
    assert "claim_entailment" in req["antibodies"]
    assert req["cited_pmids"]  # PMID extracted
    assert req["frontier_allowed"] is False  # local-first: no frontier here
    assert res.frontier_calls == 0


def test_missing_pmid_flags_but_still_escalates(dag):
    res = screen_text_claim("P1", "This protein does something important.")
    pmid_v = [v for v in res.floor_verdicts if v.antibody == "pmid_citation_present"][0]
    assert pmid_v.status == "flag"
    assert res.needs_escalation is True  # entailment lane still decides
