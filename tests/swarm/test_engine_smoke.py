"""test_engine_smoke.py -- end-to-end smoke + quarantine tests for run_simulation.

Runs N=5 personas x M=2 rounds against the stub Ollama client (NO live
ollama daemon, NO GPU). Asserts:

* SimulationSummary returned and pydantic-validated
* per-round artifacts (round_0.json, round_1.json) and summary.json persist
* per-persona response files persist under r{round}/<persona_id>.json
* jsd_round_over_round[0] == 0.0 and len == n_rounds
* quarantine path: invalid JSON triggers retry-then-quarantine; aggregate
  excludes the quarantined response

Time is virtualised via the stub client's ``virtual_clock_seconds`` value;
``time.sleep`` is monkeypatched to a no-op so the suite stays under the
5-second budget specified in 70-03 acceptance.

PROMPT-OLLARMA-SWARM-001 task T7 (Phase 70 plan 70-03 wave 3).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ollarma.swarm import engine
from ollarma.swarm.engine import run_simulation
from ollarma.swarm.schemas import SimulationSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_engine_clock(monkeypatch: pytest.MonkeyPatch, client) -> None:
    """Patch the engine's ``time`` module so perf_counter follows the stub
    client's virtual clock, and time.sleep is a no-op.

    The engine imports ``time`` at module scope (``import time``), so we
    patch ``ollarma.swarm.engine.time.perf_counter`` and ``...time.sleep``
    rather than the global ``time`` module (cleaner; doesn't affect pytest
    timing infrastructure).
    """
    monkeypatch.setattr(
        engine.time, "perf_counter", lambda: client.virtual_clock_seconds
    )
    monkeypatch.setattr(engine.time, "sleep", lambda _s: None)


# ---------------------------------------------------------------------------
# Smoke: N=5 x M=2 happy path
# ---------------------------------------------------------------------------

def test_run_simulation_smoke(
    stub_ollama_client,
    tmp_run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N=5 x M=2 with all valid responses; full persistence + summary checks."""
    client = stub_ollama_client(wall_ms_per_response=10)
    _patch_engine_clock(monkeypatch, client)

    summary = run_simulation(
        scenario="Test scenario for smoke run.",
        scenario_id="smoke_test",
        n_personas=5,
        n_rounds=2,
        seed=42,
        run_dir=tmp_run_dir,
        ollama_client=client,
    )

    # Type + shape.
    assert isinstance(summary, SimulationSummary)
    assert summary.n_personas == 5
    assert summary.n_rounds == 2
    assert len(summary.rounds) == 2
    assert summary.scenario_id == "smoke_test"

    # JSD series convention: index 0 fixed at 0.0; length == n_rounds.
    assert summary.jsd_round_over_round[0] == 0.0
    assert len(summary.jsd_round_over_round) == 2
    assert 0.0 <= summary.jsd_round_over_round[1] <= 1.0

    # No quarantines on the happy path.
    assert summary.quarantine_rate == 0.0

    # Per-round + summary artifacts on disk.
    assert (tmp_run_dir / "round_0.json").is_file()
    assert (tmp_run_dir / "round_1.json").is_file()
    assert (tmp_run_dir / "summary.json").is_file()

    # summary.json is valid pydantic round-trip of the returned summary.
    on_disk = json.loads((tmp_run_dir / "summary.json").read_text())
    assert on_disk["n_personas"] == 5
    assert on_disk["n_rounds"] == 2

    # Stub got exactly n_personas * n_rounds calls (no retries on happy path).
    assert len(client.calls) == 5 * 2


# ---------------------------------------------------------------------------
# Quarantine: invalid JSON every 4th call
# ---------------------------------------------------------------------------

def test_run_simulation_quarantine_path(
    stub_ollama_client,
    tmp_run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub returns invalid JSON every 4th call; engine retries once then
    quarantines. Quarantine ledger has >=1 entry and aggregate excludes
    the quarantined response (proportions over (5-q), not 5)."""
    client = stub_ollama_client(
        wall_ms_per_response=10, invalid_json_every_n=4
    )
    _patch_engine_clock(monkeypatch, client)

    summary = run_simulation(
        scenario="Test scenario for quarantine path.",
        scenario_id="quarantine_test",
        n_personas=5,
        n_rounds=2,
        seed=42,
        run_dir=tmp_run_dir,
        ollama_client=client,
    )

    # Run completed with no exception.
    assert isinstance(summary, SimulationSummary)

    # quarantine.jsonl exists with >= 1 entry.
    q_path = tmp_run_dir / "quarantine.jsonl"
    assert q_path.is_file()
    q_lines = [
        json.loads(line)
        for line in q_path.read_text().splitlines()
        if line.strip()
    ]
    assert len(q_lines) >= 1
    # Schema spot-check on quarantine entries.
    for entry in q_lines:
        assert "persona_id" in entry
        assert "round_idx" in entry
        assert "attempts" in entry
        assert entry["attempts"] >= 1

    # Quarantine rate is positive but not 1.0 (mostly accepted).
    assert 0.0 < summary.quarantine_rate < 1.0

    # Aggregate proportions of the affected round sum to 1.0 (within float
    # tolerance) over the *accepted* responses, not over n_personas. With
    # invalid_every=4 and 5 personas/round, calls 4 and 8 fail (1-indexed):
    # call 4 = round 0 persona 4; call 8 = round 1 persona 3. Each round
    # loses one response, so distributions normalise over 4 accepted.
    for r_artifact in summary.rounds:
        total = sum(r_artifact.stance_distribution.values())
        # Either fully accepted (sum == 1.0) or partially quarantined (still
        # normalised to 1.0 over the accepted subset). stance_distribution
        # always normalises — so total should be ~1.0.
        assert abs(total - 1.0) < 1e-9
        # At least one round must have quarantined_count > 0.
    assert any(r.quarantined_count > 0 for r in summary.rounds)


# ---------------------------------------------------------------------------
# Per-response persistence
# ---------------------------------------------------------------------------

def test_run_simulation_persists_per_response(
    stub_ollama_client,
    tmp_run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each accepted persona-round writes <run_dir>/r<round>/<persona_id>.json."""
    client = stub_ollama_client(wall_ms_per_response=10)
    _patch_engine_clock(monkeypatch, client)

    summary = run_simulation(
        scenario="Per-response persistence test.",
        scenario_id="persistence_test",
        n_personas=5,
        n_rounds=2,
        seed=42,
        run_dir=tmp_run_dir,
        ollama_client=client,
    )

    # Round directories exist.
    assert (tmp_run_dir / "r0").is_dir()
    assert (tmp_run_dir / "r1").is_dir()

    # All personas accepted on the happy path -> 5 files per round dir.
    r0_files = list((tmp_run_dir / "r0").glob("*.json"))
    r1_files = list((tmp_run_dir / "r1").glob("*.json"))
    assert len(r0_files) == 5
    assert len(r1_files) == 5

    # Each per-persona file is valid JSON with the schema fields.
    for f in r0_files + r1_files:
        body = json.loads(f.read_text())
        assert "persona_id" in body
        assert "stance" in body
        assert 0.0 <= body["confidence"] <= 1.0
        # File name matches persona_id.
        assert f.stem == body["persona_id"]

    # Sanity: summary echoes 0 quarantines so len(per-round files) == 5 each.
    assert summary.quarantine_rate == 0.0
