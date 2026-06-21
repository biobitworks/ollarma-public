"""Tests for D3 local citation resolution (fail-closed on novel PMIDs)."""
from ollarma.cafa.citation import (
    LocalAbstractCorpus,
    resolve_citations,
    normalize_pmid,
)


def test_normalize_pmid_forms():
    assert normalize_pmid("PMID: 1234567") == "1234567"
    assert normalize_pmid("PMID1234567") == "1234567"
    assert normalize_pmid("1234567") == "1234567"
    assert normalize_pmid("no digits") is None


def _corpus():
    return LocalAbstractCorpus({
        "1234567": "P53 induces apoptosis upon DNA damage.",
        "7654321": "BRCA1 participates in homologous recombination repair.",
    })


def test_all_resolved_locally_passes():
    c = _corpus()
    r = resolve_citations("P04637", ["PMID:1234567"], c)
    assert r.verdict.status == "pass"
    assert r.resolved["1234567"].startswith("P53")
    assert r.novel_pmids == []
    assert r.absolutely_necessary is False
    assert r.network_calls == 0


def test_novel_pmid_flagged_not_fetched():
    c = _corpus()
    r = resolve_citations("P1", ["PMID:9999999"], c)
    assert r.verdict.status == "flag"
    assert r.novel_pmids == ["9999999"]
    assert r.absolutely_necessary is True   # online gate required, NOT auto-fetched
    assert r.network_calls == 0             # fail-closed: still no network
    assert "9999999" not in r.resolved


def test_mixed_resolved_and_novel():
    c = _corpus()
    r = resolve_citations("P1", ["1234567", "PMID: 5555555"], c)
    assert set(r.resolved) == {"1234567"}
    assert r.novel_pmids == ["5555555"]
    assert r.absolutely_necessary is True


def test_no_citations_is_flag():
    r = resolve_citations("P1", [], _corpus())
    assert r.verdict.status == "flag"


def test_corpus_membership_and_jsonl(tmp_path):
    p = tmp_path / "abstracts.jsonl"
    p.write_text(
        '{"pmid": "PMID:1111111", "abstract": "alpha"}\n'
        '{"pmid": "2222222", "abstract": "beta"}\n'
        '{"pmid": "3333333"}\n'  # no abstract -> skipped
    )
    c = LocalAbstractCorpus.from_jsonl(p)
    assert len(c) == 2
    assert "1111111" in c and "PMID:2222222" in c
    assert "3333333" not in c
