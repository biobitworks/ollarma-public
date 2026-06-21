"""Schema contract tests -- verify BenchmarkResult fields and ResultStore behavior.
No Ollama required. All tests run offline.
"""
import datetime
import inspect
import pathlib
import sys

import orjson
import pytest

from ollarma.executor import BenchmarkResult
from ollarma.store import ResultStore

# ---------------------------------------------------------------------------
# bench source cache (loaded once per session for guard-wiring tests)
# ---------------------------------------------------------------------------

def _bench_source() -> str:
    """Return bench.py source via inspect. Import bench module at call time."""
    import importlib
    # Ensure a fresh import to reflect the current file on disk
    if "ollarma.cli" in sys.modules:
        bench_mod = sys.modules["ollarma.cli"]
    else:
        import ollarma.cli as bench_mod  # type: ignore[import]
    return inspect.getsource(bench_mod)


def _service_source() -> str:
    """Return ollarma/service.py source via inspect."""
    from ollarma import service as svc_mod
    return inspect.getsource(svc_mod)


MANDATORY_FIELDS = {
    "prefill_tps", "decode_tps", "quality_score", "model_digest",
    "ollama_version", "num_ctx", "thinking_mode", "prompt_hash",
    "schema_version", "run_ts", "raw_response",
}


def _make_result(**overrides) -> BenchmarkResult:
    defaults = dict(
        model="test-model",
        task_id="test_task_01",
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
        raw_response="This is a test response.",
    )
    defaults.update(overrides)
    return BenchmarkResult(**defaults)


def test_benchmark_result_mandatory_fields():
    """All 11 SKEL-01 mandatory fields must be present on a BenchmarkResult."""
    _make_result()  # validate construction succeeds
    model_fields = set(BenchmarkResult.model_fields.keys())
    missing = MANDATORY_FIELDS - model_fields
    assert not missing, f"BenchmarkResult missing mandatory fields: {missing}"


def test_prefill_tps_can_be_none():
    """prefill_tps=None is valid (divide-by-zero guard per D-20)."""
    r = _make_result(prefill_tps=None)
    assert r.prefill_tps is None


def test_quality_score_is_none_in_phase1():
    """quality_score=None is valid and intentional in Phase 1 (per D-07)."""
    r = _make_result(quality_score=None)
    assert r.quality_score is None


def test_schema_version_default():
    """schema_version defaults to '1' (per D-23)."""
    r = _make_result()
    assert r.schema_version == "1"


def test_thinking_mode_default():
    """thinking_mode defaults to False in Phase 1 (per D-24)."""
    r = _make_result()
    assert r.thinking_mode is False


def test_store_append_creates_jsonl(tmp_path, monkeypatch):
    """ResultStore.append() creates the JSONL file and writes one valid JSON line."""
    monkeypatch.setattr(ResultStore, "RESULTS_DIR", tmp_path / "results")
    store = ResultStore("test-run-001")
    r = _make_result()
    store.append(r)
    assert store.jsonl_path.exists(), "JSONL file not created"
    lines = [line.strip() for line in store.jsonl_path.read_bytes().split(b"\n") if line.strip()]
    assert len(lines) == 1, f"Expected 1 line, got {len(lines)}"
    row = orjson.loads(lines[0])
    assert row["schema_version"] == "1"
    assert row["thinking_mode"] is False
    assert row["quality_score"] is None


def test_store_seal_preserves_jsonl(tmp_path, monkeypatch):
    """After seal(), both .jsonl and .json exist (partial run preservation per D-17)."""
    monkeypatch.setattr(ResultStore, "RESULTS_DIR", tmp_path / "results")
    store = ResultStore("test-run-002")
    store.append(_make_result())
    sealed = store.seal()
    assert store.jsonl_path.exists(), "JSONL was deleted after seal -- must be preserved"
    assert sealed.exists(), "Sealed .json not created"


def test_jsonl_contains_all_mandatory_fields(tmp_path, monkeypatch):
    """Every JSONL row must contain all 11 SKEL-01 mandatory fields."""
    monkeypatch.setattr(ResultStore, "RESULTS_DIR", tmp_path / "results")
    store = ResultStore("test-run-003")
    store.append(_make_result())
    lines = [line.strip() for line in store.jsonl_path.read_bytes().split(b"\n") if line.strip()]
    row = orjson.loads(lines[0])
    missing = MANDATORY_FIELDS - set(row.keys())
    assert not missing, f"JSONL row missing mandatory fields: {missing}"


def test_run_ts_serializes_as_iso_string(tmp_path, monkeypatch):
    """run_ts must serialize as an ISO string, not a Python datetime repr."""
    monkeypatch.setattr(ResultStore, "RESULTS_DIR", tmp_path / "results")
    store = ResultStore("test-run-004")
    store.append(_make_result())
    lines = [line.strip() for line in store.jsonl_path.read_bytes().split(b"\n") if line.strip()]
    row = orjson.loads(lines[0])
    assert isinstance(row["run_ts"], str), f"run_ts should be string, got {type(row['run_ts'])}"
    # Should look like an ISO timestamp
    assert "T" in row["run_ts"] or "-" in row["run_ts"], f"run_ts does not look like ISO: {row['run_ts']}"


# ---------------------------------------------------------------------------
# Guard-wiring source tests (offline — no Ollama required)
# These tests inspect bench.py source to confirm guard integration is present.
# ---------------------------------------------------------------------------

def test_bench_run_imports_preflight_check():
    """ollarma/service.py must import preflight_check from ollarma.guards (GUARD-02 wiring)."""
    src = _service_source()
    assert "preflight_check" in src, (
        "ollarma/service.py does not reference preflight_check — guard is not wired into run_benchmark"
    )


def test_bench_run_imports_warmup_model():
    """ollarma/service.py must import warmup_model from ollarma.guards (GUARD-01 wiring)."""
    src = _service_source()
    assert "warmup_model" in src, (
        "ollarma/service.py does not reference warmup_model — warmup guard is not wired into run_benchmark"
    )


def test_bench_run_catches_preflight_error():
    """ollarma.cli.py must reference PreflightError to handle guard failures with Exit(1)."""
    src = _bench_source()
    assert "PreflightError" in src, (
        "ollarma.cli.py does not reference PreflightError — pre-flight failures will not be caught"
    )


def test_bench_dry_run_skips_guards():
    """dry-run branch must NOT contain a preflight_check() call.

    Source inspection: the dry_run block ends before the non-dry-run block begins.
    We verify this by checking that 'preflight_check' does not appear before the
    first occurrence of '# Non-dry-run' or 'if dry_run' resolves without calling it.

    Strategy: confirm that 'preflight_check' appears AFTER 'if dry_run:' block
    concludes (i.e., it is in the non-dry-run path only). We do this by verifying
    that 'preflight_check' does not appear inside the dry_run branch, which in
    bench.py's structure ends with 'return' before the non-dry-run block begins.
    """
    src = _bench_source()
    # The dry-run branch ends with 'return' before any guard call.
    # Locate the dry_run branch by finding the `if dry_run:` block and confirm
    # that 'preflight_check' only appears after 'return' that closes the dry_run block.
    lines = src.splitlines()
    in_dry_run_block = False
    dry_run_block_lines: list[str] = []
    for line in lines:
        if "if dry_run:" in line:
            in_dry_run_block = True
        if in_dry_run_block:
            dry_run_block_lines.append(line)
            # The dry_run block ends when we hit the dedented return at block end
            if line.strip() == "return" and in_dry_run_block:
                break
    dry_run_src = "\n".join(dry_run_block_lines)
    assert "preflight_check" not in dry_run_src, (
        "preflight_check() is called inside the dry_run branch — "
        "dry-run must skip guards per D-05/D-06"
    )
