"""Tests for scripts/swarm_calibration.py — EXP-OLLARMA-SWARM-002 runner.

Covers (PROMPT-002 §Add tests):

* smoke test the runner without a live Ollama daemon;
* verdict classification for PASS / CALIBRATION_BASELINE / FAIL;
* cold-start (round0->round1) vs steady-state JSD split;
* required artifact names + manifest fields.

The script lives under ``scripts/`` (not an importable package), so it is loaded
via ``importlib`` from its file path — the same shape a future scripts-package
refactor could replace without changing test intent.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from ollarma.swarm.schemas import SimulationSummary

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "swarm_calibration.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("swarm_calibration", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cal = _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summary(jsd: list[float], *, run_id: str, quarantine_rate: float = 0.0,
             wall_seconds: float = 1.0, n_personas: int = 50,
             n_rounds: int = 5) -> SimulationSummary:
    """Build a minimal SimulationSummary with a controlled JSD series."""
    return SimulationSummary(
        run_id=run_id,
        scenario_id=run_id,
        n_personas=n_personas,
        n_rounds=n_rounds,
        rounds=[],
        jsd_round_over_round=jsd,
        wall_seconds=wall_seconds,
        quarantine_rate=quarantine_rate,
    )


def _five_summaries(jsd_per_prompt: list[list[float]]) -> list[SimulationSummary]:
    assert len(jsd_per_prompt) == len(cal.INERT_PROMPTS)
    return [
        _summary(jsd, run_id=cal.INERT_PROMPTS[i]["prompt_id"])
        for i, jsd in enumerate(jsd_per_prompt)
    ]


def _evaluate(summaries: list[SimulationSummary]) -> dict:
    return cal.evaluate_calibration(
        cal.INERT_PROMPTS,
        summaries,
        model=cal.MODEL_TAG,
        ollama_evidence={"mode": "test"},
        mode="live",
    )


# ---------------------------------------------------------------------------
# split_jsd — cold-start vs steady-state
# ---------------------------------------------------------------------------

class TestSplitJsd:
    def test_t9b_inert_series_isolates_coldstart(self):
        # The committed T9b inert series: only the round0->round1 step is high.
        series = [0.0, 0.04120259398127841, 0.0, 0.010079270553765939, 0.0015317408310325025]
        split = cal.split_jsd(series)
        assert split["max_jsd"] == pytest.approx(0.04120259398127841)
        assert split["round0_to_round1_jsd"] == pytest.approx(0.04120259398127841)
        # Steady state (rounds 2+) is clean.
        assert split["max_later_round_jsd"] == pytest.approx(0.010079270553765939)
        assert split["max_later_round_jsd"] < 0.02

    def test_steady_state_wobble_surfaces_in_later_max(self):
        series = [0.0, 0.01, 0.031, 0.005]
        split = cal.split_jsd(series)
        assert split["round0_to_round1_jsd"] == pytest.approx(0.01)
        assert split["max_later_round_jsd"] == pytest.approx(0.031)
        assert split["max_jsd"] == pytest.approx(0.031)

    def test_short_and_empty_series_are_safe(self):
        assert cal.split_jsd([]) == {
            "max_jsd": 0.0,
            "round0_to_round1_jsd": 0.0,
            "max_later_round_jsd": 0.0,
        }
        # Single element (only the convention 0.0) — no cold-start, no later.
        assert cal.split_jsd([0.0]) == {
            "max_jsd": 0.0,
            "round0_to_round1_jsd": 0.0,
            "max_later_round_jsd": 0.0,
        }
        # Two elements — cold-start present, no steady state.
        s = cal.split_jsd([0.0, 0.03])
        assert s["round0_to_round1_jsd"] == pytest.approx(0.03)
        assert s["max_later_round_jsd"] == 0.0


# ---------------------------------------------------------------------------
# Verdict classification
# ---------------------------------------------------------------------------

class TestVerdictClassification:
    def test_pass_all_below_strict(self):
        summaries = _five_summaries([[0.0, 0.01, 0.005, 0.001, 0.0]] * 5)
        v = _evaluate(summaries)
        assert v["verdict"] == "PASS"
        assert v["summary_metrics"]["every_prompt_passed_strict_clean"] is True
        # PASS does not propose amendment.
        assert v["summary_metrics"]["supports_threshold_amendment"] is False

    def test_calibration_baseline_one_coldstart_over_strict(self):
        # Four clean prompts + one T9b-style cold-start wobble (0.0412), all <= 0.05.
        t9b = [0.0, 0.0412, 0.0, 0.0101, 0.0015]
        clean = [0.0, 0.01, 0.005, 0.001, 0.0]
        summaries = _five_summaries([clean, clean, t9b, clean, clean])
        v = _evaluate(summaries)
        assert v["verdict"] == "CALIBRATION_BASELINE"
        assert v["summary_metrics"]["every_prompt_passed_strict_clean"] is False
        # Cold-start is the sole driver; steady state clean -> amendment indicated.
        assert v["summary_metrics"]["supports_threshold_amendment"] is True
        assert "EXP-003" in v["next_gated_action"]

    def test_calibration_baseline_steady_state_wobble_blocks_amendment(self):
        # A prompt where the LATER rounds (not just cold-start) exceed 0.02.
        steady_wobble = [0.0, 0.01, 0.035, 0.04, 0.03]  # max 0.04 <= 0.05
        clean = [0.0, 0.01, 0.005, 0.001, 0.0]
        summaries = _five_summaries([clean, steady_wobble, clean, clean, clean])
        v = _evaluate(summaries)
        assert v["verdict"] == "CALIBRATION_BASELINE"
        # Steady-state wobble -> NOT a pure cold-start artifact -> no amendment.
        assert v["summary_metrics"]["supports_threshold_amendment"] is False

    def test_fail_exceeds_falsification_threshold(self):
        falsified = [0.0, 0.08, 0.02, 0.01, 0.0]  # max 0.08 > 0.05
        clean = [0.0, 0.01, 0.005, 0.001, 0.0]
        summaries = _five_summaries([clean, clean, clean, clean, falsified])
        v = _evaluate(summaries)
        assert v["verdict"] == "FAIL"
        assert "falsification" in v["verdict_reason"].lower()

    def test_boundary_exactly_strict_is_not_clean(self):
        # max JSD exactly == 0.02 is NOT < 0.02, so it is not strict-clean.
        boundary = [0.0, 0.02, 0.0, 0.0, 0.0]
        clean = [0.0, 0.01, 0.0, 0.0, 0.0]
        summaries = _five_summaries([boundary, clean, clean, clean, clean])
        v = _evaluate(summaries)
        assert v["verdict"] == "CALIBRATION_BASELINE"

    def test_boundary_exactly_falsification_is_not_fail(self):
        # max JSD exactly == 0.05 is NOT > 0.05, so falsification does not fire.
        boundary = [0.0, 0.05, 0.0, 0.0, 0.0]
        clean = [0.0, 0.01, 0.0, 0.0, 0.0]
        summaries = _five_summaries([boundary, clean, clean, clean, clean])
        v = _evaluate(summaries)
        assert v["verdict"] == "CALIBRATION_BASELINE"

    def test_claim_ceiling_is_pinned(self):
        summaries = _five_summaries([[0.0, 0.01, 0.0, 0.0, 0.0]] * 5)
        v = _evaluate(summaries)
        assert v["claim_ceiling"] == "HYPOTHESIS_STAGE_1"
        assert v["exp_id"] == "EXP-OLLARMA-SWARM-002"
        assert v["thresholds"]["inert_clean_jsd_max"] == 0.02
        assert v["thresholds"]["inert_falsification_threshold"] == 0.05

    def test_mismatched_counts_raise(self):
        with pytest.raises(ValueError):
            cal.evaluate_calibration(
                cal.INERT_PROMPTS,
                _five_summaries([[0.0, 0.01]] * 5)[:3],
                model=cal.MODEL_TAG,
                ollama_evidence={},
                mode="live",
            )


# ---------------------------------------------------------------------------
# Smoke run (no live Ollama) + required artifacts/manifest fields
# ---------------------------------------------------------------------------

class TestSmokeRunArtifacts:
    def test_smoke_run_emits_required_artifacts(self, tmp_path: Path):
        run_dir = tmp_path / "swarm_calibration_TEST"
        rc = cal.main(["--smoke", "--run-dir", str(run_dir)])
        # Stub responses are all-neutral -> clean -> PASS -> exit 0.
        assert rc == 0

        # Required top-level artifacts (PROMPT-002 §Expected Outputs).
        assert (run_dir / "manifest.json").is_file()
        assert (run_dir / "calibration_verdict.json").is_file()
        assert (run_dir / "audit_output_receipt.json").is_file()

        # One prompt_<n>/summary.json per inert prompt class.
        for i in range(1, len(cal.INERT_PROMPTS) + 1):
            assert (run_dir / f"prompt_{i}" / "summary.json").is_file()

    def test_manifest_fields(self, tmp_path: Path):
        run_dir = tmp_path / "swarm_calibration_TEST"
        cal.main(["--smoke", "--run-dir", str(run_dir)])
        manifest = json.loads((run_dir / "manifest.json").read_text())

        for key in (
            "schema_version", "exp_id", "prompt_id", "parent_prompt_id",
            "decision_packet", "generated_at_utc", "mode", "run_dir",
            "model_tag", "settings", "settings_match_t9b", "thresholds",
            "claim_ceiling", "single_writer", "prompts",
            "ollama_runtime_evidence",
        ):
            assert key in manifest, f"manifest missing {key}"

        assert manifest["exp_id"] == "EXP-OLLARMA-SWARM-002"
        assert manifest["model_tag"] == "qwen2.5-coder:7b"
        assert manifest["claim_ceiling"] == "HYPOTHESIS_STAGE_1"
        assert len(manifest["prompts"]) == 5
        # Smoke is N=2 x M=2 -> NOT a T9b-matching configuration.
        assert manifest["settings_match_t9b"] is False
        assert manifest["mode"] == "smoke"
        # All five named inert classes are present.
        kinds = {p["kind"] for p in manifest["prompts"]}
        assert kinds == {
            "lorem", "shuffled_neutral", "repeated_sentence",
            "random_tokens", "bland_factual",
        }

    def test_verdict_run_specific_requirements_present(self, tmp_path: Path):
        run_dir = tmp_path / "swarm_calibration_TEST"
        cal.main(["--smoke", "--run-dir", str(run_dir)])
        verdict = json.loads((run_dir / "calibration_verdict.json").read_text())

        # PROMPT-002 §Run-Specific Result Requirements — each must be reported.
        for p in verdict["per_prompt"]:
            assert "max_jsd" in p
            assert "round0_to_round1_jsd" in p
            assert "max_later_round_jsd" in p
            assert "quarantine_rate" in p
            assert "wall_seconds" in p
        sm = verdict["summary_metrics"]
        assert "every_prompt_passed_strict_clean" in sm
        assert "supports_threshold_amendment" in sm
        assert verdict["model_tag"] == "qwen2.5-coder:7b"
        assert "ollama_runtime_evidence" in verdict

    def test_provisional_audit_receipt_is_non_authoritative(self, tmp_path: Path):
        run_dir = tmp_path / "swarm_calibration_TEST"
        cal.main(["--smoke", "--run-dir", str(run_dir)])
        receipt = json.loads((run_dir / "audit_output_receipt.json").read_text())
        assert receipt["status"] == "PROVISIONAL"
        assert receipt["authoritative"] is False
        assert "EXP_CLOSEOUT_CONTRACT" in receipt["reason"]

    def test_smoke_resume_is_idempotent(self, tmp_path: Path):
        run_dir = tmp_path / "swarm_calibration_TEST"
        rc1 = cal.main(["--smoke", "--run-dir", str(run_dir)])
        summary_path = run_dir / "prompt_1" / "summary.json"
        first_mtime = summary_path.stat().st_mtime_ns
        # Re-run: existing summaries are reused, not regenerated.
        rc2 = cal.main(["--smoke", "--run-dir", str(run_dir)])
        assert rc1 == 0 and rc2 == 0
        assert summary_path.stat().st_mtime_ns == first_mtime
