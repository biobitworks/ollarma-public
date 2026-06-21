"""Tests for the deterministic GO guardrail antibodies + ontology loader.

Pure-local, fixture-driven (tests/cafa/fixtures/mini_go.obo). No network, no model.
"""
from pathlib import Path

import pytest

from ollarma.cafa.go_ontology import GoDag
from ollarma.cafa import (
    Prediction,
    go_score_range,
    go_term_validity,
    go_term_cap,
    subontology_partition,
    go_propagation_consistency,
    propagate_to_root,
    run_all,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mini_go.obo"


@pytest.fixture(scope="module")
def dag() -> GoDag:
    return GoDag.from_obo(FIXTURE)


# --- ontology loader ------------------------------------------------------

def test_parse_counts_and_namespaces(dag):
    assert len(dag) == 11
    assert dag.namespace_of("GO:0044237") == "biological_process"
    assert dag.subontology_of("GO:0005488") == "MF"
    assert dag.subontology_of("GO:0005634") == "CC"


def test_alt_id_normalization(dag):
    assert dag.normalize("GO:0088888") == "GO:0099999"
    assert dag.is_valid("GO:0088888")  # alt id resolves to a valid primary


def test_obsolete_rejected(dag):
    assert dag.is_valid("GO:0000001") is False
    assert dag.is_valid("GO:0000001", allow_obsolete=True) is True


def test_unknown_term_invalid(dag):
    assert dag.is_valid("GO:9999999") is False
    assert dag.normalize("GO:9999999") is None


def test_ancestors_multi_parent_and_part_of(dag):
    # GO:0044237 is_a both cellular process and metabolic process -> up to root
    anc = dag.ancestors("GO:0044237")
    assert {"GO:0009987", "GO:0008152", "GO:0008150"} <= anc
    # part_of contributes to ancestors
    assert "GO:0005634" in dag.ancestors("GO:0070013")
    # roots have no ancestors
    assert dag.ancestors("GO:0008150") == frozenset()


# --- antibodies -----------------------------------------------------------

def test_go_score_range_pass_and_block(dag):
    assert go_score_range([Prediction("P1", "GO:0044237", 0.5)]).status == "pass"
    assert go_score_range([Prediction("P1", "GO:0044237", 0.0)]).status == "block"
    assert go_score_range([Prediction("P1", "GO:0044237", 1.5)]).status == "block"
    # >3 sig figs
    assert go_score_range([Prediction("P1", "GO:0044237", 0.12345)]).status == "block"
    assert go_score_range([Prediction("P1", "GO:0044237", 0.123)]).status == "pass"
    assert go_score_range([Prediction("P1", "GO:0044237", 1.0)]).status == "pass"


def test_go_term_validity_flags_invalid_and_obsolete(dag):
    preds = [
        Prediction("P1", "GO:0044237", 0.9),   # valid
        Prediction("P1", "GO:9999999", 0.9),   # unknown
        Prediction("P1", "GO:0000001", 0.9),   # obsolete
        Prediction("P1", "GO:0088888", 0.9),   # alt -> valid
    ]
    v = go_term_validity(preds, dag)
    assert v.status == "flag"
    bad_ids = {go for _, go in v.offenders}
    assert bad_ids == {"GO:9999999", "GO:0000001"}


def test_go_term_cap(dag):
    under = [Prediction("P1", "GO:0044237", 0.5) for _ in range(3)]
    assert go_term_cap(under, cap=5).status == "pass"
    over = [Prediction("P1", f"GO:000000{i%10}", 0.5) for i in range(7)]
    assert go_term_cap(over, cap=5).status == "block"


def test_subontology_partition(dag):
    ok = [Prediction("P1", "GO:0005488", 0.5)]
    assert subontology_partition(ok, dag).status == "pass"
    bad = [Prediction("P1", "GO:9999999", 0.5)]
    assert subontology_partition(bad, dag).status == "flag"


def test_propagation_consistency_detects_violation(dag):
    # child scored, ancestor missing -> violation
    preds = [Prediction("P1", "GO:0044237", 0.9)]
    v = go_propagation_consistency(preds, dag)
    assert v.status == "flag"
    assert len(v.offenders) >= 1  # ancestors 0009987/0008152 missing


def test_propagate_to_root_fixes_consistency(dag):
    preds = [Prediction("P1", "GO:0044237", 0.9)]
    fixed = propagate_to_root(preds, dag)
    # now consistent
    assert go_propagation_consistency(fixed, dag).status == "pass"
    # ancestors present at >= child score, roots excluded
    by = {(p.protein_id, p.go_id): p.score for p in fixed}
    assert by[("P1", "GO:0009987")] >= 0.9
    assert by[("P1", "GO:0008152")] >= 0.9
    assert ("P1", "GO:0008150") not in by  # root excluded (weight 0)
    # idempotent
    assert len(propagate_to_root(fixed, dag)) == len(fixed)


def test_propagation_parent_below_child_flagged(dag):
    preds = [
        Prediction("P1", "GO:0044237", 0.9),
        Prediction("P1", "GO:0009987", 0.3),  # parent below child
        Prediction("P1", "GO:0008152", 0.95),
    ]
    v = go_propagation_consistency(preds, dag)
    assert v.status == "flag"


def test_run_all_panel_clean_submission(dag):
    preds = propagate_to_root([
        Prediction("P1", "GO:0044237", 0.9),
        Prediction("P1", "GO:0005488", 0.7),
    ], dag)
    verdicts = run_all(preds, dag)
    assert {v.antibody for v in verdicts} == {
        "go_score_range", "go_term_cap", "go_term_validity",
        "subontology_partition", "go_propagation_consistency",
    }
    assert all(v.status == "pass" for v in verdicts)
