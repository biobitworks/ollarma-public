"""Tests for PIPE-10 benchmark flag lifecycle and swap guard in run_benchmark()."""
import datetime as dt
import pathlib

import pytest

from ollarma.executor import BenchmarkResult
from ollarma.pipeline_control import PipelineController, get_pipeline_controller
from ollarma.registry import ModelConfig, TaskConfig
from ollarma.scheduler import SWAP_DEGRADED_THRESHOLD_MB, RuntimeSnapshot


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FAKE_MODEL = ModelConfig(name="fake-model:1b")
_FAKE_TASK = TaskConfig(id="t-001", suite="science", prompt="What is 2+2?", num_ctx=2048)
_FAKE_RESULT = BenchmarkResult(
    model="fake-model:1b",
    task_id="t-001",
    suite="science",
    num_ctx=2048,
    prefill_tps=100.0,
    decode_tps=50.0,
    quality_score=None,
    model_digest="sha256:fake",
    ollama_version="0.19.0",
    prompt_hash="abc123",
    schema_version="1",
    run_ts=dt.datetime.now(dt.timezone.utc),
    raw_response="4",
)


@pytest.fixture()
def bench_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Set up a fully isolated benchmark environment.

    Returns the PipelineController so tests can inspect the benchmark flag.
    """
    ctrl = PipelineController(state_dir=tmp_path)

    # Replace the singleton so get_pipeline_controller() returns our tmp_path controller
    monkeypatch.setattr("ollarma.pipeline_control._CONTROLLER", ctrl)

    # Provide a safe snapshot (no swap pressure)
    safe_snapshot = RuntimeSnapshot(queue_depth_by_lane={}, swap_used_mb=100.0)
    monkeypatch.setattr("ollarma.service.get_scheduler_snapshot", lambda: safe_snapshot)

    # Stub out model/task loading
    monkeypatch.setattr("ollarma.service.load_models", lambda: [_FAKE_MODEL])
    monkeypatch.setattr("ollarma.service.load_tasks", lambda: [_FAKE_TASK])

    # Neutralize the `ollama list` presence check (run_benchmark now skips roster
    # models absent from ollama). In this isolated env, treat all models present.
    monkeypatch.setattr("ollarma.service.list_present_models", lambda: [])
    monkeypatch.setattr("ollarma.service.model_is_present", lambda name, present=None: True)

    # Stub out preflight and warmup
    monkeypatch.setattr("ollarma.service.preflight_check", lambda: None)
    monkeypatch.setattr("ollarma.service.warmup_model", lambda *a, **kw: None)

    # Stub out scorer
    monkeypatch.setattr("ollarma.service._dispatch_scorer", lambda result: 1.0)

    # Stub out thermal/HF enforcement (lazy import inside run_benchmark)
    monkeypatch.setattr(
        "ollarma.bench_refresh.log_thermal_state",
        lambda: {"thermal_ok": True, "soc_temp_c": 40.0, "threshold_c": 70.0},
    )
    monkeypatch.setattr("ollarma.bench_refresh.enforce_hf_offline", lambda: None)

    return ctrl


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_benchmark_writes_flag_during_execution(
    bench_env: PipelineController, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """During a non-dry-run benchmark, benchmark_active.flag exists while inference runs."""
    from ollarma.service import run_benchmark

    ctrl = bench_env
    flag_seen_during_inference = False

    def _capturing_inference(**kwargs):
        nonlocal flag_seen_during_inference
        flag_seen_during_inference = ctrl._benchmark_flag.exists()
        return _FAKE_RESULT

    monkeypatch.setattr("ollarma.service.run_inference", _capturing_inference)

    run_benchmark(
        trials=1,
        skip_preflight=True,
        results_dir=tmp_path / "results",
    )

    assert flag_seen_during_inference, (
        "benchmark_active.flag should exist during inference execution"
    )


def test_run_benchmark_clears_flag_on_success(
    bench_env: PipelineController, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """After run_benchmark completes normally, benchmark_active.flag does not exist."""
    from ollarma.service import run_benchmark

    ctrl = bench_env
    monkeypatch.setattr("ollarma.service.run_inference", lambda **kw: _FAKE_RESULT)

    run_benchmark(
        trials=1,
        skip_preflight=True,
        results_dir=tmp_path / "results",
    )

    assert not ctrl._benchmark_flag.exists(), (
        "benchmark_active.flag should be cleared after successful completion"
    )


def test_run_benchmark_clears_flag_on_error(
    bench_env: PipelineController, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """When the only model's inference fails, the per-model backstop skips it,
    no rows seal, run_benchmark raises NoResultsError, and the flag still clears.

    (Per-model inference errors are no longer propagated raw — they are skipped
    so one bad model cannot abort a multi-model run. The flag-clearing guarantee
    on the outer try/finally is what this test pins.)
    """
    from ollarma.service import run_benchmark, NoResultsError

    ctrl = bench_env

    def _exploding_inference(**kwargs):
        raise RuntimeError("inference kaboom")

    monkeypatch.setattr("ollarma.service.run_inference", _exploding_inference)

    with pytest.raises(NoResultsError):
        run_benchmark(
            trials=1,
            skip_preflight=True,
            results_dir=tmp_path / "results",
        )

    assert not ctrl._benchmark_flag.exists(), (
        "benchmark_active.flag should be cleared even after an error (try/finally)"
    )


def test_run_benchmark_rejects_swap_pressure(
    bench_env: PipelineController, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """When swap > threshold AND rescue model is NOT resident, run_benchmark raises
    ValueError('SWAP_DEGRADED_NO_RESCUE') before the benchmark flag is written.

    GPU-07: the residency-aware guard distinguishes between swap-degraded-with-rescue
    (runs in degraded mode) and swap-degraded-without-rescue (hard block). This test
    locks in the hard-block path by explicitly forcing rescue_resident=False.
    """
    from ollarma.service import run_benchmark

    ctrl = bench_env

    # Override snapshot with high swap AND pin residency decision to no-rescue so
    # the test exercises the hard-block path regardless of what Ollama is doing
    # on the host.
    high_swap = RuntimeSnapshot(queue_depth_by_lane={}, swap_used_mb=SWAP_DEGRADED_THRESHOLD_MB + 100.0)
    monkeypatch.setattr("ollarma.service.get_scheduler_snapshot", lambda: high_swap)
    monkeypatch.setattr("ollarma.scheduler._read_memory_pressure_level", lambda: 2)
    monkeypatch.setattr(
        "ollarma.service.get_residency_decision",
        lambda: _fake_residency_decision(rescue_resident=False),
    )

    monkeypatch.setattr("ollarma.service.run_inference", lambda **kw: _FAKE_RESULT)

    with pytest.raises(ValueError, match="SWAP_DEGRADED"):
        run_benchmark(
            trials=1,
            skip_preflight=True,
            results_dir=tmp_path / "results",
        )

    assert not ctrl._benchmark_flag.exists(), (
        "benchmark_active.flag should never be written when swap is degraded"
    )


def test_run_benchmark_dry_run_skips_flag_and_swap(
    bench_env: PipelineController, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """dry_run=True does NOT write the flag or check swap, even if swap is degraded."""
    from ollarma.service import run_benchmark

    ctrl = bench_env

    # Swap is degraded, but dry-run should not care
    high_swap = RuntimeSnapshot(queue_depth_by_lane={}, swap_used_mb=SWAP_DEGRADED_THRESHOLD_MB + 100.0)
    monkeypatch.setattr("ollarma.service.get_scheduler_snapshot", lambda: high_swap)

    monkeypatch.setattr("ollarma.service.run_inference", lambda **kw: _FAKE_RESULT)

    # Should NOT raise ValueError for swap
    result = run_benchmark(
        dry_run=True,
        trials=1,
        skip_preflight=True,
        results_dir=tmp_path / "results",
    )

    assert result.dry_run is True
    assert not ctrl._benchmark_flag.exists(), (
        "benchmark_active.flag should never be written during dry-run"
    )


# ---------------------------------------------------------------------------
# GPU-07: Residency-aware benchmark guard
# ---------------------------------------------------------------------------


def _fake_residency_decision(*, rescue_resident: bool):
    """Build a minimal ResidencyDecision stand-in for the guard tests."""
    from ollarma.residency import ResidencyDecision, RESCUE_MODEL, OPTIONAL_STRONGER
    return ResidencyDecision(
        state="ready" if rescue_resident else "degraded",
        rescue_target=RESCUE_MODEL,
        rescue_resident=rescue_resident,
        opportunistic_target=OPTIONAL_STRONGER,
        opportunistic_resident=False,
        swap_used_mb=None,
        reason_code=None if rescue_resident else "RESCUE_LOADING",
        next_action="hold" if rescue_resident else "pin_rescue",
    )


def test_run_benchmark_blocks_on_critical_pressure(
    bench_env: PipelineController, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """Kernel memory pressure == CRITICAL (4) hard-blocks regardless of swap or residency."""
    from ollarma.service import run_benchmark

    ctrl = bench_env
    # Low swap, rescue resident — only the pressure signal should matter here.
    low_swap = RuntimeSnapshot(queue_depth_by_lane={}, swap_used_mb=100.0)
    monkeypatch.setattr("ollarma.service.get_scheduler_snapshot", lambda: low_swap)
    monkeypatch.setattr(
        "ollarma.scheduler._read_memory_pressure_level", lambda: 4,
    )
    monkeypatch.setattr(
        "ollarma.service.get_residency_decision",
        lambda: _fake_residency_decision(rescue_resident=True),
    )
    monkeypatch.setattr("ollarma.service.run_inference", lambda **kw: _FAKE_RESULT)

    with pytest.raises(ValueError, match="MEMORY_PRESSURE_CRITICAL"):
        run_benchmark(trials=1, skip_preflight=True, results_dir=tmp_path / "results")

    assert not ctrl._benchmark_flag.exists()


def test_run_benchmark_blocks_on_swap_degraded_no_rescue(
    bench_env: PipelineController, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """Swap over threshold AND rescue NOT resident -> hard block, no fallback path."""
    from ollarma.service import run_benchmark

    ctrl = bench_env
    high_swap = RuntimeSnapshot(queue_depth_by_lane={}, swap_used_mb=SWAP_DEGRADED_THRESHOLD_MB + 100.0)
    monkeypatch.setattr("ollarma.service.get_scheduler_snapshot", lambda: high_swap)
    monkeypatch.setattr("ollarma.scheduler._read_memory_pressure_level", lambda: 2)
    monkeypatch.setattr(
        "ollarma.service.get_residency_decision",
        lambda: _fake_residency_decision(rescue_resident=False),
    )
    monkeypatch.setattr("ollarma.service.run_inference", lambda **kw: _FAKE_RESULT)

    with pytest.raises(ValueError, match="SWAP_DEGRADED_NO_RESCUE"):
        run_benchmark(trials=1, skip_preflight=True, results_dir=tmp_path / "results")

    assert not ctrl._benchmark_flag.exists()


def test_run_benchmark_degraded_mode_constrains_to_rescue_model(
    bench_env: PipelineController, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """Swap over threshold AND rescue resident -> run benchmark on rescue model only."""
    from ollarma.residency import RESCUE_MODEL
    from ollarma.service import run_benchmark

    # Register the rescue model + a non-rescue model so we can see which one ran.
    rescue_cfg = ModelConfig(name=RESCUE_MODEL)
    other_cfg = ModelConfig(name="other:2b")
    monkeypatch.setattr("ollarma.service.load_models", lambda: [rescue_cfg, other_cfg])

    high_swap = RuntimeSnapshot(queue_depth_by_lane={}, swap_used_mb=SWAP_DEGRADED_THRESHOLD_MB + 100.0)
    monkeypatch.setattr("ollarma.service.get_scheduler_snapshot", lambda: high_swap)
    monkeypatch.setattr("ollarma.scheduler._read_memory_pressure_level", lambda: 2)
    monkeypatch.setattr(
        "ollarma.service.get_residency_decision",
        lambda: _fake_residency_decision(rescue_resident=True),
    )

    executed_models: list[str] = []

    def _record(**kw):
        executed_models.append(kw["model"])
        return _FAKE_RESULT.model_copy(update={"model": kw["model"]})

    monkeypatch.setattr("ollarma.service.run_inference", _record)

    # No models_filter supplied — the degraded-mode guard must force RESCUE only.
    result = run_benchmark(
        trials=1, skip_preflight=True, results_dir=tmp_path / "results",
    )

    assert result.rows_written >= 1
    assert executed_models, "expected at least one inference"
    assert set(executed_models) == {RESCUE_MODEL}, (
        f"degraded mode must constrain to rescue only; got {set(executed_models)}"
    )


def test_read_memory_pressure_level_returns_int_or_none():
    """Scheduler probe returns int on Darwin or None on non-Darwin / parse failure."""
    from ollarma.scheduler import _read_memory_pressure_level
    value = _read_memory_pressure_level()
    assert value is None or isinstance(value, int)
    if value is not None:
        # Must be one of the kernel's published levels; we do not assert a
        # specific value because test hosts vary.
        assert value in (1, 2, 4), f"unexpected pressure level: {value}"


def test_run_benchmark_skips_unrunnable_model_and_continues(
    bench_env: PipelineController, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """A present-but-un-runnable model (e.g. embedding-only -> generate 400) is
    skipped; the run still seals the other models' rows instead of aborting.

    Regression for the SELECTION_STALE lock: nomic-embed-text crashed the whole
    run after the generative models had already produced rows.
    """
    from ollarma.service import run_benchmark
    from ollarma.registry import ModelConfig

    good = ModelConfig(name="good:1b")
    bad = ModelConfig(name="embed-only:1b")
    monkeypatch.setattr("ollarma.service.load_models", lambda: [good, bad])
    # Both are present in `ollama list`, so the present-filter passes them both;
    # the per-model backstop is what must catch the runtime failure.
    monkeypatch.setattr(
        "ollarma.service.list_present_models", lambda: ["good:1b", "embed-only:1b"]
    )

    def _inference(**kwargs):
        if kwargs["model"] == "embed-only:1b":
            raise RuntimeError('"embed-only:1b" does not support generate (status code: 400)')
        return _FAKE_RESULT.model_copy(update={"model": "good:1b"})

    monkeypatch.setattr("ollarma.service.run_inference", _inference)

    result = run_benchmark(trials=1, skip_preflight=True, results_dir=tmp_path / "results")

    # Run completed (no raise) and sealed only the good model's row.
    assert result.rows_written == 1
    assert {r.model for r in result.results} == {"good:1b"}


def test_run_benchmark_raises_when_all_models_unrunnable(
    bench_env: PipelineController, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """If every model fails at inference, nothing seals and NoResultsError is raised."""
    from ollarma.service import run_benchmark
    from ollarma.service import NoResultsError
    from ollarma.registry import ModelConfig

    monkeypatch.setattr("ollarma.service.load_models", lambda: [ModelConfig(name="bad:1b")])
    monkeypatch.setattr("ollarma.service.list_present_models", lambda: ["bad:1b"])

    def _always_fail(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("ollarma.service.run_inference", _always_fail)

    with pytest.raises(NoResultsError):
        run_benchmark(trials=1, skip_preflight=True, results_dir=tmp_path / "results")
