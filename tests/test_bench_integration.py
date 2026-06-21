"""Integration tests for Phase 36 benchmark harness refresh."""
import datetime as dt
import pytest
from unittest.mock import patch, MagicMock
from ollarma.bench_refresh import get_contamination_label, is_primary_suite


class TestContaminationLabels:
    def test_humaneval_contamination_label(self):
        assert get_contamination_label("humaneval") == "possibly contaminated"

    def test_bigcodebench_uncontaminated(self):
        assert get_contamination_label("bigcodebench") == "uncontaminated"

    def test_mmlu_pro_not_primary(self):
        assert not is_primary_suite("mmlu_pro")


class TestSchemaVersion:
    def test_selection_artifact_has_schema_version_field(self):
        """ModelSelectionArtifact has schema_version field."""
        from ollarma.evidence import ModelSelectionArtifact
        fields = ModelSelectionArtifact.model_fields
        assert "schema_version" in fields

    def test_build_selection_artifact_defaults_to_v3(self):
        """build_selection_artifact produces schema_version: 3 by default.

        v3 adds the recorded min_quality_floor selection parameter.
        """
        from ollarma.evidence import build_selection_artifact
        artifact = build_selection_artifact("run-test", "deadbeef", stats=[])
        assert artifact.schema_version == "3"
        assert artifact.min_quality_floor == 0.5

    def test_build_selection_artifact_accepts_v1(self):
        """build_selection_artifact accepts explicit schema_version: 1."""
        from ollarma.evidence import build_selection_artifact
        artifact = build_selection_artifact("run-test", "deadbeef", stats=[], schema_version="1")
        assert artifact.schema_version == "1"

    def test_artifact_hash_includes_schema_version(self):
        """schema_version is included in the stable_decision_hash computation."""
        from ollarma.evidence import build_selection_artifact
        artifact_v1 = build_selection_artifact("run-test", "deadbeef", stats=[], schema_version="1")
        artifact_v2 = build_selection_artifact("run-test", "deadbeef", stats=[], schema_version="2")
        # Different schema_version -> different hashes
        assert artifact_v1.stable_decision_hash != artifact_v2.stable_decision_hash


class TestScorerRouting:
    def test_dispatch_scorer_routes_bigcodebench(self):
        """_dispatch_scorer routes bigcodebench suite to score_bigcodebench."""
        from ollarma.service import _dispatch_scorer
        from ollarma.executor import BenchmarkResult
        result = BenchmarkResult(
            model="qwen3:7b", task_id="bcb_001", suite="bigcodebench",
            num_ctx=4096, prefill_tps=None, decode_tps=100.0,
            quality_score=None, model_digest="sha256:abc", ollama_version="0.19.0",
            prompt_hash="abc123", schema_version="1",
            run_ts=dt.datetime.now(dt.timezone.utc), raw_response="print('hello')",
        )
        with patch("ollarma.bench_refresh.run_sandboxed_code", return_value=(0, "", "")):
            score = _dispatch_scorer(result)
        assert score == 1.0

    def test_dispatch_scorer_routes_mteb(self):
        """_dispatch_scorer routes mteb suite to score_mteb."""
        from ollarma.service import _dispatch_scorer
        from ollarma.executor import BenchmarkResult
        result = BenchmarkResult(
            model="nomic-embed-text", task_id="mteb_sts", suite="mteb",
            num_ctx=512, prefill_tps=None, decode_tps=200.0,
            quality_score=None, model_digest="sha256:def", ollama_version="0.19.0",
            prompt_hash="def456", schema_version="1",
            run_ts=dt.datetime.now(dt.timezone.utc), raw_response="",
        )
        with patch("ollarma.bench_refresh.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = '{"status": "ok", "model": "nomic-embed-text", "tasks": ["STS17"]}'
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc
            score = _dispatch_scorer(result)
        assert score == 1.0


class TestThermalLogging:
    def test_log_thermal_state_returns_required_fields(self):
        """log_thermal_state returns dict with soc_temp_c, thermal_ok, threshold_c."""
        from ollarma.bench_refresh import log_thermal_state
        with patch("ollarma.bench_refresh.read_soc_temperature_c", return_value=45.0):
            state = log_thermal_state()
        assert state["thermal_ok"] is True
        assert state["soc_temp_c"] == 45.0
        assert "threshold_c" in state
