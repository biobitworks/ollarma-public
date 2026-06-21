#!/usr/bin/env python3
"""Phase 71 first real-repo notebook-workflow run (operator-gated).

Exercises ``ollarma.notebook_workflow.run_notebook_workflow`` against a REAL
sibling repo (deltaprot) for the first time, per the v5.1 ROADMAP Phase 71
operator gate.

Safety posture (criteria #1 + #9):
  - Uses the DEFAULT read-only validate runner: the target notebook is NEVER
    executed. The runtime reads it, validates independence, hashes it, and emits
    a hash-chained notebook_workflow_receipt. No writeback to the target repo.
  - Demonstrates BOTH behaviors against the real repo:
      A) fail-closed: pointing the selection check at a dir with no fresh
         selection artifact -> SELECTION_STALE / SELECTION_MISSING (criterion #6).
      B) happy path: no selection dir (a read-only validate run selects and
         invokes NO model), producing a real hash-chained receipt for
         deltaprot's EXP-004 notebook.

Run: .venv/bin/python scripts/phase71_first_target_run.py
"""
from __future__ import annotations

import json
import pathlib
import tempfile

from ollarma.notebook_workflow import (
    NotebookStep,
    NotebookWorkflowManifest,
    NotebookWorkflowBlocked,
    run_notebook_workflow,
    verify_receipt_chain,
)

DELTAPROT = pathlib.Path("<repo>")
NOTEBOOK_REL = "notebooks/EXP-004_descriptive_analysis.ipynb"
# Committable human-readable report (run-ledger receipts are NOT allowed here --
# the run-ledger enforces a dedicated execution subtree, see RECEIPTS below).
OUT = pathlib.Path("runs/notebook_workflow/phase71_deltaprot_first_run")
# The run-ledger only admits writes under a dedicated execution subtree
# (run_ledger._EXECUTION_SUBTREE_PREFIXES); ".ollarma/" is the canonical one.
RECEIPTS = pathlib.Path(".ollarma/notebook_workflow/phase71_deltaprot_first_run")


def _manifest() -> NotebookWorkflowManifest:
    return NotebookWorkflowManifest(
        manifest_version="1.0",
        consumer_repo="ollarma",
        task_class="validated-notebook",
        run_id="phase71-deltaprot-first-run",
        exp_linkage="EXP-004",  # deltaprot's own EXP linkage
        steps=(
            NotebookStep(
                step_id="deltaprot-exp004-descriptive",
                target_repo="deltaprot",
                notebook=NOTEBOOK_REL,
            ),
        ),
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    repo_roots = {"deltaprot": DELTAPROT}
    report: dict = {"target_repo": str(DELTAPROT), "notebook": NOTEBOOK_REL}

    # A) fail-closed demonstration (criterion #6) against the real repo: an empty
    #    results dir has no fresh selection artifact, so the selection gate fires.
    with tempfile.TemporaryDirectory() as empty_results:
        try:
            run_notebook_workflow(
                _manifest(),
                repo_roots=repo_roots,
                receipts_repo_root=RECEIPTS / "_failclosed_store",
                receipts_path=RECEIPTS / "_failclosed_store" / "receipts.json",
                selection_results_dir=empty_results,
            )
            report["fail_closed_demo"] = "UNEXPECTED_PASS (selection was fresh)"
        except NotebookWorkflowBlocked as exc:
            report["fail_closed_demo"] = {"refused": True, "reason_code": exc.reason_code, "detail": exc.detail[:160]}

    # B) happy path: read-only validate runner (no model selected/invoked, so no
    #    selection_results_dir). Produces a real hash-chained receipt.
    receipts = run_notebook_workflow(
        _manifest(),
        repo_roots=repo_roots,
        receipts_repo_root=RECEIPTS / "receipt_store",
        receipts_path=RECEIPTS / "receipt_store" / "receipts.json",
    )
    tail = verify_receipt_chain(receipts)
    report["receipt_count"] = len(receipts)
    report["chain_tail_hash"] = tail
    report["receipts"] = [json.loads(r.model_dump_json()) for r in receipts]

    (OUT / "run_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nbundle written: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
