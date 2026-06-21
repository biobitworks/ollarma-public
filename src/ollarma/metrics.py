"""metrics.py — Simple in-process Prometheus-style metrics registry (HARDEN-02).

No OTel SDK required. Exposes counters and gauges in Prometheus text format.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


class MetricsRegistry:
    """Thread-safe in-process metrics registry for Prometheus text exposition."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        self._gauges: dict[str, float] = {}
        self._gauge_help: dict[str, str] = {}
        self._counter_help: dict[str, str] = {}

    def counter_inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0, help_text: str = "") -> None:
        """Increment a counter metric."""
        key = tuple(sorted((labels or {}).items()))
        with self._lock:
            self._counters[name][key] += value
            if help_text and name not in self._counter_help:
                self._counter_help[name] = help_text

    def gauge_set(self, name: str, value: float, help_text: str = "") -> None:
        """Set a gauge metric value."""
        with self._lock:
            self._gauges[name] = value
            if help_text:
                self._gauge_help[name] = help_text

    def expose_text(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            for name, label_map in sorted(self._counters.items()):
                help_text = self._counter_help.get(name, name)
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} counter")
                for labels, val in sorted(label_map.items()):
                    label_str = ",".join(f'{k}="{v}"' for k, v in labels)
                    suffix = f"{{{label_str}}}" if label_str else ""
                    lines.append(f"{name}{suffix} {val}")

            for name, val in sorted(self._gauges.items()):
                help_text = self._gauge_help.get(name, name)
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name} {val}")

        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Reset all metrics (for testing)."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()


# Module-level singleton
_registry = MetricsRegistry()


def get_registry() -> MetricsRegistry:
    """Return the global metrics registry singleton."""
    return _registry


def collect_pipeline_gauges() -> None:
    """Populate pipeline gauges from live state (best-effort)."""
    try:
        from ollarma.service import get_scheduler
        snapshot = get_scheduler().snapshot()
        swap_mb = getattr(snapshot, "swap_used_mb", 0.0) or 0.0
        _registry.gauge_set("ollarma_swap_used_mb", swap_mb, "macOS swap memory used in MB")
    except Exception:
        pass

    try:
        from ollarma.pipeline_control import get_pipeline_controller
        ctrl = get_pipeline_controller()
        pinned = sum(
            1 for ps in ctrl._pin_state.values() if ps.pin_count > 0
        )
        _registry.gauge_set("ollarma_pinned_models", float(pinned), "Number of pinned models")
        for model, ps in ctrl._pin_state.items():
            tps = ps.decode_tps_ewma or 0.0
            _registry.gauge_set(
                f"ollarma_decode_tps",
                tps,
                f"Decode tokens/sec EWMA for {model}",
            )
    except Exception:
        pass
