"""tests/test_reporter.py -- Unit tests for harness/reporter.py.

Covers REPT-01, REPT-02, REPT-03. All tests run offline -- no Ollama required.
Tests are written first (TDD RED) before harness/reporter.py exists.
"""
from __future__ import annotations

import pathlib

import orjson
import pytest

from ollarma.reporter import (
    aggregate_trials,
    composite_score,
    normalize_min_max,
    pareto_frontier_2d,
    extract_model_size,
    render_results_md,
    render_selection_md,
    load_sealed_results,
    find_latest_sealed,
    select_suite_winner,
    ModelSuiteStats,
    MIN_QUALITY_FLOOR,
    QUALITY_WEIGHT_DEFAULT,
    SPEED_WEIGHT_DEFAULT,
    QUALITY_WEIGHT_ROUTING,
    SPEED_WEIGHT_ROUTING,
)


def _mk_stats(model, suite, quality, tps, composite):
    """Build a ModelSuiteStats with a pre-set composite (quality-floor tests)."""
    return ModelSuiteStats(
        model=model,
        suite=suite,
        quality_mean=quality,
        quality_std=0.0,
        decode_tps_mean=tps,
        decode_tps_std=0.0,
        prefill_tps_mean=None,
        prefill_tps_std=0.0,
        ttft_ms=None,
        trial_count=3,
        composite_score=composite,
    )


class TestQualityFloorWinner:
    """#6: a fast-but-useless model must never win a suite over a usable one."""

    def test_floor_excludes_fast_zero_quality_model(self):
        # The quality-0 model has the higher (speed-weighted swarm) composite,
        # but the floor must keep it from winning over the 0.95 model.
        fast_zero = _mk_stats("qwen2.5:1.5b", "swarm", 0.0, 125.0, 0.60)
        accurate = _mk_stats("granite4.1:8b", "swarm", 0.95, 36.0, 0.50)
        assert select_suite_winner([fast_zero, accurate]).model == "granite4.1:8b"

    def test_best_composite_among_qualified_wins(self):
        a = _mk_stats("a", "code", 0.9, 50.0, 0.80)
        b = _mk_stats("b", "code", 0.7, 40.0, 0.60)
        assert select_suite_winner([a, b]).model == "a"

    def test_all_below_floor_falls_back_to_quality_not_speed(self):
        # Neither clears the floor; the faster one has the higher composite but
        # lower quality — quality-first fallback must pick the more accurate one.
        assert MIN_QUALITY_FLOOR > 0.45
        fast = _mk_stats("fast", "swarm", 0.2, 120.0, 0.50)
        accurate = _mk_stats("accurate", "swarm", 0.45, 20.0, 0.20)
        assert select_suite_winner([fast, accurate]).model == "accurate"

    def test_none_when_no_quality_scores(self):
        assert select_suite_winner([_mk_stats("x", "code", None, 50.0, 0.0)]) is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result_dict(**overrides) -> dict:
    """Return a dict matching BenchmarkResult.model_dump(mode='json') output.

    Reporter works with dicts from orjson deserialization, not BenchmarkResult objects.
    """
    defaults = dict(
        model="qwen3:8b",
        task_id="science_triage_01",
        suite="science",
        num_ctx=4096,
        prefill_tps=45.2,
        decode_tps=32.1,
        quality_score=0.85,
        model_digest="sha256:abc123deadbeef",
        ollama_version="ollama version 0.19.0",
        thinking_mode=False,
        prompt_hash="aabbccddeeff00112233",
        schema_version="1",
        run_ts="2026-04-07T00:00:00+00:00",
        raw_response="test response",
    )
    defaults.update(overrides)
    return defaults


def _make_sample_rows() -> list[dict]:
    """Return 9 dicts simulating a 3-model x 1-suite x 3-trial run.

    Models: qwen3:8b, phi4-mini, qwen3:1.7b
    Suite: science (all rows)
    Varied decode_tps and quality_score for realistic aggregation tests.
    """
    rows = []
    # qwen3:8b -- 3 trials
    for decode, quality in [(30.0, 0.90), (32.0, 0.85), (31.0, 0.88)]:
        rows.append(_make_result_dict(model="qwen3:8b", decode_tps=decode, quality_score=quality))
    # phi4-mini -- 3 trials
    for decode, quality in [(50.0, 0.70), (52.0, 0.72), (51.0, 0.68)]:
        rows.append(_make_result_dict(model="phi4-mini", decode_tps=decode, quality_score=quality))
    # qwen3:1.7b -- 3 trials
    for decode, quality in [(80.0, 0.50), (82.0, 0.55), (81.0, 0.52)]:
        rows.append(_make_result_dict(model="qwen3:1.7b", decode_tps=decode, quality_score=quality))
    return rows


# ---------------------------------------------------------------------------
# TestAggregation
# ---------------------------------------------------------------------------

class TestAggregation:
    """aggregate_trials groups rows and computes mean/std."""

    def test_groups_by_model_suite(self):
        """6 rows (2 models x 1 suite x 3 trials) -> 2 ModelSuiteStats entries."""
        rows = [
            _make_result_dict(model="a", decode_tps=10.0, quality_score=0.5),
            _make_result_dict(model="a", decode_tps=12.0, quality_score=0.6),
            _make_result_dict(model="a", decode_tps=11.0, quality_score=0.7),
            _make_result_dict(model="b", decode_tps=20.0, quality_score=0.8),
            _make_result_dict(model="b", decode_tps=22.0, quality_score=0.9),
            _make_result_dict(model="b", decode_tps=21.0, quality_score=0.85),
        ]
        stats = aggregate_trials(rows)
        models = {s.model for s in stats}
        assert models == {"a", "b"}

    def test_quality_mean_computed(self):
        """quality_mean is fmean of non-None quality_scores."""
        rows = [
            _make_result_dict(model="a", quality_score=0.6),
            _make_result_dict(model="a", quality_score=0.8),
            _make_result_dict(model="a", quality_score=1.0),
        ]
        stats = aggregate_trials(rows)
        s = [x for x in stats if x.model == "a"][0]
        assert s.quality_mean == pytest.approx(0.8)

    def test_decode_tps_mean_and_std(self):
        """decode_tps_mean and decode_tps_std computed correctly across 3 trials."""
        rows = [
            _make_result_dict(model="a", decode_tps=10.0),
            _make_result_dict(model="a", decode_tps=20.0),
            _make_result_dict(model="a", decode_tps=30.0),
        ]
        stats = aggregate_trials(rows)
        s = [x for x in stats if x.model == "a"][0]
        assert s.decode_tps_mean == pytest.approx(20.0)
        assert s.decode_tps_std == pytest.approx(10.0)

    def test_prefill_tps_mean_filters_none(self):
        """prefill_tps_mean computed only from non-None values."""
        rows = [
            _make_result_dict(model="a", prefill_tps=40.0),
            _make_result_dict(model="a", prefill_tps=None),
            _make_result_dict(model="a", prefill_tps=60.0),
        ]
        stats = aggregate_trials(rows)
        s = [x for x in stats if x.model == "a"][0]
        assert s.prefill_tps_mean == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases: N=1 trials, all-None quality, all-None prefill."""

    def test_single_trial_std_zero(self):
        """N=1 trial returns std=0.0 (not StatisticsError)."""
        rows = [_make_result_dict(model="a", decode_tps=25.0)]
        stats = aggregate_trials(rows)
        s = [x for x in stats if x.model == "a"][0]
        assert s.decode_tps_std == 0.0
        assert s.quality_std == 0.0

    def test_all_quality_none(self):
        """All quality_score=None in a group yields quality_mean=None (not 0.0)."""
        rows = [
            _make_result_dict(model="a", quality_score=None),
            _make_result_dict(model="a", quality_score=None),
            _make_result_dict(model="a", quality_score=None),
        ]
        stats = aggregate_trials(rows)
        s = [x for x in stats if x.model == "a"][0]
        assert s.quality_mean is None

    def test_all_prefill_none(self):
        """All prefill_tps=None yields prefill_tps_mean=None and ttft_ms=None."""
        rows = [
            _make_result_dict(model="a", prefill_tps=None),
            _make_result_dict(model="a", prefill_tps=None),
        ]
        stats = aggregate_trials(rows)
        s = [x for x in stats if x.model == "a"][0]
        assert s.prefill_tps_mean is None
        assert s.ttft_ms is None


# ---------------------------------------------------------------------------
# TestNormalization
# ---------------------------------------------------------------------------

class TestNormalization:
    """normalize_min_max edge cases."""

    def test_basic_normalization(self):
        """normalize_min_max([10, 20, 30]) returns [0.0, 0.5, 1.0]."""
        result = normalize_min_max([10.0, 20.0, 30.0])
        assert result == pytest.approx([0.0, 0.5, 1.0])

    def test_all_equal(self):
        """normalize_min_max([5, 5, 5]) returns [1.0, 1.0, 1.0]."""
        result = normalize_min_max([5.0, 5.0, 5.0])
        assert result == [1.0, 1.0, 1.0]

    def test_single_value(self):
        """normalize_min_max with single value returns [1.0]."""
        result = normalize_min_max([42.0])
        assert result == [1.0]


# ---------------------------------------------------------------------------
# TestComposite
# ---------------------------------------------------------------------------

class TestComposite:
    """composite_score applies REPT-03 weights."""

    def test_science_weights(self):
        """science suite: 0.8*0.7 + 0.6*0.3 = 0.74."""
        result = composite_score(quality=0.8, tps_normalized=0.6, suite="science")
        assert result == pytest.approx(0.74)

    def test_code_weights(self):
        """code suite uses same weights as science: 0.74."""
        result = composite_score(quality=0.8, tps_normalized=0.6, suite="code")
        assert result == pytest.approx(0.74)

    def test_swarm_weights(self):
        """swarm suite: 0.8*0.4 + 0.6*0.6 = 0.68."""
        result = composite_score(quality=0.8, tps_normalized=0.6, suite="swarm")
        assert result == pytest.approx(0.68)


# ---------------------------------------------------------------------------
# TestPareto
# ---------------------------------------------------------------------------

class TestPareto:
    """pareto_frontier_2d identifies non-dominated points."""

    def test_four_points(self):
        """Non-dominated points from [(0.5,0.5), (0.9,0.3), (0.3,0.9), (0.8,0.8)]."""
        points = [(0.5, 0.5), (0.9, 0.3), (0.3, 0.9), (0.8, 0.8)]
        result = pareto_frontier_2d(points)
        # (0.8,0.8) dominates (0.5,0.5). (0.9,0.3), (0.3,0.9), (0.8,0.8) are non-dominated.
        assert sorted(result) == [1, 2, 3]

    def test_single_point(self):
        """Single point returns [0]."""
        result = pareto_frontier_2d([(0.5, 0.5)])
        assert result == [0]

    def test_all_identical(self):
        """All identical points returns all indices."""
        result = pareto_frontier_2d([(0.5, 0.5), (0.5, 0.5), (0.5, 0.5)])
        assert sorted(result) == [0, 1, 2]


# ---------------------------------------------------------------------------
# TestModelSize
# ---------------------------------------------------------------------------

class TestModelSize:
    """extract_model_size parses model names."""

    def test_qwen3_8b(self):
        """'qwen3:8b' -> 8.0."""
        assert extract_model_size("qwen3:8b") == pytest.approx(8.0)

    def test_phi4_mini(self):
        """'phi4-mini' -> 3.8 (lookup table)."""
        assert extract_model_size("phi4-mini") == pytest.approx(3.8)

    def test_smollm2(self):
        """'smollm2' -> 1.7 (lookup table)."""
        assert extract_model_size("smollm2") == pytest.approx(1.7)

    def test_unknown_model(self):
        """'unknown-model' -> None."""
        assert extract_model_size("unknown-model") is None


# ---------------------------------------------------------------------------
# TestResultsMd
# ---------------------------------------------------------------------------

class TestResultsMd:
    """render_results_md produces valid Markdown pipe-tables."""

    def test_pipe_table_header(self):
        """Output contains '| Model |' header."""
        rows = _make_sample_rows()
        stats = aggregate_trials(rows)
        md = render_results_md(stats)
        assert "| Model |" in md

    def test_all_models_present(self):
        """Output contains all models from input data."""
        rows = _make_sample_rows()
        stats = aggregate_trials(rows)
        md = render_results_md(stats)
        assert "qwen3:8b" in md
        assert "phi4-mini" in md
        assert "qwen3:1.7b" in md

    def test_number_formatting(self):
        """Numbers formatted to 2 decimal places for scores, 1 for tps."""
        rows = [_make_result_dict(model="a", decode_tps=32.1234, quality_score=0.85678)]
        stats = aggregate_trials(rows)
        md = render_results_md(stats)
        # Score should be .2f format
        assert "0.86" in md
        # TPS should be .1f format
        assert "32.1" in md


# ---------------------------------------------------------------------------
# TestSelectionMd
# ---------------------------------------------------------------------------

class TestSelectionMd:
    """render_selection_md produces model selection guide."""

    def test_pareto_section(self):
        """Output contains 'Pareto Frontier' section."""
        rows = _make_sample_rows()
        stats = aggregate_trials(rows)
        md = render_selection_md(stats)
        assert "Pareto Frontier" in md or "Pareto frontier" in md

    def test_per_workload_winner(self):
        """Output contains per-workload winner table."""
        rows = _make_sample_rows()
        stats = aggregate_trials(rows)
        md = render_selection_md(stats)
        # Should mention at least one of the models as winner
        assert "qwen3:8b" in md or "phi4-mini" in md or "qwen3:1.7b" in md

    def test_small_model_recommendation(self):
        """Output contains small-model orchestrator recommendation."""
        rows = _make_sample_rows()
        stats = aggregate_trials(rows)
        md = render_selection_md(stats)
        # Should have a small-model section
        assert "orchestrator" in md.lower() or "small" in md.lower() or "< 4B" in md or "<4B" in md


# ---------------------------------------------------------------------------
# TestWeightsMetadata
# ---------------------------------------------------------------------------

class TestWeightsMetadata:
    """Rendered reports contain weight labels."""

    def test_results_md_weight_labels(self):
        """render_results_md output contains '0.7' and '0.3' weight labels."""
        rows = _make_sample_rows()
        stats = aggregate_trials(rows)
        md = render_results_md(stats)
        assert "0.7" in md
        assert "0.3" in md

    def test_selection_md_design_decision_language(self):
        """render_selection_md output contains 'design decision' or 'tunable' language."""
        rows = _make_sample_rows()
        stats = aggregate_trials(rows)
        md = render_selection_md(stats)
        assert "design decision" in md.lower() or "tunable" in md.lower()


# ---------------------------------------------------------------------------
# TestWeightsSource
# ---------------------------------------------------------------------------

class TestWeightsSource:
    """Weight constants are module-level with correct values."""

    def test_quality_weight_default(self):
        assert QUALITY_WEIGHT_DEFAULT == 0.7

    def test_speed_weight_default(self):
        assert SPEED_WEIGHT_DEFAULT == 0.3

    def test_quality_weight_routing(self):
        assert QUALITY_WEIGHT_ROUTING == 0.4

    def test_speed_weight_routing(self):
        assert SPEED_WEIGHT_ROUTING == 0.6


# ---------------------------------------------------------------------------
# TestLoadResults
# ---------------------------------------------------------------------------

class TestLoadResults:
    """load_sealed_results and find_latest_sealed I/O tests."""

    def test_load_sealed_reads_json(self, tmp_path: pathlib.Path):
        """load_sealed_results reads a JSON file and returns list of dicts."""
        data = [_make_result_dict(), _make_result_dict(model="phi4-mini")]
        path = tmp_path / "run-test.json"
        path.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2))
        result = load_sealed_results(path)
        assert len(result) == 2
        assert result[0]["model"] == "qwen3:8b"
        assert result[1]["model"] == "phi4-mini"

    def test_load_sealed_file_not_found(self, tmp_path: pathlib.Path):
        """load_sealed_results raises FileNotFoundError for missing file."""
        path = tmp_path / "run-missing.json"
        with pytest.raises(FileNotFoundError):
            load_sealed_results(path)

    def test_find_latest_sealed(self, tmp_path: pathlib.Path):
        """find_latest_sealed returns most recent run-*.json by filename sort."""
        # Create two sealed files with different timestamps
        (tmp_path / "run-2026-04-01T00:00:00Z.json").write_bytes(orjson.dumps([]))
        (tmp_path / "run-2026-04-02T00:00:00Z.json").write_bytes(orjson.dumps([]))
        result = find_latest_sealed(tmp_path)
        assert result.name == "run-2026-04-02T00:00:00Z.json"

    def test_find_latest_sealed_excludes_artifacts(self, tmp_path: pathlib.Path):
        """find_latest_sealed must not select .artifact.json or .evidence.json derivatives."""
        # Create a real sealed run and derivative files with later timestamps
        (tmp_path / "run-2026-04-01T00:00:00Z.json").write_bytes(orjson.dumps([]))
        (tmp_path / "run-2026-04-19T17:45:00Z.artifact.json").write_bytes(b"{}")
        (tmp_path / "run-2026-04-19T18:00:00Z.evidence.json").write_bytes(b"{}")
        result = find_latest_sealed(tmp_path)
        assert result.name == "run-2026-04-01T00:00:00Z.json"
