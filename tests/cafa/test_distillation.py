"""Tests for D5 distillation-pair collection (local→local, dedup, frontier tracking)."""
from ollarma.cafa.distillation import DistillationCollector


def _req():
    return {
        "to_lane": "antigence",
        "antibodies": ["claim_entailment", "open_world_overclaim"],
        "target": "P04637",
        "text": "P53 induces apoptosis upon DNA damage (PMID: 1234567).",
        "cited_pmids": ["1234567"],
        "frontier_allowed": False,
    }


def test_collect_writes_pair(tmp_path):
    c = DistillationCollector(tmp_path / "pairs.jsonl")
    p = c.collect(escalation_request=_req(), teacher_model="deepseek-r1:14b",
                  teacher_tier="reasoning_rung", verdict="SUPPORT",
                  rationale="abstract states apoptosis induction")
    assert p is not None
    assert p.antibody == "claim_entailment"
    assert p.teacher_tier == "reasoning_rung"
    assert c.count() == 1


def test_dedup_same_pair(tmp_path):
    c = DistillationCollector(tmp_path / "pairs.jsonl")
    kw = dict(escalation_request=_req(), teacher_model="deepseek-r1:14b",
              teacher_tier="reasoning_rung", verdict="SUPPORT", rationale="x")
    assert c.collect(**kw) is not None
    assert c.collect(**kw) is None       # duplicate -> not re-written
    assert c.count() == 1


def test_persistence_across_instances(tmp_path):
    store = tmp_path / "pairs.jsonl"
    c1 = DistillationCollector(store)
    c1.collect(escalation_request=_req(), teacher_model="qwen3.5:9b",
               teacher_tier="escalation_rung", verdict="NOINFO", rationale="y")
    c2 = DistillationCollector(store)     # reload
    assert c2.count() == 1
    # different verdict/teacher -> new pair
    assert c2.collect(escalation_request=_req(), teacher_model="qwen3.5:9b",
                      teacher_tier="escalation_rung", verdict="SUPPORT",
                      rationale="z") is not None
    assert c2.count() == 2


def test_frontier_fraction_tracks_absolutely_necessary(tmp_path):
    store = tmp_path / "pairs.jsonl"
    c = DistillationCollector(store)
    c.collect(escalation_request=_req(), teacher_model="deepseek-r1:14b",
              teacher_tier="reasoning_rung", verdict="SUPPORT", rationale="a")
    assert c.frontier_fraction() == 0.0   # local teacher -> 0 frontier
    r2 = _req(); r2["text"] = "novel claim with no local abstract"
    c.collect(escalation_request=r2, teacher_model="frontier-claude",
              teacher_tier="frontier", verdict="REFUTE", rationale="b",
              frontier_used=True)
    assert 0.0 < c.frontier_fraction() <= 0.5
