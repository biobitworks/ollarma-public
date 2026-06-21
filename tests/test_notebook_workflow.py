"""Phase 71 cross-repo notebook workflow runtime tests (criterion #8).

All fixtures are TINY in-repo notebooks created under ``tmp_path`` -- there is
NO execution against any real science repo (ROADMAP criterion #9). The default
runtime runner is read-only (validate-only); these tests inject a stub runner
only to exercise the executed-notebook-hash path without papermill / Ollama.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

import orjson
import pytest

from ollarma.evidence import canonical_hash
from ollarma.notebook_workflow import (
    NotebookIndependenceError,
    NotebookStep,
    NotebookWorkflowBlocked,
    NotebookWorkflowManifest,
    check_swap_blockers,
    run_notebook_workflow,
    sha256_file,
    validate_notebook_independence,
    verify_receipt_chain,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _notebook(code_cells: list[str], markdown: str = "intro") -> dict:
    """Build a minimal nbformat-4 notebook dict."""
    cells: list[dict] = [{"cell_type": "markdown", "source": [markdown], "metadata": {}}]
    for src in code_cells:
        cells.append(
            {"cell_type": "code", "source": [src], "metadata": {}, "outputs": [], "execution_count": None}
        )
    return {
        "cells": cells,
        "metadata": {"kernelspec": {"name": "python3", "language": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _write_notebook(path: pathlib.Path, code_cells: list[str]) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_notebook(code_cells), indent=1), encoding="utf-8")
    return path


def _valid_notebook(repo_root: pathlib.Path, rel: str = "notebooks/analysis.ipynb") -> pathlib.Path:
    """A genuine, independent replay notebook (no delegation/wrappers)."""
    return _write_notebook(
        repo_root / rel,
        [
            "import numpy as np",
            "data = np.array([1.0, 2.0, 3.0])\nmean = data.mean()",
            "assert abs(mean - 2.0) < 1e-9\nprint('mean', mean)",
        ],
    )


def _manifest_single(run_id: str = "20260531T120000Z") -> NotebookWorkflowManifest:
    return NotebookWorkflowManifest(
        manifest_version="1.0",
        consumer_repo="ollarma",
        task_class="validated-notebook",
        run_id=run_id,
        steps=(
            NotebookStep(
                step_id="analysis",
                target_repo="deltaprot",
                notebook="notebooks/analysis.ipynb",
                outputs=("notebooks/result.json",),
            ),
        ),
        exp_linkage="EXP-DELTAPROT-001",
        prompt_linkage="PROMPT-DELTAPROT-001",
    )


def _stub_runner(notebook_path: pathlib.Path) -> str:
    """Stub runner: hashes the notebook WITHOUT executing it (no papermill)."""
    return sha256_file(notebook_path)


# ---------------------------------------------------------------------------
# Independence validator (criterion #4)
# ---------------------------------------------------------------------------


def test_independent_notebook_passes(tmp_path: pathlib.Path) -> None:
    nb = _valid_notebook(tmp_path)
    result = validate_notebook_independence(nb)
    assert result.independent is True
    assert result.violations == ()
    assert result.code_cell_count == 3


@pytest.mark.parametrize(
    ("cell", "expected_code"),
    [
        ("%run other_notebook.ipynb", "PERCENT_RUN"),
        ("!python scripts/run.py", "BANG_PYTHON"),
        ("import subprocess\nsubprocess.run(['python', 'x.py'])", "SUBPROCESS"),
        ("import subprocess", "SUBPROCESS_IMPORT"),
        ("import os\nos.system('python scripts/run.py')", "OS_SYSTEM"),
        ("!bash run.sh", "SHELL_WRAPPER"),
        ("import runpy\nrunpy.run_path('scripts/expABC.py')", "EXP_SCRIPT_DELEGATION"),
    ],
)
def test_wrapper_notebook_rejected(tmp_path: pathlib.Path, cell: str, expected_code: str) -> None:
    nb = _write_notebook(tmp_path / "wrapper.ipynb", ["import numpy as np", cell])
    result = validate_notebook_independence(nb)
    assert result.independent is False
    assert expected_code in result.violations


def test_runtime_raises_on_wrapper_notebook(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "deltaprot"
    _write_notebook(
        repo / "notebooks/analysis.ipynb",
        ["import subprocess", "subprocess.run(['python', 'scripts/exp001.py'])"],
    )
    receipts_root = tmp_path / "ollarma_repo"
    receipts_path = receipts_root / ".ollarma" / "runs" / "r1" / "receipts.json"

    with pytest.raises(NotebookIndependenceError) as exc:
        run_notebook_workflow(
            _manifest_single(),
            repo_roots={"deltaprot": repo},
            receipts_repo_root=receipts_root,
            receipts_path=receipts_path,
            runner=_stub_runner,
        )
    assert "SUBPROCESS" in exc.value.result.violations
    # No receipt should be written when the very first step is rejected.
    assert not receipts_path.exists()


# ---------------------------------------------------------------------------
# Happy path (criteria #1 + #3 + #7)
# ---------------------------------------------------------------------------


def test_single_notebook_happy_path(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "deltaprot"
    _valid_notebook(repo)
    (repo / "notebooks/result.json").write_text('{"ok": true}', encoding="utf-8")
    receipts_root = tmp_path / "ollarma_repo"
    receipts_path = receipts_root / ".ollarma" / "runs" / "r1" / "receipts.json"

    receipts = run_notebook_workflow(
        _manifest_single(),
        repo_roots={"deltaprot": repo},
        receipts_repo_root=receipts_root,
        receipts_path=receipts_path,
        runner=_stub_runner,
    )

    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.status == "completed"
    assert receipt.run_id == "20260531T120000Z"
    assert receipt.executed_notebook_hash.startswith("sha256:")
    assert receipt.output_hashes["notebooks/result.json"].startswith("sha256:")
    assert receipt.claim_promotion_performed is False
    assert "no-writeback" in receipt.no_writeback_statement
    assert receipt.exp_linkage == "EXP-DELTAPROT-001"
    assert receipt.validation_result["independent"] is True
    # Criterion #7: status projected onto the existing run-ledger receipt store.
    assert receipts_path.exists()
    ledger = orjson.loads(receipts_path.read_bytes())
    assert ledger[0]["status"] == "completed"
    assert ledger[0]["lane"] == "notebook_workflow_queue"


def test_receipt_carries_input_and_notebook_hashes(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "deltaprot"
    _valid_notebook(repo)
    (repo / "data/input.csv").parent.mkdir(parents=True, exist_ok=True)
    (repo / "data/input.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    manifest = NotebookWorkflowManifest(
        manifest_version="1.0",
        consumer_repo="ollarma",
        task_class="validated-notebook",
        run_id="r-inputs",
        steps=(
            NotebookStep(
                step_id="analysis",
                target_repo="deltaprot",
                notebook="notebooks/analysis.ipynb",
                inputs=("data/input.csv",),
            ),
        ),
        exp_linkage="EXP-X-1",
    )
    receipts_root = tmp_path / "ollarma_repo"
    receipts = run_notebook_workflow(
        manifest,
        repo_roots={"deltaprot": repo},
        receipts_repo_root=receipts_root,
        receipts_path=receipts_root / ".ollarma" / "runs" / "r-inputs" / "receipts.json",
        runner=_stub_runner,
    )
    assert receipts[0].input_hashes["data/input.csv"] == sha256_file(repo / "data/input.csv")


# ---------------------------------------------------------------------------
# Hash-chain verification (criterion #8)
# ---------------------------------------------------------------------------


def test_hash_chain_verifies_and_detects_tampering(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "deltaprot"
    # Two-step DAG so the chain has more than one link.
    _write_notebook(repo / "notebooks/a.ipynb", ["x = 1"])
    _write_notebook(repo / "notebooks/b.ipynb", ["y = 2"])
    manifest = NotebookWorkflowManifest(
        manifest_version="1.0",
        consumer_repo="ollarma",
        task_class="validated-notebook-dag",
        run_id="r-chain",
        steps=(
            NotebookStep(step_id="a", target_repo="deltaprot", notebook="notebooks/a.ipynb"),
            NotebookStep(
                step_id="b", target_repo="deltaprot", notebook="notebooks/b.ipynb", depends_on=("a",)
            ),
        ),
        exp_linkage="EXP-X-1",
    )
    receipts_root = tmp_path / "ollarma_repo"
    receipts = run_notebook_workflow(
        manifest,
        repo_roots={"deltaprot": repo},
        receipts_repo_root=receipts_root,
        receipts_path=receipts_root / ".ollarma" / "runs" / "r-chain" / "receipts.json",
        runner=_stub_runner,
    )
    # Intact chain verifies and returns the tail hash.
    tail = verify_receipt_chain(receipts)
    assert tail == receipts[-1].receipt_hash
    assert receipts[0].parent_hash == "0" * 64
    assert receipts[1].parent_hash == receipts[0].receipt_hash

    # Tamper with the first receipt's payload (keep its old hash) -> chain breaks.
    tampered_first = receipts[0].model_copy(update={"target_repo": "evil-repo"})
    with pytest.raises(ValueError, match="not append-only"):
        verify_receipt_chain([tampered_first, receipts[1]])


# ---------------------------------------------------------------------------
# Missing-EXP-linkage refusal (criterion #5 / #8)
# ---------------------------------------------------------------------------


def test_missing_exp_linkage_refusal(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "deltaprot"
    _valid_notebook(repo)
    manifest = NotebookWorkflowManifest(
        manifest_version="1.0",
        consumer_repo="ollarma",
        task_class="validated-notebook",
        run_id="r-nolink",
        steps=(
            NotebookStep(step_id="analysis", target_repo="deltaprot", notebook="notebooks/analysis.ipynb"),
        ),
        # Both linkages omitted -> gsigmad gate must refuse.
    )
    receipts_root = tmp_path / "ollarma_repo"
    receipts_path = receipts_root / ".ollarma" / "runs" / "r-nolink" / "receipts.json"
    with pytest.raises(NotebookWorkflowBlocked) as exc:
        run_notebook_workflow(
            manifest,
            repo_roots={"deltaprot": repo},
            receipts_repo_root=receipts_root,
            receipts_path=receipts_path,
            runner=_stub_runner,
        )
    assert exc.value.reason_code == "MISSING_EXP_LINKAGE"
    # Refused before any execution -> no receipts written.
    assert not receipts_path.exists()


# ---------------------------------------------------------------------------
# Swap-blocked refusal (criterion #6 / #8)
# ---------------------------------------------------------------------------


def test_swap_blocked_refusal(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "deltaprot"
    _valid_notebook(repo)
    receipts_root = tmp_path / "ollarma_repo"
    receipts_path = receipts_root / ".ollarma" / "runs" / "r-swap" / "receipts.json"
    with pytest.raises(NotebookWorkflowBlocked) as exc:
        run_notebook_workflow(
            _manifest_single("r-swap"),
            repo_roots={"deltaprot": repo},
            receipts_repo_root=receipts_root,
            receipts_path=receipts_path,
            swap_pct=95.0,  # >= SWAP_BLOCKED_PCT_THRESHOLD (80)
            runner=_stub_runner,
        )
    assert exc.value.reason_code == "SWAP_BLOCKED"
    assert not receipts_path.exists()


def test_swap_degraded_refusal_direct() -> None:
    # Reuses Phase 67 threshold (50): degraded fires below the blocked ceiling.
    with pytest.raises(NotebookWorkflowBlocked) as exc:
        check_swap_blockers(60.0)
    assert exc.value.reason_code == "SWAP_DEGRADED"
    # Healthy host passes.
    check_swap_blockers(10.0)


def test_selection_stale_refusal(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "deltaprot"
    _valid_notebook(repo)
    # Write an aged selection artifact for the science suite (NOTEBOOK workload).
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    body = {
        "run_id": "2026-01-01T00:00:00Z",  # far older than 24h vs `now`
        "per_suite_winners": {"science": "qwen3:4b"},
        "pareto_frontier": ["qwen3:4b"],
    }
    body["stable_decision_hash"] = canonical_hash(body)
    (results_dir / "run-old.artifact.json").write_bytes(orjson.dumps(body))

    receipts_root = tmp_path / "ollarma_repo"
    receipts_path = receipts_root / ".ollarma" / "runs" / "r-stale" / "receipts.json"
    with pytest.raises(NotebookWorkflowBlocked) as exc:
        run_notebook_workflow(
            _manifest_single("r-stale"),
            repo_roots={"deltaprot": repo},
            receipts_repo_root=receipts_root,
            receipts_path=receipts_path,
            selection_results_dir=results_dir,
            now=dt.datetime(2026, 5, 31, tzinfo=dt.timezone.utc),
            runner=_stub_runner,
        )
    assert exc.value.reason_code == "SELECTION_STALE"
    assert not receipts_path.exists()


# ---------------------------------------------------------------------------
# Multi-notebook cross-repo DAG happy path (criterion #2 / #8)
# ---------------------------------------------------------------------------


def test_cross_repo_dag_happy_path(tmp_path: pathlib.Path) -> None:
    repo_a = tmp_path / "deltaprot"
    repo_b = tmp_path / "bioviz-atlas"
    _write_notebook(repo_a / "notebooks/prep.ipynb", ["prep = [1, 2, 3]"])
    _write_notebook(repo_b / "notebooks/render.ipynb", ["render = sum([1, 2, 3])"])

    manifest = NotebookWorkflowManifest(
        manifest_version="1.0",
        consumer_repo="ollarma",
        task_class="validated-notebook-dag",
        run_id="r-dag",
        steps=(
            # Declared out of order; runtime must topologically sort.
            NotebookStep(
                step_id="render",
                target_repo="bioviz-atlas",
                notebook="notebooks/render.ipynb",
                depends_on=("prep",),
            ),
            NotebookStep(step_id="prep", target_repo="deltaprot", notebook="notebooks/prep.ipynb"),
        ),
        exp_linkage="EXP-CROSS-1",
    )
    receipts_root = tmp_path / "ollarma_repo"
    receipts_path = receipts_root / ".ollarma" / "runs" / "r-dag" / "receipts.json"
    receipts = run_notebook_workflow(
        manifest,
        repo_roots={"deltaprot": repo_a, "bioviz-atlas": repo_b},
        receipts_repo_root=receipts_root,
        receipts_path=receipts_path,
        runner=_stub_runner,
    )
    # Dependency order respected: prep before render.
    assert [r.step_id for r in receipts] == ["prep", "render"]
    assert receipts[0].target_repo == "deltaprot"
    assert receipts[1].target_repo == "bioviz-atlas"
    assert verify_receipt_chain(receipts) == receipts[-1].receipt_hash
    # Both steps observable in the existing receipt store (criterion #7).
    ledger = orjson.loads(receipts_path.read_bytes())
    assert [row["step_id"] for row in ledger] == ["prep", "render"]


# ---------------------------------------------------------------------------
# Manifest schema guards
# ---------------------------------------------------------------------------


def test_single_class_rejects_multistep() -> None:
    with pytest.raises(ValueError, match="exactly one step"):
        NotebookWorkflowManifest(
            manifest_version="1.0",
            consumer_repo="ollarma",
            task_class="validated-notebook",
            run_id="r",
            steps=(
                NotebookStep(step_id="a", target_repo="deltaprot", notebook="notebooks/a.ipynb"),
                NotebookStep(step_id="b", target_repo="deltaprot", notebook="notebooks/b.ipynb"),
            ),
            exp_linkage="EXP-X-1",
        )


def test_dag_rejects_cycle() -> None:
    with pytest.raises(ValueError, match="cycle"):
        NotebookWorkflowManifest(
            manifest_version="1.0",
            consumer_repo="ollarma",
            task_class="validated-notebook-dag",
            run_id="r",
            steps=(
                NotebookStep(step_id="a", target_repo="r1", notebook="n/a.ipynb", depends_on=("b",)),
                NotebookStep(step_id="b", target_repo="r1", notebook="n/b.ipynb", depends_on=("a",)),
            ),
            exp_linkage="EXP-X-1",
        )


def test_unmapped_target_repo_refusal(tmp_path: pathlib.Path) -> None:
    receipts_root = tmp_path / "ollarma_repo"
    with pytest.raises(NotebookWorkflowBlocked) as exc:
        run_notebook_workflow(
            _manifest_single("r-unmapped"),
            repo_roots={},  # deltaprot intentionally unmapped
            receipts_repo_root=receipts_root,
            receipts_path=receipts_root / ".ollarma" / "runs" / "r-unmapped" / "receipts.json",
            runner=_stub_runner,
        )
    assert exc.value.reason_code == "TARGET_REPO_UNRESOLVED"
