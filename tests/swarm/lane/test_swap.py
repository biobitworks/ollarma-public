"""test_swap.py -- Phase 68 ``live_swap_percent_provider`` coverage.

Five observable paths:

1. Real call on this host -> returns a float in ``[0.0, 100.0]`` without
   raising. We do NOT pin a value -- the host may genuinely be under swap
   pressure (Wave 2 saw ~90%); the contract is only the range.
2. Unknown ``sys.platform`` -> fail-OPEN sentinel ``0.0``.
3. ``subprocess.run`` raises ``CalledProcessError`` -> fail-OPEN ``0.0``.
4. darwin ``sysctl -n vm.swapusage`` parse: stub stdout with known totals
   -> verify the percent arithmetic.
5. linux ``/proc/swaps`` parse: stub a 1-device file -> verify the percent
   arithmetic.
"""
from __future__ import annotations

import builtins
import io
import subprocess
import sys

import pytest

from ollarma.swarm.lane import swap as swap_module
from ollarma.swarm.lane.swap import live_swap_percent_provider


# ---------------------------------------------------------------------------
# 1. Real-host call: contract is only the range
# ---------------------------------------------------------------------------


def test_live_swap_percent_returns_float_in_range() -> None:
    """A real call on this host must return a float in [0.0, 100.0].

    No specific value is asserted -- the host may legitimately be under
    high swap pressure during the test run (Wave 2 observed ~90% on dev
    machines). The contract is fail-OPEN: even if probing fails, the
    function MUST return a float in range, never raise.
    """
    result = live_swap_percent_provider()
    assert isinstance(result, float)
    assert 0.0 <= result <= 100.0


# ---------------------------------------------------------------------------
# 2. Unknown platform -> 0.0 (fail-OPEN)
# ---------------------------------------------------------------------------


def test_live_swap_percent_unknown_platform_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sys.platform`` outside {darwin, linux*} hits the WARN-and-return-0
    branch.
    """
    monkeypatch.setattr(sys, "platform", "freebsd")
    assert live_swap_percent_provider() == 0.0


# ---------------------------------------------------------------------------
# 3. subprocess failure -> 0.0 (fail-OPEN)
# ---------------------------------------------------------------------------


def test_live_swap_percent_subprocess_failure_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``CalledProcessError`` from ``sysctl`` is caught and returns 0.0.

    Pin platform to darwin first so we exercise the subprocess path
    regardless of the actual host OS (Linux CI would skip this branch
    otherwise).
    """
    monkeypatch.setattr(sys, "platform", "darwin")

    def _raises(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(returncode=1, cmd=["sysctl"])

    monkeypatch.setattr(swap_module.subprocess, "run", _raises)
    assert live_swap_percent_provider() == 0.0


# ---------------------------------------------------------------------------
# 4. darwin parse: known stdout -> known percent
# ---------------------------------------------------------------------------


def test_live_swap_percent_darwin_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``sysctl -n vm.swapusage`` to a deterministic line; expect ~25%.

    Format: ``total = 4096.00M  used = 1024.00M  free = 3072.00M``
    -> 1024 / 4096 = 0.25 -> 25.0%.
    """
    monkeypatch.setattr(sys, "platform", "darwin")

    fake_completed = subprocess.CompletedProcess(
        args=["sysctl", "-n", "vm.swapusage"],
        returncode=0,
        stdout="total = 4096.00M  used = 1024.00M  free = 3072.00M\n",
        stderr="",
    )

    def _fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return fake_completed

    monkeypatch.setattr(swap_module.subprocess, "run", _fake_run)
    result = live_swap_percent_provider()
    assert result == pytest.approx(25.0, abs=0.01)


# ---------------------------------------------------------------------------
# 5. linux /proc/swaps parse: known content -> known percent
# ---------------------------------------------------------------------------


def test_live_swap_percent_linux_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``/proc/swaps`` to a 1-device file; expect ~25%.

    Strategy: monkeypatch ``builtins.open`` to intercept ``/proc/swaps``
    specifically and return a StringIO with stub content; delegate to the
    real ``open`` for any other path so logging / pytest internals stay
    intact.

    Format (kernel-stable since 2.4):
        Filename                Type        Size      Used    Priority
        /swap.img               file        2048      512     -2

    -> 512 / 2048 = 0.25 -> 25.0%.
    """
    monkeypatch.setattr(sys, "platform", "linux")

    real_open = builtins.open
    stub_content = (
        "Filename\t\t\t\tType\t\tSize\tUsed\tPriority\n"
        "/swap.img                               file\t\t2048\t512\t-2\n"
    )

    def _fake_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(file) == "/proc/swaps":
            return io.StringIO(stub_content)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _fake_open)
    result = live_swap_percent_provider()
    assert result == pytest.approx(25.0, abs=0.01)
