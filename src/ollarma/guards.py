"""guards.py — Pre-flight and data-integrity guards for the benchmark harness.

Three guard responsibilities:
1. warmup_model: discard cold-start-contaminated result before recording.
2. preflight_check: abort with PreflightError on dirty environment
   (loaded Ollama model, macOS swap pressure, MiroFish running).
3. strip_thinking: detect and strip <think>...</think> blocks from qwen3
   reasoning-mode responses before scoring.

All subprocess calls use shell=False and a timeout to prevent hangs.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class PreflightError(RuntimeError):
    """Raised by preflight_check() when the environment is not clean.

    Subclass of RuntimeError so callers can catch RuntimeError broadly.
    """


OLLAMA_API_PS_URL = "http://127.0.0.1:11434/api/ps"
LOCAL_MEMORY_BUDGET_MB = 12 * 1024


class RuntimeModelTelemetry(BaseModel):
    """Normalized per-model runtime facts collected from local probes."""

    model_config = ConfigDict(frozen=True)

    name: str
    processor: str | None = None
    context_length: int | None = None
    size_vram_bytes: int | None = None
    size_bytes: int | None = None


class RuntimeTelemetry(BaseModel):
    """Scheduler-ready local runtime telemetry snapshot."""

    model_config = ConfigDict(frozen=True)

    loaded_models: tuple[str, ...] = ()
    loaded_model_count: int = 0
    size_vram_bytes: int = 0
    swap_used_mb: float | None = None
    degraded_mode: bool = False
    degraded_reason: str | None = None
    telemetry_source: str = "unavailable"
    api_models: tuple[RuntimeModelTelemetry, ...] = ()


def _coerce_int(value: Any) -> int | None:
    """Best-effort conversion for integer-like telemetry values."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _parse_ollama_api_ps_payload(payload: Any) -> tuple[RuntimeModelTelemetry, ...]:
    """Normalize `GET /api/ps` responses into immutable model telemetry rows."""
    if isinstance(payload, dict):
        raw_models = payload.get("models", [])
    elif isinstance(payload, list):
        raw_models = payload
    else:
        raw_models = []

    models: list[RuntimeModelTelemetry] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("model") or raw.get("name") or "").strip()
        if not name:
            continue
        models.append(
            RuntimeModelTelemetry(
                name=name,
                processor=str(raw.get("processor")).strip() if raw.get("processor") else None,
                context_length=_coerce_int(raw.get("details", {}).get("context_length"))
                if isinstance(raw.get("details"), dict)
                else _coerce_int(raw.get("context_length")),
                size_vram_bytes=_coerce_int(raw.get("size_vram")),
                size_bytes=_coerce_int(raw.get("size")),
            )
        )
    return tuple(models)


def _fetch_ollama_api_ps(api_url: str = OLLAMA_API_PS_URL) -> tuple[RuntimeModelTelemetry, ...]:
    """Fetch runtime facts from Ollama's `GET /api/ps` endpoint."""
    try:
        with urllib.request.urlopen(api_url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        ValueError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ):
        return ()
    return _parse_ollama_api_ps_payload(payload)


def _parse_ollama_ps_output(stdout: str) -> tuple[str, ...]:
    """Extract loaded model names from `ollama ps` CLI output."""
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return ()
    names: list[str] = []
    for line in lines[1:]:
        fields = line.split()
        if fields:
            names.append(fields[0])
    return tuple(names)


def _collect_ollama_cli_ps() -> tuple[str, ...]:
    """Collect loaded model names from `ollama ps`."""
    try:
        result = subprocess.run(
            ["ollama", "ps"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ()
    return _parse_ollama_ps_output(result.stdout)


_SWAP_RE = re.compile(r"used\s*=\s*([0-9.]+)\s*([KMGTP])", re.IGNORECASE)


def _parse_swapusage_mb(stdout: str) -> float | None:
    """Parse `sysctl vm.swapusage` output into megabytes used."""
    match = _SWAP_RE.search(stdout)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    unit_scale = {
        "K": 1 / 1024,
        "M": 1,
        "G": 1024,
        "T": 1024 * 1024,
        "P": 1024 * 1024 * 1024,
    }
    return value * unit_scale[unit]


def _collect_swap_usage_mb() -> float | None:
    """Collect macOS swap usage via `sysctl vm.swapusage`."""
    try:
        result = subprocess.run(
            ["sysctl", "vm.swapusage"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return _parse_swapusage_mb(result.stdout)


def collect_runtime_telemetry(
    api_url: str = OLLAMA_API_PS_URL,
    *,
    memory_budget_mb: int = LOCAL_MEMORY_BUDGET_MB,
) -> RuntimeTelemetry:
    """Collect normalized scheduler telemetry from Ollama and host probes.

    Sources:
    - `GET /api/ps` for live loaded-model facts
    - `ollama ps` for loaded-model names and processor placement
    - `sysctl vm.swapusage` for host swap pressure / degraded mode
    """
    api_models = _fetch_ollama_api_ps(api_url)
    cli_models = _collect_ollama_cli_ps()

    names: list[str] = []
    for name in [*(model.name for model in api_models), *cli_models]:
        if name and name not in names:
            names.append(name)

    size_vram_bytes = sum(
        model.size_vram_bytes or model.size_bytes or 0 for model in api_models
    )
    swap_used_mb = _collect_swap_usage_mb()

    degraded_reason = None
    if swap_used_mb is not None and swap_used_mb > 0:
        degraded_reason = f"swap in use ({swap_used_mb:.1f} MB)"
    elif size_vram_bytes > memory_budget_mb * 1024 * 1024:
        footprint_mb = size_vram_bytes / (1024 * 1024)
        degraded_reason = (
            f"loaded model footprint {footprint_mb:.1f} MB exceeds "
            f"local budget {memory_budget_mb} MB"
        )

    sources: list[str] = []
    if api_models:
        sources.append("api/ps")
    if cli_models:
        sources.append("ollama ps")
    if swap_used_mb is not None:
        sources.append("sysctl vm.swapusage")

    return RuntimeTelemetry(
        loaded_models=tuple(names),
        loaded_model_count=len(names),
        size_vram_bytes=size_vram_bytes,
        swap_used_mb=swap_used_mb,
        degraded_mode=degraded_reason is not None,
        degraded_reason=degraded_reason,
        telemetry_source="+".join(sources) if sources else "unavailable",
        api_models=api_models,
    )


# ---------------------------------------------------------------------------
# strip_thinking
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking(text: str) -> tuple[str, bool]:
    """Strip all <think>...</think> blocks from *text*.

    Returns:
        (clean_text, thinking_mode)
        clean_text    — text with all <think> blocks removed, then strip()ed
        thinking_mode — True if at least one block was found and removed
    """
    has_thinking = bool(_THINK_RE.search(text))
    clean = _THINK_RE.sub("", text).strip()
    return (clean, has_thinking)


# ---------------------------------------------------------------------------
# preflight_check helpers
# ---------------------------------------------------------------------------

def _check_ollama_ps() -> None:
    """Raise PreflightError if any Ollama model is currently loaded.

    Parses `ollama ps` stdout; first line is header, remaining lines are body.
    T-02-05: shell=False, timeout=10.
    """
    result = subprocess.run(
        ["ollama", "ps"],
        capture_output=True,
        text=True,
        shell=False,
        timeout=10,
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    # lines[0] is the header row; lines[1:] are loaded models
    body = lines[1:]
    if body:
        names = [ln.split()[0] for ln in body if ln.split()]
        # Auto-unload instead of failing — external tools (Continue.dev,
        # VS Code extensions) may keep models loaded between runs.
        import time
        for name in names:
            subprocess.run(
                ["ollama", "stop", name],
                capture_output=True,
                timeout=30,
            )
        time.sleep(10)
        # Re-check after unload
        result2 = subprocess.run(
            ["ollama", "ps"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=10,
        )
        lines2 = [ln for ln in result2.stdout.splitlines() if ln.strip()]
        body2 = lines2[1:]
        if body2:
            still_loaded = [ln.split()[0] for ln in body2 if ln.split()]
            raise PreflightError(
                f"preflight: {len(still_loaded)} model(s) still loaded after auto-unload attempt: "
                + ", ".join(still_loaded)
            )


def _check_swap_zero() -> None:
    """Raise PreflightError if macOS swap usage is non-zero.

    Parses `vm_stat` stdout for 'Pages swapped out'.
    T-02-06: int() parse wrapped in try/except ValueError.
    T-02-05: shell=False, timeout=5.
    """
    result = subprocess.run(
        ["vm_stat"],
        capture_output=True,
        text=True,
        shell=False,
        timeout=5,
    )
    for line in result.stdout.splitlines():
        if "Pages swapped out" in line:
            # e.g. "Pages swapped out:                           0."
            raw = line.split(":")[-1].strip().rstrip(".")
            try:
                pages = int(raw)
            except ValueError:
                # malformed — silently skip per T-02-06
                return
            if pages > 0:
                raise PreflightError(
                    f"preflight: macOS swap is non-zero ({pages} pages swapped out) — "
                    "memory pressure will contaminate benchmark results. "
                    "Restart the machine or free memory before running."
                )
            return


def _check_mirofish_down() -> None:
    """Raise PreflightError if the MiroFish stack is running.

    Checks docker ps for mirofish containers, then pgrep for mirofish processes.
    FileNotFoundError is silently swallowed for both (tools may not be installed).
    T-02-05: shell=False, timeout specified.
    """
    # docker check
    try:
        docker_result = subprocess.run(
            ["docker", "ps", "--filter", "name=mirofish", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=10,
        )
        if docker_result.stdout.strip():
            raise PreflightError(
                "preflight: MiroFish docker container(s) are running — "
                "stop them before benchmarking to avoid memory contention: "
                + docker_result.stdout.strip()
            )
    except FileNotFoundError:
        pass  # docker not installed — skip

    # pgrep check
    try:
        pgrep_result = subprocess.run(
            ["pgrep", "-f", "mirofish"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=5,
        )
        if pgrep_result.returncode == 0 and pgrep_result.stdout.strip():
            raise PreflightError(
                "preflight: MiroFish process(es) detected (pgrep) — "
                "stop them before benchmarking: pids " + pgrep_result.stdout.strip()
            )
    except FileNotFoundError:
        pass  # pgrep not available — skip


def _log_power_mode() -> str:
    """Return the current macOS power mode string.

    Parses `pmset -g` output for 'lowpowermode'. Never raises.
    Returns 'low' if low power mode is active, 'high' otherwise, 'unknown' on error.
    T-02-04: power mode is benchmark metadata — not sensitive.
    T-02-05: shell=False, timeout=5.
    """
    try:
        result = subprocess.run(
            ["pmset", "-g"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if "lowpowermode" in line.lower():
                parts = line.strip().split()
                if len(parts) >= 2:
                    value = parts[-1].strip()
                    return "low" if value == "1" else "high"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# preflight_check
# ---------------------------------------------------------------------------

def preflight_check() -> str:
    """Run all pre-flight environment checks. Return the power mode string.

    Checks (in order):
    1. No Ollama models loaded (prevents contaminated cold-start timing)
    2. Zero macOS swap usage (prevents memory pressure degradation)
    3. MiroFish stack not running (prevents 32GB unified memory contention)
    4. Log power mode (metadata — never raises)

    Returns:
        str — "low", "high", or "unknown" (power mode from pmset)

    Raises:
        PreflightError: on first failed check with a human-readable message.
    """
    _check_ollama_ps()
    _check_swap_zero()
    _check_mirofish_down()
    return _log_power_mode()


# ---------------------------------------------------------------------------
# warmup_model
# ---------------------------------------------------------------------------

def _run_inference_for_warmup(
    model: str,
    prompt: str,
    task_id: str,
    suite: str,
    num_ctx: int,
) -> None:
    """Thin wrapper around run_inference — exists so tests can patch it.

    Lazy import of run_inference breaks potential circular import:
    executor.py may import strip_thinking from guards.py (Phase 02-02),
    so guards.py must NOT import executor at module level.
    """
    from ollarma.executor import run_inference  # lazy import — breaks circular dep
    run_inference(
        model=model,
        prompt=prompt,
        task_id=task_id,
        suite=suite,
        num_ctx=num_ctx,
    )


def warmup_model(model: str, prompt: str, num_ctx: int, warmed: set[str]) -> None:
    """Warm up *model* once and discard the result.

    If *model* is already in *warmed*, this is a no-op — run_inference is
    NOT called a second time. This prevents warmup from inflating timing data
    when the same model appears in multiple task suites.

    The warmup result is intentionally discarded — it is never written to store.

    Args:
        model:  Ollama model tag (e.g. "qwen3:8b")
        prompt: Prompt to use for warmup (same as first benchmark prompt)
        num_ctx: Context window size matching the benchmark task
        warmed: Mutable set tracking which models have been warmed this session
    """
    if model in warmed:
        return
    _run_inference_for_warmup(
        model=model,
        prompt=prompt,
        task_id="__warmup__",
        suite="__warmup__",
        num_ctx=num_ctx,
    )
    warmed.add(model)
