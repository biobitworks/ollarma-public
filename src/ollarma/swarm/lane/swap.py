"""swap.py -- Phase 68 live swap-pressure provider.

Public surface: :func:`live_swap_percent_provider` -- a zero-argument
``Callable[[], float]`` that the operator passes explicitly to
:func:`ollarma.swarm.lane.runtime.run_swarm_lane` /
:func:`ollarma.swarm.lane.runtime.resume_swarm_lane`. Returns a float in
``[0.0, 100.0]``.

Why stdlib only
---------------
psutil was rejected at smart-discuss time (CONTEXT 68 §"deferred"). The
project keeps zero new top-level deps; macOS + Linux both expose swap
state through stable stdlib-friendly surfaces:

* darwin -> ``sysctl -n vm.swapusage`` (stable since 10.x; reports total /
  used / free in megabytes).
* linux  -> ``/proc/swaps`` (kernel-stable since 2.4; per-device Used/Size
  rows in 1024-byte blocks).

Fail-OPEN semantics
-------------------
Any subprocess error, parse error, or unknown platform -> log WARN +
return ``0.0`` (treat as healthy). Rationale (CONTEXT 68 §decisions):
fail-closed (raise) would break the rescue path entirely -- the routing
ladder would NEVER execute on a host where swap probing failed, even
though most hosts run with low swap pressure. Surfacing the issue belongs
on the metrics channel, not in-band.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys


__all__ = ["live_swap_percent_provider"]


logger = logging.getLogger(__name__)


def live_swap_percent_provider() -> float:
    """Best-effort live swap-pressure measurement.

    Returns
    -------
    float
        Swap usage as a percent in ``[0.0, 100.0]``. ``0.0`` is also the
        fail-open sentinel: on ANY error (subprocess failure, parse
        failure, unknown platform) we log WARN and return ``0.0``.
    """
    try:
        if sys.platform == "darwin":
            return _darwin_swap_percent()
        if sys.platform.startswith("linux"):
            return _linux_swap_percent()
        logger.warning(
            "live_swap_percent_provider: unknown platform %r; returning 0.0",
            sys.platform,
        )
        return 0.0
    except Exception as exc:  # noqa: BLE001 -- fail-OPEN is the contract.
        logger.warning("live_swap_percent_provider failed: %s", exc)
        return 0.0


# ---------------------------------------------------------------------------
# Platform backends
# ---------------------------------------------------------------------------

# Matches the M-suffixed numbers in `sysctl -n vm.swapusage`:
#     total = 4096.00M  used = 1234.56M  free = 2861.44M  (encrypted)
_DARWIN_TOTAL_RE = re.compile(r"total\s*=\s*([0-9.]+)M")
_DARWIN_USED_RE = re.compile(r"used\s*=\s*([0-9.]+)M")


def _darwin_swap_percent() -> float:
    """macOS backend: parse ``sysctl -n vm.swapusage``.

    More reliable than ``vm_stat`` for swap-specific numbers (vm_stat
    reports page-level VM stats, not the swap file's own utilization).
    """
    completed = subprocess.run(
        ["sysctl", "-n", "vm.swapusage"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    out = completed.stdout
    total_match = _DARWIN_TOTAL_RE.search(out)
    used_match = _DARWIN_USED_RE.search(out)
    if total_match is None or used_match is None:
        logger.warning(
            "_darwin_swap_percent: could not parse vm.swapusage output: %r", out
        )
        return 0.0
    total_mb = float(total_match.group(1))
    used_mb = float(used_match.group(1))
    if total_mb <= 0.0:
        # No swap configured -> no pressure.
        return 0.0
    return _clamp_pct((used_mb / total_mb) * 100.0)


def _linux_swap_percent() -> float:
    """Linux backend: parse ``/proc/swaps``.

    File format (header + one row per swap device)::

        Filename                Type        Size      Used    Priority
        /swap.img               file        2097148   12345   -2

    Size and Used are in 1024-byte blocks. We sum across all devices and
    return ``used / size * 100``.
    """
    try:
        with open("/proc/swaps", "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        logger.warning("_linux_swap_percent: /proc/swaps not available")
        return 0.0
    # Header line + per-device rows. < 2 lines -> no swap configured.
    if len(lines) < 2:
        return 0.0
    total_size = 0
    total_used = 0
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            total_size += int(parts[2])
            total_used += int(parts[3])
        except ValueError:
            # Skip malformed rows; a corrupted single device shouldn't
            # zero the whole reading.
            continue
    if total_size <= 0:
        return 0.0
    return _clamp_pct((total_used / total_size) * 100.0)


def _clamp_pct(value: float) -> float:
    """Clamp ``value`` to ``[0.0, 100.0]``."""
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return value
