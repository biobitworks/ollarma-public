"""Tests for harness/store.py ResultStore — TDD RED phase.

These tests MUST FAIL before harness/store.py is implemented.
"""
import datetime
import pathlib
import re
import tempfile

import orjson
import pytest

from ollarma.executor import BenchmarkResult
from ollarma.store import ResultStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(**overrides) -> BenchmarkResult:
    defaults = dict(
        model="test-model",
        task_id="test_task",
        suite="science",
        num_ctx=4096,
        prefill_tps=45.2,
        decode_tps=32.1,
        quality_score=None,
        model_digest="sha256:abc123",
        ollama_version="ollama version 0.19.0",
        thinking_mode=False,
        prompt_hash="deadbeef" * 8,
        schema_version="1",
        run_ts=datetime.datetime.now(datetime.timezone.utc),
        raw_response="test response",
    )
    defaults.update(overrides)
    return BenchmarkResult(**defaults)


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------

class TestResultStoreInit:
    def test_jsonl_path_uses_run_id(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("2026-04-06T14:30:00Z")
        assert store.jsonl_path == pathlib.Path("results/run-2026-04-06T14:30:00Z.jsonl")

    def test_json_path_uses_run_id(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("2026-04-06T14:30:00Z")
        assert store.json_path == pathlib.Path("results/run-2026-04-06T14:30:00Z.json")

    def test_run_id_stored(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("2026-04-06T14:30:00Z")
        assert store.run_id == "2026-04-06T14:30:00Z"

    def test_invalid_run_id_raises(self, tmp_path, monkeypatch):
        """T-03-04: run_id must match ^[\\w:.-]+$ to prevent path traversal."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError):
            ResultStore("../../etc/passwd")
        with pytest.raises(ValueError):
            ResultStore("run id with spaces")
        with pytest.raises(ValueError):
            ResultStore("run/id/with/slashes")

    def test_valid_run_ids_accepted(self, tmp_path, monkeypatch):
        """Typical ISO timestamp run_ids must be accepted."""
        monkeypatch.chdir(tmp_path)
        ResultStore("2026-04-06T14:30:00Z")
        ResultStore("TEST")
        ResultStore("2026-04-06T14:30:00.123Z")


# ---------------------------------------------------------------------------
# append()
# ---------------------------------------------------------------------------

class TestAppend:
    def test_append_creates_results_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        assert not (tmp_path / "results").exists()
        store.append(_make_result())
        assert (tmp_path / "results").is_dir()

    def test_append_creates_jsonl_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        assert store.jsonl_path.exists()

    def test_append_writes_one_line(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        lines = [l for l in store.jsonl_path.read_bytes().split(b"\n") if l.strip()]
        assert len(lines) == 1

    def test_append_second_call_adds_second_line(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        store.append(_make_result(task_id="task_002"))
        lines = [l for l in store.jsonl_path.read_bytes().split(b"\n") if l.strip()]
        assert len(lines) == 2

    def test_append_row_is_valid_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        line = store.jsonl_path.read_bytes().strip()
        row = orjson.loads(line)
        assert isinstance(row, dict)

    def test_append_row_has_all_14_fields(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        row = orjson.loads(store.jsonl_path.read_bytes().strip())
        expected_fields = {
            "model", "task_id", "suite", "num_ctx",
            "prefill_tps", "decode_tps", "quality_score",
            "model_digest", "ollama_version", "thinking_mode",
            "prompt_hash", "schema_version", "run_ts", "raw_response",
        }
        assert set(row.keys()) == expected_fields

    def test_append_thinking_mode_is_false(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result(thinking_mode=False))
        row = orjson.loads(store.jsonl_path.read_bytes().strip())
        assert row["thinking_mode"] is False

    def test_append_schema_version_is_string_one(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result(schema_version="1"))
        row = orjson.loads(store.jsonl_path.read_bytes().strip())
        assert row["schema_version"] == "1"

    def test_append_quality_score_none_serializes_as_null(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result(quality_score=None))
        row = orjson.loads(store.jsonl_path.read_bytes().strip())
        assert row["quality_score"] is None

    def test_append_run_ts_serializes_as_iso_string(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        row = orjson.loads(store.jsonl_path.read_bytes().strip())
        assert isinstance(row["run_ts"], str), "run_ts must be ISO string, not datetime object"

    def test_append_uses_orjson(self):
        """Source-level: orjson.dumps is used, not json.dumps."""
        import inspect
        from ollarma import store
        src = inspect.getsource(store)
        assert "orjson.dumps" in src, "orjson.dumps not found in store.py source"

    def test_append_uses_binary_mode(self):
        """Source-level: append binary mode 'ab' is used for JSONL writes."""
        import inspect
        from ollarma import store
        src = inspect.getsource(store)
        assert '"ab"' in src or "'ab'" in src, "'ab' (append binary) not found in store.py"

    def test_append_uses_model_dump_mode_json(self):
        """Source-level: model_dump(mode='json') used for datetime serialization."""
        import inspect
        from ollarma import store
        src = inspect.getsource(store)
        assert 'mode="json"' in src or "mode='json'" in src


# ---------------------------------------------------------------------------
# seal()
# ---------------------------------------------------------------------------

class TestSeal:
    def test_seal_returns_json_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        result = store.seal()
        assert result == store.json_path

    def test_seal_creates_json_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        store.seal()
        assert store.json_path.exists()

    def test_seal_preserves_jsonl(self, tmp_path, monkeypatch):
        """seal() MUST NOT delete the .jsonl file (partial run recovery pattern D-17)."""
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        store.seal()
        assert store.jsonl_path.exists(), ".jsonl was deleted after seal — must be preserved"

    def test_seal_writes_json_array(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        store.seal()
        data = orjson.loads(store.json_path.read_bytes())
        assert isinstance(data, list)

    def test_seal_array_length_matches_appended(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result(task_id="t1"))
        store.append(_make_result(task_id="t2"))
        store.append(_make_result(task_id="t3"))
        store.seal()
        data = orjson.loads(store.json_path.read_bytes())
        assert len(data) == 3

    def test_seal_data_contains_correct_fields(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result(task_id="unique_task_id_123"))
        store.seal()
        data = orjson.loads(store.json_path.read_bytes())
        assert data[0]["task_id"] == "unique_task_id_123"

    def test_seal_run_ts_is_iso_string(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        store.seal()
        data = orjson.loads(store.json_path.read_bytes())
        assert isinstance(data[0]["run_ts"], str)

    def test_seal_does_not_modify_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = ResultStore("TEST")
        store.append(_make_result())
        before = store.jsonl_path.read_bytes()
        store.seal()
        after = store.jsonl_path.read_bytes()
        assert before == after, "seal() must not modify the .jsonl file"

    def test_seal_source_no_unlink(self):
        """Source-level: seal() must not call .unlink() on jsonl_path."""
        import inspect
        from ollarma import store
        src = inspect.getsource(store.ResultStore.seal)
        assert "unlink" not in src, "seal() must not delete the .jsonl (D-17)"


# ---------------------------------------------------------------------------
# Threat T-03-04: run_id validation
# ---------------------------------------------------------------------------

class TestRunIdValidation:
    def test_run_id_regex_pattern(self, tmp_path, monkeypatch):
        """Regex must be ^[\\w:.-]+$ — ISO timestamps and 'TEST' must pass."""
        monkeypatch.chdir(tmp_path)
        valid_ids = [
            "2026-04-06T14:30:00Z",
            "TEST",
            "run.id-with.dots-and-dashes:colons",
            "2026-04-06T14:30:00.123456Z",
        ]
        for rid in valid_ids:
            ResultStore(rid)  # Must not raise

    def test_path_traversal_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bad_ids = [
            "../etc/passwd",
            "../../secret",
            "/absolute/path",
            "has spaces",
            "has\nnewline",
        ]
        for bad in bad_ids:
            with pytest.raises(ValueError, match="run_id"):
                ResultStore(bad)
