"""Tests for D8 oracle harness — measures the local↔online boundary."""
from ollarma.cafa.oracle import measure_task, measure_boundary


def test_proven_local_high_agreement():
    local = ["SUPPORT"] * 18 + ["REFUTE"] * 2
    oracle = ["SUPPORT"] * 18 + ["REFUTE"] * 2
    tb = measure_task("claim_entailment", local=local, oracle=oracle)
    assert tb.verdict == "proven_local"
    assert tb.agreement == 1.0


def test_needs_online_low_agreement():
    local = ["SUPPORT"] * 20
    oracle = ["SUPPORT"] * 10 + ["REFUTE"] * 10   # 50% agreement
    tb = measure_task("sequence_grounding", local=local, oracle=oracle)
    assert tb.verdict == "needs_online"
    assert tb.agreement == 0.5


def test_insufficient_data():
    tb = measure_task("x", local=["A"] * 5, oracle=["A"] * 5, min_n=20)
    assert tb.verdict == "insufficient_data"


def test_gold_labels_accuracy_and_mcc():
    # local matches gold well; oracle slightly better
    gold = ["SUPPORT", "REFUTE"] * 10
    local = list(gold)
    local[0] = "REFUTE"  # one miss
    oracle = list(gold)
    tb = measure_task("claim_entailment", local=local, oracle=oracle,
                      gold=gold, positive="SUPPORT")
    assert tb.accuracy_local is not None and tb.accuracy_local < 1.0
    assert tb.accuracy_oracle == 1.0
    assert tb.mcc_local is not None
    # local within 0.05 of oracle and high agreement -> proven_local
    assert tb.verdict == "proven_local"


def test_local_materially_worse_than_oracle_needs_online():
    gold = ["SUPPORT"] * 10 + ["REFUTE"] * 10
    oracle = list(gold)                      # perfect
    local = ["SUPPORT"] * 20                 # 50% accuracy, agreement 0.5 w/ oracle
    tb = measure_task("open_world_overclaim", local=local, oracle=oracle,
                      gold=gold, positive="SUPPORT")
    assert tb.verdict == "needs_online"


def test_boundary_report_partitions_tasks():
    rep = measure_boundary({
        "claim_entailment": {"local": ["A"] * 20, "oracle": ["A"] * 20},
        "sequence_grounding": {"local": ["A"] * 20,
                               "oracle": ["B"] * 20},
    })
    assert rep.proven_local == ["claim_entailment"]
    assert rep.needs_online == ["sequence_grounding"]
    assert "PROVEN-LOCAL" in rep.summary()
