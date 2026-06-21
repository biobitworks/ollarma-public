"""tests/test_scorers.py — Unit tests for harness/scorers.py.

Covers SCORE-01 through SCORE-05. All tests run offline — no Ollama required.
Tests are written first (TDD RED) before harness/scorers.py exists.
"""
from __future__ import annotations

import datetime
import pathlib
import subprocess
import textwrap
import unittest.mock

import pytest

from ollarma.executor import BenchmarkResult
from ollarma.scorers import score_science, score_code, score_swarm, ROUTING_ORACLE


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_score_result(**overrides) -> BenchmarkResult:
    """Return a minimal BenchmarkResult for scorer tests."""
    defaults = dict(
        model="test-model",
        task_id="science_triage_01",
        suite="science",
        num_ctx=4096,
        prefill_tps=45.2,
        decode_tps=32.1,
        quality_score=None,
        model_digest="sha256:abc123deadbeef",
        ollama_version="ollama version 0.19.0",
        thinking_mode=False,
        prompt_hash="aabbccddeeff00112233",
        schema_version="1",
        run_ts=datetime.datetime.now(datetime.timezone.utc),
        raw_response="",
    )
    defaults.update(overrides)
    return BenchmarkResult(**defaults)


# ---------------------------------------------------------------------------
# Code fixtures
# ---------------------------------------------------------------------------

GOOD_FLATTEN = textwrap.dedent("""\
    def flatten(lst):
        result = []
        for item in lst:
            if isinstance(item, list):
                result.extend(flatten(item))
            else:
                result.append(item)
        return result
""")

BAD_FLATTEN = textwrap.dedent("""\
    def flatten(lst):
        return []
""")


# ---------------------------------------------------------------------------
# TestScienceScorer
# ---------------------------------------------------------------------------

class TestScienceScorer:
    """score_science(result) — parses JSON verdict, returns clamped confidence."""

    def test_score_science_support_verdict(self):
        """SUPPORT verdict with confidence=0.85 → returns 0.85."""
        r = _make_score_result(
            raw_response='{"verdict":"SUPPORT","confidence":0.85,"reasoning":"x"}'
        )
        assert score_science(r) == pytest.approx(0.85)

    def test_score_science_refute_verdict(self):
        """REFUTE verdict with confidence=0.7 → returns 0.7."""
        r = _make_score_result(
            raw_response='{"verdict":"REFUTE","confidence":0.7,"reasoning":"y"}'
        )
        assert score_science(r) == pytest.approx(0.7)

    def test_score_science_neutral_verdict(self):
        """NEUTRAL verdict with confidence=0.5 → returns 0.5."""
        r = _make_score_result(
            raw_response='{"verdict":"NEUTRAL","confidence":0.5,"reasoning":"z"}'
        )
        assert score_science(r) == pytest.approx(0.5)

    def test_score_science_invalid_verdict(self):
        """Unknown verdict → returns 0.0."""
        r = _make_score_result(
            raw_response='{"verdict":"UNKNOWN","confidence":0.9,"reasoning":"bad"}'
        )
        assert score_science(r) == 0.0

    def test_score_science_malformed_json(self):
        """Malformed JSON → returns 0.0."""
        r = _make_score_result(raw_response="not json")
        assert score_science(r) == 0.0

    def test_score_science_empty_response(self):
        """Empty raw_response → returns 0.0."""
        r = _make_score_result(raw_response="")
        assert score_science(r) == 0.0

    def test_score_science_clamps_above_one(self):
        """confidence=1.5 → clamped to 1.0."""
        r = _make_score_result(
            raw_response='{"verdict":"SUPPORT","confidence":1.5,"reasoning":"over"}'
        )
        assert score_science(r) == pytest.approx(1.0)

    def test_score_science_clamps_below_zero(self):
        """confidence=-0.1 → clamped to 0.0."""
        r = _make_score_result(
            raw_response='{"verdict":"SUPPORT","confidence":-0.1,"reasoning":"under"}'
        )
        assert score_science(r) == pytest.approx(0.0)

    def test_score_science_strips_thinking(self):
        """<think> block before JSON is stripped before json.loads → returns 0.9."""
        r = _make_score_result(
            raw_response=(
                '<think>reasoning</think>'
                '{"verdict":"SUPPORT","confidence":0.9,"reasoning":"y"}'
            )
        )
        assert score_science(r) == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# TestCodeScorer
# ---------------------------------------------------------------------------

class TestCodeScorer:
    """score_code(result) — AST parse + subprocess execution of generated code."""

    def test_score_code_good_flatten(self):
        """Correct flatten implementation → returns 1.0."""
        r = _make_score_result(
            task_id="code_generate_01",
            suite="code",
            raw_response=GOOD_FLATTEN,
        )
        assert score_code(r) == pytest.approx(1.0)

    def test_score_code_bad_flatten(self):
        """Flatten that always returns [] → returns 0.0."""
        r = _make_score_result(
            task_id="code_generate_01",
            suite="code",
            raw_response=BAD_FLATTEN,
        )
        assert score_code(r) == 0.0

    def test_score_code_syntax_error(self):
        """Python with syntax error → returns 0.0."""
        r = _make_score_result(
            task_id="code_generate_01",
            suite="code",
            raw_response="def foo(: pass",
        )
        assert score_code(r) == 0.0

    def test_score_code_empty_response(self):
        """Empty raw_response → returns 0.0."""
        r = _make_score_result(
            task_id="code_generate_01",
            suite="code",
            raw_response="",
        )
        assert score_code(r) == 0.0

    def test_score_code_fenced_good(self):
        """Correct flatten wrapped in ```python``` fences → returns 1.0."""
        fenced = "```python\n" + GOOD_FLATTEN + "```"
        r = _make_score_result(
            task_id="code_generate_01",
            suite="code",
            raw_response=fenced,
        )
        assert score_code(r) == pytest.approx(1.0)

    def test_score_code_timeout_handled(self):
        """subprocess.TimeoutExpired → returns 0.0 (no hang)."""
        r = _make_score_result(
            task_id="code_generate_01",
            suite="code",
            raw_response=GOOD_FLATTEN,
        )
        with unittest.mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("cmd", 10),
        ):
            assert score_code(r) == 0.0

    def test_score_code_unknown_task_returns_zero(self):
        """task_id not in _CODE_TEST_HARNESS → returns 0.0."""
        r = _make_score_result(
            task_id="unknown_task",
            suite="code",
            raw_response=GOOD_FLATTEN,
        )
        assert score_code(r) == 0.0


# ---------------------------------------------------------------------------
# TestSwarmScorer
# ---------------------------------------------------------------------------

class TestSwarmScorer:
    """score_swarm(result) — validates selected_model against ROUTING_ORACLE."""

    def test_score_swarm_correct_routing(self):
        """selected_model matches oracle, confidence=0.9 → returns 0.9."""
        r = _make_score_result(
            task_id="swarm_route_01",
            suite="swarm",
            raw_response='{"selected_model":"qwen3-coder:7b","confidence":0.9,"reason":"code"}',
        )
        assert score_swarm(r) == pytest.approx(0.9)

    def test_score_swarm_wrong_routing(self):
        """selected_model does not match oracle → returns 0.0."""
        r = _make_score_result(
            task_id="swarm_route_01",
            suite="swarm",
            raw_response='{"selected_model":"qwen3:8b","confidence":0.9,"reason":"wrong"}',
        )
        assert score_swarm(r) == 0.0

    def test_score_swarm_malformed_json(self):
        """Malformed JSON → returns 0.0."""
        r = _make_score_result(
            task_id="swarm_route_01",
            suite="swarm",
            raw_response="bad",
        )
        assert score_swarm(r) == 0.0

    def test_score_swarm_unknown_task(self):
        """task_id not in ROUTING_ORACLE → returns 0.0."""
        r = _make_score_result(
            task_id="unknown_swarm_task",
            suite="swarm",
            raw_response='{"selected_model":"qwen3-coder:7b","confidence":0.9,"reason":"x"}',
        )
        assert score_swarm(r) == 0.0

    def test_score_swarm_strips_thinking(self):
        """<think> block before JSON → stripped before parsing, returns correct score."""
        r = _make_score_result(
            task_id="swarm_route_01",
            suite="swarm",
            raw_response=(
                '<think>thinking block</think>'
                '{"selected_model":"qwen3-coder:7b","confidence":0.8,"reason":"ok"}'
            ),
        )
        assert score_swarm(r) == pytest.approx(0.8)

    def test_score_swarm_clamps_confidence(self):
        """confidence=2.0 → clamped to 1.0."""
        r = _make_score_result(
            task_id="swarm_route_01",
            suite="swarm",
            raw_response='{"selected_model":"qwen3-coder:7b","confidence":2.0,"reason":"over"}',
        )
        assert score_swarm(r) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# TestRoutingOracle
# ---------------------------------------------------------------------------

class TestRoutingOracle:
    """ROUTING_ORACLE dict — must cover all swarm tasks in tasks/swarm/."""

    def test_oracle_covers_all_swarm_tasks(self):
        """Every *.yml in tasks/swarm/ must have an entry in ROUTING_ORACLE."""
        tasks_dir = pathlib.Path("tasks/swarm")
        swarm_task_ids = {p.stem for p in tasks_dir.glob("*.yml")}
        for task_id in swarm_task_ids:
            assert task_id in ROUTING_ORACLE, (
                f"ROUTING_ORACLE missing entry for {task_id!r} — "
                "add an oracle entry when adding a new swarm task"
            )

    def test_oracle_swarm_route_01_value(self):
        """ROUTING_ORACLE['swarm_route_01'] must equal 'qwen3-coder:7b'."""
        assert ROUTING_ORACLE["swarm_route_01"] == "qwen3-coder:7b"
