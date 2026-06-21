"""Subprocess-driven smoke for scripts/swarm_proof_run.py.

Per PI direction: 'Smoke test against a stub target only. Do not mark
Phase 69 complete from the stub smoke alone.'

Three cases:
1. Happy path: smoke + dry-run produces the documented bundle layout.
2. Safety gate: --mode proof without --allow-target-writes refuses (exit 2).
3. --mode proof --dry-run is allowed without --allow-target-writes (no LLM,
   no target writes; the gate fires only on a REAL proof-run that would
   actually call Ollama).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = REPO_ROOT / "scripts" / "swarm_proof_run.py"


def test_proof_run_smoke_dry_run(tmp_path):
    """Happy path: smoke + dry-run produces the full bundle layout."""
    out = tmp_path / "bundle"
    result = subprocess.run(
        [
            sys.executable, str(DRIVER),
            "--target", "/tmp/stub",
            "--task", "stub task",
            "--mode", "smoke",
            "--dry-run",
            "--out", str(out),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert result.returncode == 0, (
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )

    # Manifest exists with expected shape.
    manifest_path = out / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["phase"] == "69-proof-run-and-verification-bundle"
    assert manifest["args"]["mode"] == "smoke"
    assert manifest["args"]["dry_run"] is True
    assert manifest["args"]["target"] == "/tmp/stub"
    assert manifest["args"]["task"] == "stub task"
    assert manifest["driver_version"]
    assert manifest["invoked_at_utc"]
    assert "git_head" in manifest

    # README exists.
    assert (out / "README.md").exists()

    # LaneStore artifacts under runs/<run_id>/ (LaneStore.run_dir_for
    # resolves to base_dir/runs/<run_id>).
    runs_dir = out / "runs"
    assert runs_dir.exists()
    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1, (
        f"expected exactly one run-dir under {runs_dir}; "
        f"got {[p.name for p in run_dirs]}"
    )
    run_dir = run_dirs[0]

    # 4 transitions = planner -> executor -> reviewer -> synthesizer.
    transitions = (run_dir / "lane_transitions.jsonl").read_text().splitlines()
    assert len(transitions) == 4

    # Final checkpoint records synthesizer as last completed.
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
    assert checkpoint["last_completed_role"] == "synthesizer"

    # 4 lane_outputs/*.json (one per role).
    lane_outputs = list((run_dir / "lane_outputs").glob("*.json"))
    assert len(lane_outputs) == 4


def test_proof_run_refuses_proof_without_allow_target_writes(tmp_path):
    """Safety gate: --mode proof without --allow-target-writes exits 2."""
    result = subprocess.run(
        [
            sys.executable, str(DRIVER),
            "--target", "/tmp/stub",
            "--task", "stub",
            "--mode", "proof",
            "--out", str(tmp_path / "bundle"),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=10,
    )
    assert result.returncode == 2, (
        f"expected exit 2 (safety gate); got {result.returncode}\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )
    assert "--allow-target-writes" in result.stderr


def test_proof_run_proof_dry_run_succeeds_without_target_writes(tmp_path):
    """--mode proof --dry-run is allowed without --allow-target-writes.

    The gate fires only on a REAL proof-run (proof + not dry-run + not
    allow-target-writes). With --dry-run there is no LLM and no writes
    outside --out, so the bundle smoke is permitted.
    """
    out = tmp_path / "bundle"
    result = subprocess.run(
        [
            sys.executable, str(DRIVER),
            "--target", "/tmp/stub",
            "--task", "stub",
            "--mode", "proof",
            "--dry-run",
            "--out", str(out),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert result.returncode == 0, (
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )
    # Manifest records mode=proof + dry_run=True.
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["args"]["mode"] == "proof"
    assert manifest["args"]["dry_run"] is True
