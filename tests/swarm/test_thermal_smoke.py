"""test_thermal_smoke.py -- throttle persistence + sleep_ms decision smoke.

Runs N=10 x M=3 (30 responses) against the stub Ollama client with a per-
response wall schedule that creates a clear *per-response* anomaly above
the smoothed median. Asserts:

* throttle.jsonl exists and has exactly n_personas * n_rounds entries
* every line parses as JSON with the documented key set
* at least one entry has sleep_ms > 0 (the per-response anomaly trigger
  fired at least once)

Design notes (matches the as-shipped runtime, NOT the original plan draft):
  - ``AdaptiveSleeper.record()`` returns ``int`` milliseconds.
  - The throttle decision is per-response: ``sleep_ms > 0`` iff the window
    is full AND the most recent ``wall_ms`` exceeds
    ``threshold_ratio * smoothed_median``. The "window-sum" reading from
    the original draft is degenerate.

To trigger a decision in 30 responses we monkeypatch the AdaptiveSleeper
the engine instantiates so it uses ``window=10`` (test-only; production
default remains 100). The wall schedule is 100ms baseline with a 500ms
spike at the end of each of rounds 1 and 2 (response idx 19 and 29). At
those indices the window has already filled (since it filled at idx 9),
so 500ms > 1.5 * 100ms = 150ms triggers ``sleep_ms = DEFAULT_SLEEP_MS``.

PROMPT-OLLARMA-SWARM-001 task T8 (Phase 70 plan 70-03 wave 3).
"""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from ollarma.swarm import engine
from ollarma.swarm.engine import run_simulation
from ollarma.swarm.throttle import AdaptiveSleeper


# ---------------------------------------------------------------------------
# Wall schedule: 100ms baseline, 500ms spike at idx 19 and 29.
# ---------------------------------------------------------------------------

_BASELINE_MS = 100
_SPIKE_MS = 500
_SPIKE_AT = {19, 29}


def _wall_schedule(idx: int) -> int:
    return _SPIKE_MS if idx in _SPIKE_AT else _BASELINE_MS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_engine_clock(monkeypatch: pytest.MonkeyPatch, client) -> None:
    """Identical to test_engine_smoke._patch_engine_clock; duplicated to
    keep the two test files independently runnable."""
    monkeypatch.setattr(
        engine.time, "perf_counter", lambda: client.virtual_clock_seconds
    )
    monkeypatch.setattr(engine.time, "sleep", lambda _s: None)


def _patch_sleeper_window(monkeypatch: pytest.MonkeyPatch, window: int) -> None:
    """Force the engine-built AdaptiveSleeper to use a small window so a
    30-response simulation can fill the window and surface a decision.

    Production uses window=100 (PROMPT line 134); tests use 10 so 30
    responses cleanly fill the window and produce > 1 spike-window
    intersection.
    """
    Original = AdaptiveSleeper

    def _make(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("window", window)
        return Original(*args, **kwargs)

    monkeypatch.setattr(engine, "AdaptiveSleeper", _make)


# ---------------------------------------------------------------------------
# Test 1: at least one sleep_ms > 0 in throttle.jsonl
# ---------------------------------------------------------------------------

def test_throttle_persists_decisions(
    stub_ollama_client,
    tmp_run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """30 responses with two per-response spikes -> >=1 sleep_ms > 0 entry."""
    client = stub_ollama_client(wall_ms_per_response=_wall_schedule)
    _patch_engine_clock(monkeypatch, client)
    _patch_sleeper_window(monkeypatch, window=10)

    summary = run_simulation(
        scenario="Thermal smoke scenario.",
        scenario_id="thermal_test",
        n_personas=10,
        n_rounds=3,
        seed=42,
        run_dir=tmp_run_dir,
        ollama_client=client,
    )
    assert summary.n_personas == 10
    assert summary.n_rounds == 3

    throttle_path = tmp_run_dir / "throttle.jsonl"
    assert throttle_path.is_file(), "engine must persist throttle.jsonl"

    lines = [
        json.loads(line)
        for line in throttle_path.read_text().splitlines()
        if line.strip()
    ]
    # One line per persona-round response.
    assert len(lines) == 10 * 3 == 30

    # At least one decision fired (the 500ms spikes at idx 19 and 29).
    spiked = [e for e in lines if e["sleep_ms"] > 0]
    assert len(spiked) >= 1, (
        f"expected >= 1 sleep_ms > 0 entry, got 0; full series: "
        f"{[(e['response_idx'], e['wall_ms'], e['sleep_ms']) for e in lines]}"
    )

    # And the spike entries are at the indices we engineered. The wall_ms
    # the engine recorded must equal what the stub returned (validates the
    # virtual-clock plumbing).
    by_idx = {e["response_idx"]: e for e in lines}
    for spike_idx in _SPIKE_AT:
        e = by_idx[spike_idx]
        assert e["wall_ms"] == pytest.approx(_SPIKE_MS, abs=1e-6)
        assert e["sleep_ms"] > 0


# ---------------------------------------------------------------------------
# Test 2: every throttle.jsonl line has the documented key set
# ---------------------------------------------------------------------------

def test_throttle_jsonl_shape(
    stub_ollama_client,
    tmp_run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each throttle.jsonl line must parse as JSON with the documented
    keys: ts, response_idx, wall_ms, smoothed_median_ms, sleep_ms."""
    client = stub_ollama_client(wall_ms_per_response=_wall_schedule)
    _patch_engine_clock(monkeypatch, client)
    _patch_sleeper_window(monkeypatch, window=10)

    run_simulation(
        scenario="Thermal shape scenario.",
        scenario_id="thermal_shape",
        n_personas=10,
        n_rounds=3,
        seed=42,
        run_dir=tmp_run_dir,
        ollama_client=client,
    )

    throttle_path = tmp_run_dir / "throttle.jsonl"
    raw_lines = throttle_path.read_text().splitlines()
    assert raw_lines, "throttle.jsonl must not be empty"

    required = {"ts", "response_idx", "wall_ms", "smoothed_median_ms", "sleep_ms"}
    for raw in raw_lines:
        if not raw.strip():
            continue
        body = json.loads(raw)
        assert required.issubset(body.keys()), (
            f"throttle entry missing keys: have={set(body)} "
            f"required={required}"
        )
        # Type sanity.
        assert isinstance(body["response_idx"], int)
        assert isinstance(body["wall_ms"], (int, float))
        assert isinstance(body["smoothed_median_ms"], (int, float))
        # AdaptiveSleeper.record() returns int ms (per as-shipped runtime).
        assert isinstance(body["sleep_ms"], int)
        assert body["sleep_ms"] >= 0


# ---------------------------------------------------------------------------
# Suppress unused-import warnings for `partial` in test discovery (kept
# intentionally for future thermal-schedule helpers).
# ---------------------------------------------------------------------------
_ = partial
