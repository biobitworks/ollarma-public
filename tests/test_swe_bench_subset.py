"""test_swe_bench_subset.py -- SWE-06 subset selector."""
from __future__ import annotations

import pytest

from ollarma.swe_bench import SWEBenchProblem, resolve_subset


def _mk(id_: str) -> SWEBenchProblem:
    return SWEBenchProblem(
        instance_id=id_, repo="ex/r", base_commit="abc",
        problem_statement="s", test_patch="", test_cmd="true",
    )


def test_subset_first_n():
    problems = [_mk(f"p-{i}") for i in range(5)]
    sel = resolve_subset(problems, "first-3")
    assert [p.instance_id for p in sel] == ["p-0", "p-1", "p-2"]


def test_subset_explicit_ids():
    problems = [_mk(f"p-{i}") for i in range(5)]
    sel = resolve_subset(problems, "ids=p-3,p-0,p-2")
    assert [p.instance_id for p in sel] == ["p-3", "p-0", "p-2"]

    with pytest.raises(ValueError, match="unknown instance_ids"):
        resolve_subset(problems, "ids=p-99")


def test_subset_random_seed_reproducible():
    problems = [_mk(f"p-{i}") for i in range(20)]

    sel_a = resolve_subset(problems, "random-seed=42,count=5")
    sel_b = resolve_subset(problems, "random-seed=42,count=5")
    sel_c = resolve_subset(problems, "random-seed=43,count=5")

    ids_a = [p.instance_id for p in sel_a]
    ids_b = [p.instance_id for p in sel_b]
    ids_c = [p.instance_id for p in sel_c]

    assert ids_a == ids_b  # deterministic given seed
    assert ids_a != ids_c  # different seed -> different sample
    assert len(ids_a) == 5
    assert len(set(ids_a)) == 5  # unique

    with pytest.raises(ValueError, match="unrecognized subset spec"):
        resolve_subset(problems, "garbage")
