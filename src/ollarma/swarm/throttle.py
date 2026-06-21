"""throttle.py -- Adaptive sleeper for the persona swarm engine.

Implements PROMPT-OLLARMA-SWARM-001 task T6 and the GPU-thermal hygiene
contract from PROMPT lines 132-136:

  > After every 100 persona-round responses, sleep 30s if the prior
  > 100-response wall is > 1.5x the smoothed median (heuristic
  > thermal-throttle detection).
  > Persist per-response wall to enable retroactive throttling analysis.

The sleeper is a small stateful object: the engine instantiates one per run,
calls ``record(idx, wall_ms)`` after every Ollama generate(), and ``time.sleep``s
for the returned milliseconds before issuing the next call. Every record() call
appends a JSONL line to ``<run_dir>/throttle.jsonl`` so the decision history
is auditable independent of the smoothed-median state.

Stdlib only -- ``collections.deque``, ``statistics.median``, ``time``, ``orjson``
(already a project dep).
"""
from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from statistics import median
from typing import Final

import orjson


# ---------------------------------------------------------------------------
# Defaults pulled directly from PROMPT lines 134-135.
# ---------------------------------------------------------------------------

DEFAULT_WINDOW: Final[int] = 100
"""Sliding-window length over per-response wall times (PROMPT line 134)."""

DEFAULT_THRESHOLD_RATIO: Final[float] = 1.5
"""Trigger sleep when window-aggregate wall > ratio x smoothed median."""

DEFAULT_SLEEP_MS: Final[int] = 30_000
"""Cooldown sleep in milliseconds (PROMPT line 135: '30s')."""


# ---------------------------------------------------------------------------
# AdaptiveSleeper
# ---------------------------------------------------------------------------

class AdaptiveSleeper:
    """Sliding-window thermal-throttle sleeper for sequential Ollama calls.

    State held in-memory:
      * ``_walls``: bounded deque of the last ``window`` per-response wall_ms
        values; older entries roll off automatically.

    State persisted to disk (append-only ``<run_dir>/throttle.jsonl``):
      * One JSON object per ``record()`` call, regardless of whether a sleep
        decision fired. This produces a complete throttle audit trail even
        if the engine crashes mid-run.

    Decision rule (matches PROMPT lines 134-135):
      * If the deque is full (``len == window``) AND the *most recent*
        wall_ms is > ``threshold_ratio * smoothed_median``, return
        ``DEFAULT_SLEEP_MS``. Otherwise return ``0``.

      Rationale for "most recent" rather than "sum of last 100": the PROMPT
      reads "the prior 100-response wall is > 1.5x the smoothed median",
      which is most naturally interpreted as a per-response anomaly above
      the rolling baseline (the smoothed median already summarises the
      window). Summing the window and comparing to ``ratio * window *
      median`` would always be approximately ``window * median`` regardless
      of throttling, which makes the test trivially false. Per-response
      comparison surfaces actual decode-rate degradation.

    The sleeper does NOT itself call ``time.sleep``; the engine owns the
    main loop and does the sleeping. This keeps the sleeper pure (modulo the
    JSONL append) and unit-testable without real sleeps.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        window: int = DEFAULT_WINDOW,
        threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
        sleep_ms: int = DEFAULT_SLEEP_MS,
    ) -> None:
        if window <= 0:
            raise ValueError(f"window must be positive, got {window}")
        if threshold_ratio <= 0.0:
            raise ValueError(
                f"threshold_ratio must be positive, got {threshold_ratio}"
            )
        if sleep_ms < 0:
            raise ValueError(f"sleep_ms must be non-negative, got {sleep_ms}")

        self.run_dir = run_dir
        self.window = window
        self.threshold_ratio = threshold_ratio
        self.sleep_ms = sleep_ms

        self._walls: deque[float] = deque(maxlen=window)
        self._jsonl_path: Path = run_dir / "throttle.jsonl"

        # Ensure the parent dir exists so the first record() doesn't crash.
        run_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, response_idx: int, wall_ms: float) -> int:
        """Record a per-response wall time and decide whether to sleep.

        Args:
            response_idx: Monotonic 0-indexed response counter across the
                whole simulation (engine increments for every persona-round).
            wall_ms: Wall clock time spent on this response, in milliseconds.

        Returns:
            ``sleep_ms`` if the throttle threshold tripped, else ``0``.
            Engine should ``time.sleep(returned_ms / 1000.0)`` before its
            next ``ollama.generate()`` call.
        """
        if wall_ms < 0:
            raise ValueError(f"wall_ms must be non-negative, got {wall_ms}")

        self._walls.append(float(wall_ms))

        # Smoothed median is well-defined as soon as the deque has any data,
        # but the throttle decision only fires when the window is full --
        # that's what makes it a *smoothed* baseline (PROMPT line 134).
        smoothed_median_ms: float = median(self._walls)

        sleep_decision_ms: int = 0
        if len(self._walls) == self.window:
            # Per-response anomaly above the baseline. See class docstring
            # for why this is preferred over "sum of window > ratio * window
            # * median" (which is degenerate).
            if wall_ms > self.threshold_ratio * smoothed_median_ms:
                sleep_decision_ms = self.sleep_ms

        # Persist BEFORE returning so an engine crash leaves an honest record.
        self._append_jsonl(
            {
                "ts": time.time(),
                "response_idx": response_idx,
                "wall_ms": float(wall_ms),
                "smoothed_median_ms": float(smoothed_median_ms),
                "sleep_ms": int(sleep_decision_ms),
                "window_filled": len(self._walls) == self.window,
            }
        )
        return sleep_decision_ms

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _append_jsonl(self, record: dict) -> None:
        """Append one JSON line to ``<run_dir>/throttle.jsonl``."""
        line = orjson.dumps(record, option=orjson.OPT_SORT_KEYS)
        # Open in append-binary mode so concurrent flushes (shouldn't happen
        # in the sequential engine, but cheap insurance) don't interleave.
        with self._jsonl_path.open("ab") as fh:
            fh.write(line)
            fh.write(b"\n")
