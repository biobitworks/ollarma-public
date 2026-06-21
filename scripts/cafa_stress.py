#!/usr/bin/env python3
"""D7 (deterministic slice) — stress the local GO verification substrate at scale.

Generates a large synthetic GO submission and runs it through the deterministic
cascade against the REAL go-basic.obo, measuring throughput, propagation blow-up,
and that the substrate holds with zero model/network/frontier. The full-pipeline
stress (with the Antigence model lanes) follows once that lane is live.

Usage: .venv/bin/python scripts/cafa_stress.py [n_proteins] [terms_per_protein]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from ollarma.cafa.go_ontology import GoDag
from ollarma.cafa import Prediction, verify_go_submission
from ollarma.cafa.receipts import verify_chain

OBO = Path("data/go-basic.obo")


def main() -> int:
    n_proteins = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    per = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    if not OBO.is_file():
        print(f"[stress] {OBO} absent — run the one-time bootstrap first "
              f"(dataset_registry.bootstrap_go_basic(..., allow_download=True)).")
        return 2

    t0 = time.perf_counter()
    dag = GoDag.from_obo(OBO)
    load_s = time.perf_counter() - t0

    # pick deep, valid, non-obsolete leaf-ish terms to force real propagation
    deep = [t for t in dag.terms
            if not dag.terms[t].is_obsolete and len(dag.ancestors(t)) >= 5]
    deep.sort(key=lambda t: len(dag.ancestors(t)), reverse=True)
    pool = deep[:5000] or [t for t in dag.terms if not dag.terms[t].is_obsolete][:5000]

    import itertools
    cyc = itertools.cycle(pool)
    submissions = [
        (f"PROT{ i:06d}", [Prediction(f"PROT{i:06d}", next(cyc), 0.5 + (j % 5) * 0.1)
                           for j in range(per)])
        for i in range(n_proteins)
    ]

    t0 = time.perf_counter()
    total_rows_in = 0
    total_rows_out = 0
    statuses: dict[str, int] = {}
    chain_ok = True
    sample_checked = 0
    for target, preds in submissions:
        res = verify_go_submission(target, preds, dag)
        statuses[res.status] = statuses.get(res.status, 0) + 1
        total_rows_in += len(preds)
        total_rows_out += len(res.propagated)
        assert res.network_calls == 0 and res.frontier_calls == 0
        if sample_checked < 50:  # verify receipt chains on a sample
            chain_ok = chain_ok and verify_chain(res.receipts)
            sample_checked += 1
    wall = time.perf_counter() - t0

    print(f"[stress] go-basic.obo: {len(dag)} terms loaded in {load_s:.2f}s")
    print(f"[stress] {n_proteins} proteins x {per} terms "
          f"= {total_rows_in} input rows")
    print(f"[stress] verified in {wall:.2f}s  -> "
          f"{n_proteins/wall:.0f} proteins/s, {total_rows_in/wall:.0f} input-rows/s")
    print(f"[stress] propagation: {total_rows_in} -> {total_rows_out} rows "
          f"({total_rows_out/total_rows_in:.1f}x ancestor blow-up)")
    print(f"[stress] statuses: {statuses}")
    print(f"[stress] receipt chains valid (sample {sample_checked}): {chain_ok}")
    print(f"[stress] model/network/frontier calls: 0 / 0 / 0  (fully local)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
