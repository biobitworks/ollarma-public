"""Tests for BenchmarkResult schema and run_inference() — TDD RED phase."""
import pytest
from datetime import datetime, timezone
from typing import get_type_hints
from ollarma.executor import BenchmarkResult, run_inference
import inspect


def make_result(**overrides):
    defaults = dict(
        model="qwen3:8b",
        task_id="science_triage_01",
        suite="science",
        num_ctx=4096,
        prefill_tps=None,
        decode_tps=12.5,
        quality_score=None,
        model_digest="sha256:abc123",
        ollama_version="ollama version 0.19.0",
        thinking_mode=False,
        prompt_hash="aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899",
        schema_version="1",
        run_ts=datetime.now(timezone.utc),
        raw_response="test response",
    )
    defaults.update(overrides)
    return BenchmarkResult(**defaults)


class TestBenchmarkResultSchema:
    def test_has_all_14_fields(self):
        fields = set(BenchmarkResult.model_fields.keys())
        expected = {
            "model", "task_id", "suite", "num_ctx",
            "prefill_tps", "decode_tps", "quality_score",
            "model_digest", "ollama_version", "thinking_mode",
            "prompt_hash", "schema_version", "run_ts", "raw_response",
        }
        assert fields == expected, f"Missing: {expected - fields}, Extra: {fields - expected}"

    def test_prefill_tps_is_optional_float(self):
        r = make_result(prefill_tps=None)
        assert r.prefill_tps is None
        r2 = make_result(prefill_tps=55.3)
        assert r2.prefill_tps == 55.3

    def test_quality_score_is_optional_float(self):
        r = make_result(quality_score=None)
        assert r.quality_score is None

    def test_thinking_mode_defaults_false(self):
        r = make_result()
        assert r.thinking_mode is False

    def test_schema_version_defaults_one(self):
        r = make_result()
        assert r.schema_version == "1"

    def test_is_frozen(self):
        r = make_result()
        with pytest.raises(Exception):
            r.model = "other"

    def test_run_ts_is_datetime(self):
        r = make_result()
        assert isinstance(r.run_ts, datetime)

    def test_num_ctx_is_int(self):
        r = make_result(num_ctx=4096)
        assert isinstance(r.num_ctx, int)

    def test_decode_tps_is_float(self):
        r = make_result(decode_tps=25.0)
        assert isinstance(r.decode_tps, float)


class TestRunInferenceSource:
    """Verify source-level correctness without calling Ollama."""

    def test_source_has_separate_prefill_and_decode(self):
        src = inspect.getsource(run_inference)
        assert "prompt_eval_duration" in src
        assert "eval_duration" in src

    def test_source_has_divide_by_zero_guard(self):
        src = inspect.getsource(run_inference)
        assert "prompt_eval_duration == 0" in src or "prompt_eval_duration==0" in src

    def test_source_has_prompt_hash(self):
        src = inspect.getsource(run_inference)
        assert "sha256" in src or "prompt_hash" in src

    def test_source_calls_ollama_show(self):
        src = inspect.getsource(run_inference)
        assert "ollama show" in src or "ollama" in src and "show" in src

    def test_source_calls_ollama_version(self):
        src = inspect.getsource(run_inference)
        assert "ollama --version" in src or "--version" in src

    def test_source_uses_ollama_client(self):
        src = inspect.getsource(run_inference)
        assert "ollama.Client" in src or "Client()" in src

    def test_schema_version_hardcoded_one(self):
        src = inspect.getsource(run_inference)
        assert '"1"' in src or "'1'" in src

    def test_thinking_mode_derived_from_strip_thinking(self):
        # GUARD-03 (Phase 2): thinking_mode is now derived from strip_thinking(),
        # not hardcoded. Verify the wiring is present in source.
        src = inspect.getsource(run_inference)
        assert "strip_thinking" in src, "strip_thinking() must be called in run_inference"
        assert "thinking_mode=False" not in src, "hardcoded thinking_mode=False must be gone"


class TestHelperFunctions:
    def test_get_model_digest_imported(self):
        from ollarma.executor import _get_model_digest
        assert callable(_get_model_digest)

    def test_get_ollama_version_imported(self):
        from ollarma.executor import _get_ollama_version
        assert callable(_get_ollama_version)


class TestThreatMitigations:
    """T-01-01: model name validation before subprocess. T-01-04: timeout on subprocess."""

    def test_model_name_validation_rejects_shell_injection(self):
        from ollarma.executor import _validate_model_name
        with pytest.raises(ValueError):
            _validate_model_name("qwen3:8b; rm -rf /")
        with pytest.raises(ValueError):
            _validate_model_name("model$(evil)")

    def test_model_name_validation_accepts_valid(self):
        from ollarma.executor import _validate_model_name
        _validate_model_name("qwen3:8b")
        _validate_model_name("phi4-mini")
        _validate_model_name("qwen3.5:9b")
        _validate_model_name("qwen3-coder:7b")


class TestThinkingModeDetection:
    """TDD tests for GUARD-03: thinking-mode detection wired into run_inference().

    These tests use mocked Ollama client and subprocess calls so no real Ollama
    process is required.
    """

    def _make_mock_response(self, response_text: str):
        """Build a MagicMock that mimics the ollama.Client().generate() return value."""
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.response = response_text
        mock_resp.prompt_eval_count = 10
        mock_resp.prompt_eval_duration = 1_000_000_000  # 1 second in ns
        mock_resp.eval_count = 50
        mock_resp.eval_duration = 4_000_000_000         # 4 seconds in ns
        return mock_resp

    def _patch_subprocess(self, mocker_or_mock):
        """Return a context manager that patches subprocess.run for digest + version."""
        from unittest.mock import patch, MagicMock

        digest_result = MagicMock()
        digest_result.stdout = "digest      sha256:deadbeef\n"
        digest_result.returncode = 0

        version_result = MagicMock()
        version_result.stdout = "ollama version 0.19.0\n"
        version_result.returncode = 0

        return digest_result, version_result

    def test_run_inference_thinking_mode_detected(self):
        """thinking_mode is True when response contains a <think> block."""
        from unittest.mock import patch, MagicMock

        think_response = "<think>some reasoning here</think>\nActual answer"
        mock_resp = self._make_mock_response(think_response)

        digest_mock = MagicMock()
        digest_mock.stdout = "qwen3:8b    sha256:deadbeef    5.2 GB    1 hour ago\n"

        version_mock = MagicMock()
        version_mock.stdout = "ollama version 0.19.0\n"

        def fake_subprocess_run(args, **kwargs):
            if "list" in args:
                return digest_mock
            return version_mock

        mock_client = MagicMock()
        mock_client.generate.return_value = mock_resp

        with patch("subprocess.run", side_effect=fake_subprocess_run), \
             patch("ollama.Client", return_value=mock_client):
            result = run_inference(
                model="qwen3:8b",
                prompt="test prompt",
                task_id="test_01",
                suite="science",
                num_ctx=4096,
            )

        assert result.thinking_mode is True, (
            f"Expected thinking_mode=True for response with <think> block, got {result.thinking_mode}"
        )

    def test_run_inference_thinking_mode_false_without_block(self):
        """thinking_mode is False when response has no <think> block."""
        from unittest.mock import patch, MagicMock

        plain_response = "This is a plain answer with no think block."
        mock_resp = self._make_mock_response(plain_response)

        digest_mock = MagicMock()
        digest_mock.stdout = "phi4-mini    sha256:deadbeef    2.5 GB    1 hour ago\n"

        version_mock = MagicMock()
        version_mock.stdout = "ollama version 0.19.0\n"

        def fake_subprocess_run(args, **kwargs):
            if "list" in args:
                return digest_mock
            return version_mock

        mock_client = MagicMock()
        mock_client.generate.return_value = mock_resp

        with patch("subprocess.run", side_effect=fake_subprocess_run), \
             patch("ollama.Client", return_value=mock_client):
            result = run_inference(
                model="phi4-mini",
                prompt="test prompt",
                task_id="test_02",
                suite="code",
                num_ctx=4096,
            )

        assert result.thinking_mode is False, (
            f"Expected thinking_mode=False for plain response, got {result.thinking_mode}"
        )

    def test_run_inference_raw_response_preserved(self):
        """raw_response stores the ORIGINAL unstripped text (audit trail)."""
        from unittest.mock import patch, MagicMock

        think_response = "<think>chain of thought</think>\nFinal answer text"
        mock_resp = self._make_mock_response(think_response)

        digest_mock = MagicMock()
        digest_mock.stdout = "qwen3:8b    sha256:deadbeef    5.2 GB    1 hour ago\n"

        version_mock = MagicMock()
        version_mock.stdout = "ollama version 0.19.0\n"

        def fake_subprocess_run(args, **kwargs):
            if "list" in args:
                return digest_mock
            return version_mock

        mock_client = MagicMock()
        mock_client.generate.return_value = mock_resp

        with patch("subprocess.run", side_effect=fake_subprocess_run), \
             patch("ollama.Client", return_value=mock_client):
            result = run_inference(
                model="qwen3:8b",
                prompt="test prompt",
                task_id="test_03",
                suite="science",
                num_ctx=4096,
            )

        assert result.raw_response == think_response, (
            f"raw_response must preserve original text including <think> block. "
            f"Got: {result.raw_response!r}"
        )

    def test_run_inference_no_longer_hardcodes_thinking_mode(self):
        """Verify source: 'thinking_mode=False' (hardcoded literal) is absent from run_inference."""
        src = inspect.getsource(run_inference)
        assert "thinking_mode=False" not in src, (
            "run_inference() still has 'thinking_mode=False' hardcoded — "
            "this must be replaced with the strip_thinking() return value."
        )


class TestModelPresence:
    """list_present_models() / model_is_present() — used to skip un-pulled
    candidate-roster models so one absent model cannot abort a whole benchmark
    run (which previously left selection SELECTION_STALE).
    """

    _OLLAMA_LIST = (
        "NAME                 ID              SIZE      MODIFIED\n"
        "granite4.1:8b        aaaa            4.9 GB    1 hour ago\n"
        "qwen3.5:9b           bbbb            6.1 GB    1 hour ago\n"
        "phi4-mini:latest     cccc            2.3 GB    1 hour ago\n"
    )

    def _patch_list(self):
        from unittest.mock import patch, MagicMock

        mock = MagicMock()
        mock.stdout = self._OLLAMA_LIST
        return patch("subprocess.run", return_value=mock)

    def test_list_present_models_parses_name_column(self):
        from ollarma.executor import list_present_models

        with self._patch_list():
            names = list_present_models()
        assert names == ["granite4.1:8b", "qwen3.5:9b", "phi4-mini:latest"]

    def test_list_present_models_empty_on_failure(self):
        from unittest.mock import patch
        from ollarma.executor import list_present_models

        with patch("subprocess.run", side_effect=RuntimeError("ollama down")):
            assert list_present_models() == []

    def test_present_exact_and_prefix_match(self):
        from ollarma.executor import model_is_present

        present = ["granite4.1:8b", "qwen3.5:9b", "phi4-mini:latest"]
        # exact
        assert model_is_present("granite4.1:8b", present) is True
        assert model_is_present("qwen3.5:9b", present) is True
        # prefix: roster 'phi4-mini' matches installed 'phi4-mini:latest'
        assert model_is_present("phi4-mini", present) is True

    def test_absent_model_not_falsely_matched(self):
        from ollarma.executor import model_is_present

        present = ["granite4.1:8b", "qwen3.5:9b", "phi4-mini:latest"]
        # the real bug: granite4.1:3b must NOT match granite4.1:8b
        assert model_is_present("granite4.1:3b", present) is False
        assert model_is_present("deepseek-r1:14b", present) is False


class TestNumPredictCap:
    """#4: a bounded decode-token cap so verbose/thinking models cannot run
    unbounded in the generic benchmark executor (the swarm engine already caps
    at DEFAULT_NUM_PREDICT=200; this guards the general run_inference path).
    """

    def _options_from_run(self, **kwargs) -> dict:
        from unittest.mock import patch, MagicMock

        resp = MagicMock()
        resp.response = "answer"
        resp.prompt_eval_count = 10
        resp.prompt_eval_duration = 1_000_000_000
        resp.eval_count = 50
        resp.eval_duration = 4_000_000_000

        digest = MagicMock()
        digest.stdout = "qwen3:8b    sha256:deadbeef    5.2 GB    1 hour ago\n"
        version = MagicMock()
        version.stdout = "ollama version 0.24.0\n"

        def fake_run(args, **kw):
            return digest if "list" in args else version

        client = MagicMock()
        client.generate.return_value = resp
        with patch("subprocess.run", side_effect=fake_run), \
             patch("ollama.Client", return_value=client):
            run_inference(
                model="qwen3:8b",
                prompt="p",
                task_id="t",
                suite="science",
                num_ctx=4096,
                **kwargs,
            )
        return client.generate.call_args.kwargs["options"]

    def test_default_cap_applied_when_unset(self):
        from ollarma.executor import DEFAULT_NUM_PREDICT

        opts = self._options_from_run()
        assert opts["num_predict"] == DEFAULT_NUM_PREDICT

    def test_explicit_cap_passed_through(self):
        opts = self._options_from_run(num_predict=256)
        assert opts["num_predict"] == 256

    def test_nonpositive_falls_back_to_default_never_unlimited(self):
        from ollarma.executor import DEFAULT_NUM_PREDICT

        assert self._options_from_run(num_predict=0)["num_predict"] == DEFAULT_NUM_PREDICT
        assert self._options_from_run(num_predict=-1)["num_predict"] == DEFAULT_NUM_PREDICT

    def test_source_passes_num_predict_option(self):
        src = inspect.getsource(run_inference)
        assert "num_predict" in src
