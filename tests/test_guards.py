"""Tests for harness/guards.py — TDD RED phase.

Covers: warmup_model, preflight_check, strip_thinking, PreflightError.
All tests run offline — no Ollama required.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from ollarma.guards import (
    PreflightError,
    collect_runtime_telemetry,
    preflight_check,
    strip_thinking,
    warmup_model,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proc(stdout: str = "", returncode: int = 0) -> MagicMock:
    """Return a mock subprocess.CompletedProcess."""
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# warmup_model
# ---------------------------------------------------------------------------

class TestWarmupModel:
    """warmup_model(model, prompt, num_ctx, warmed) — no side effects on warmed set."""

    def test_warmup_calls_run_inference_once(self):
        """First call to warmup_model triggers exactly one run_inference call."""
        warmed: set[str] = set()
        with patch("ollarma.guards._run_inference_for_warmup") as mock_ri:
            warmup_model("qwen3:8b", "hello", 4096, warmed)
        mock_ri.assert_called_once()

    def test_warmup_skips_second_call(self):
        """Second call with same model is a no-op — run_inference called exactly once total."""
        warmed: set[str] = set()
        with patch("ollarma.guards._run_inference_for_warmup") as mock_ri:
            warmup_model("qwen3:8b", "hello", 4096, warmed)
            warmup_model("qwen3:8b", "hello", 4096, warmed)
        mock_ri.assert_called_once()

    def test_warmup_calls_once_per_model(self):
        """Two different models → two run_inference calls."""
        warmed: set[str] = set()
        with patch("ollarma.guards._run_inference_for_warmup") as mock_ri:
            warmup_model("qwen3:8b", "hello", 4096, warmed)
            warmup_model("phi4-mini", "hello", 4096, warmed)
        assert mock_ri.call_count == 2

    def test_warmup_uses_warmup_sentinel(self):
        """run_inference called with task_id='__warmup__' and suite='__warmup__'."""
        warmed: set[str] = set()
        with patch("ollarma.guards._run_inference_for_warmup") as mock_ri:
            warmup_model("qwen3:8b", "hello", 4096, warmed)
        call_kwargs = mock_ri.call_args
        # Can be positional or keyword — check both
        args, kwargs = call_kwargs
        all_args = list(args) + list(kwargs.values())
        assert "__warmup__" in all_args or (
            kwargs.get("task_id") == "__warmup__" and kwargs.get("suite") == "__warmup__"
        )

    def test_warmup_adds_to_warmed_set(self):
        """Model name appears in warmed set after call."""
        warmed: set[str] = set()
        with patch("ollarma.guards._run_inference_for_warmup"):
            warmup_model("qwen3:8b", "hello", 4096, warmed)
        assert "qwen3:8b" in warmed

    def test_warmup_discards_result(self):
        """warmup_model returns None."""
        warmed: set[str] = set()
        with patch("ollarma.guards._run_inference_for_warmup"):
            result = warmup_model("qwen3:8b", "hello", 4096, warmed)
        assert result is None


# ---------------------------------------------------------------------------
# preflight_check
# ---------------------------------------------------------------------------

CLEAN_OLLAMA_PS = "NAME\tID\tSIZE\tPROCESSOR\tUNTIL\n"
LOADED_OLLAMA_PS = (
    "NAME\tID\tSIZE\tPROCESSOR\tUNTIL\n"
    "qwen3:8b\tabc123\t5.2GB\t100%\t4 minutes from now\n"
)
CLEAN_VMSTAT = (
    "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
    "Pages free:                               1000.\n"
    "Pages swapped out:                           0.\n"
)
NONZERO_VMSTAT = (
    "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
    "Pages free:                                500.\n"
    "Pages swapped out:                         100.\n"
)
CLEAN_PMSET = "System-wide power settings:\ncurrently in use:\n lowpowermode         0\n"
LOW_PMSET = "System-wide power settings:\ncurrently in use:\n lowpowermode         1\n"


def _patch_all_clean(**kwargs):
    """Context manager: patch subprocess so all checks pass, pmset returns high."""
    defaults = {
        "ollama_ps": _make_proc(CLEAN_OLLAMA_PS),
        "vmstat": _make_proc(CLEAN_VMSTAT),
        "docker_ps": _make_proc(""),
        "pgrep": _make_proc("", returncode=1),
        "pmset": _make_proc(CLEAN_PMSET),
    }
    defaults.update(kwargs)

    def side_effect(args, **kw):
        cmd = args[0] if args else ""
        if cmd == "ollama":
            return defaults["ollama_ps"]
        if cmd == "vm_stat":
            return defaults["vmstat"]
        if cmd == "docker":
            return defaults["docker_ps"]
        if cmd == "pgrep":
            return defaults["pgrep"]
        if cmd == "pmset":
            return defaults["pmset"]
        return _make_proc("")

    return patch("subprocess.run", side_effect=side_effect)


class TestPreflightCheck:
    """preflight_check() raises PreflightError on dirty environment; returns power mode string."""

    def test_preflight_raises_on_loaded_model(self):
        """ollama ps body line present → PreflightError with 'loaded models' in message."""
        def side_effect(args, **kw):
            if args[0] == "ollama":
                return _make_proc(LOADED_OLLAMA_PS)
            if args[0] == "vm_stat":
                return _make_proc(CLEAN_VMSTAT)
            if args[0] == "pmset":
                return _make_proc(CLEAN_PMSET)
            return _make_proc("")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(PreflightError) as exc_info:
                preflight_check()
        assert "loaded" in str(exc_info.value).lower()

    def test_preflight_passes_clean_ollama_ps(self):
        """ollama ps header-only → no raise."""
        with _patch_all_clean():
            result = preflight_check()
        assert isinstance(result, str)

    def test_preflight_raises_on_nonzero_swap(self):
        """vm_stat 'Pages swapped out: 100.' → PreflightError with 'swap' in message."""
        def side_effect(args, **kw):
            if args[0] == "ollama":
                return _make_proc(CLEAN_OLLAMA_PS)
            if args[0] == "vm_stat":
                return _make_proc(NONZERO_VMSTAT)
            if args[0] == "pmset":
                return _make_proc(CLEAN_PMSET)
            return _make_proc("")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(PreflightError) as exc_info:
                preflight_check()
        assert "swap" in str(exc_info.value).lower()

    def test_preflight_passes_zero_swap(self):
        """vm_stat 'Pages swapped out: 0.' → no raise."""
        with _patch_all_clean():
            result = preflight_check()
        assert isinstance(result, str)

    def test_preflight_raises_on_mirofish_containers(self):
        """docker ps returns container name → PreflightError."""
        def side_effect(args, **kw):
            if args[0] == "ollama":
                return _make_proc(CLEAN_OLLAMA_PS)
            if args[0] == "vm_stat":
                return _make_proc(CLEAN_VMSTAT)
            if args[0] == "docker":
                return _make_proc("mirofish-neo4j")
            if args[0] == "pgrep":
                return _make_proc("", returncode=1)
            if args[0] == "pmset":
                return _make_proc(CLEAN_PMSET)
            return _make_proc("")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(PreflightError) as exc_info:
                preflight_check()
        assert "mirofish" in str(exc_info.value).lower()

    def test_preflight_skips_docker_when_not_installed(self):
        """docker raises FileNotFoundError → no raise (docker optional)."""
        def side_effect(args, **kw):
            if args[0] == "ollama":
                return _make_proc(CLEAN_OLLAMA_PS)
            if args[0] == "vm_stat":
                return _make_proc(CLEAN_VMSTAT)
            if args[0] == "docker":
                raise FileNotFoundError("docker not found")
            if args[0] == "pgrep":
                raise FileNotFoundError("pgrep not found")
            if args[0] == "pmset":
                return _make_proc(CLEAN_PMSET)
            return _make_proc("")

        with patch("subprocess.run", side_effect=side_effect):
            result = preflight_check()
        assert isinstance(result, str)

    def test_preflight_raises_on_mirofish_pgrep(self):
        """pgrep returns pid → PreflightError."""
        def side_effect(args, **kw):
            if args[0] == "ollama":
                return _make_proc(CLEAN_OLLAMA_PS)
            if args[0] == "vm_stat":
                return _make_proc(CLEAN_VMSTAT)
            if args[0] == "docker":
                raise FileNotFoundError("docker not found")
            if args[0] == "pgrep":
                return _make_proc("12345", returncode=0)
            if args[0] == "pmset":
                return _make_proc(CLEAN_PMSET)
            return _make_proc("")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(PreflightError) as exc_info:
                preflight_check()
        assert "mirofish" in str(exc_info.value).lower()

    def test_preflight_returns_power_mode_string(self):
        """Full clean environment → returns 'high' or 'low' string."""
        with _patch_all_clean():
            result = preflight_check()
        assert result in ("high", "low", "unknown")

    def test_preflight_raises_are_runtime_error(self):
        """PreflightError is instance of RuntimeError."""
        err = PreflightError("test")
        assert isinstance(err, RuntimeError)


class _FakeUrlResponse:
    """Minimal urlopen response stub with context-manager support."""

    def __init__(self, payload: str):
        self._payload = payload.encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class TestRuntimeTelemetry:
    """Telemetry helpers normalize `/api/ps`, `ollama ps`, and swap state."""

    def test_collect_runtime_telemetry_marks_degraded_on_swap(self):
        """Non-zero swap marks the scheduler snapshot as degraded."""
        api_payload = (
            '{"models": [{"name": "qwen3:8b", "size_vram": 2147483648, '
            '"details": {"context_length": 8192}}]}'
        )

        def side_effect(args, **kwargs):
            if args[0] == "ollama":
                return _make_proc(LOADED_OLLAMA_PS)
            if args[0] == "sysctl":
                return _make_proc(
                    "vm.swapusage: total = 2048.00M  used = 512.00M  free = 1536.00M"
                )
            return _make_proc("")

        with patch("urllib.request.urlopen", return_value=_FakeUrlResponse(api_payload)), \
             patch("subprocess.run", side_effect=side_effect):
            telemetry = collect_runtime_telemetry()

        assert telemetry.degraded_mode is True
        assert telemetry.swap_used_mb == 512.0
        assert telemetry.loaded_models == ("qwen3:8b",)
        assert "api/ps" in telemetry.telemetry_source
        assert "ollama ps" in telemetry.telemetry_source

    def test_collect_runtime_telemetry_handles_missing_probes(self):
        """Missing `/api/ps`, `ollama ps`, and swap probes return a safe empty snapshot."""
        with patch("urllib.request.urlopen", side_effect=OSError("down")), \
             patch("subprocess.run", side_effect=FileNotFoundError("missing")):
            telemetry = collect_runtime_telemetry()

        assert telemetry.degraded_mode is False
        assert telemetry.loaded_models == ()
        assert telemetry.telemetry_source == "unavailable"


# ---------------------------------------------------------------------------
# strip_thinking
# ---------------------------------------------------------------------------

class TestStripThinking:
    """strip_thinking(text) -> (clean, bool) — handles <think> blocks."""

    def test_strip_thinking_single_block(self):
        """Text with think tags → (clean_text, True)."""
        text = "<think>some reasoning</think>\n\nFinal answer."
        clean, flag = strip_thinking(text)
        assert flag is True
        assert "think" not in clean
        assert "Final answer." in clean

    def test_strip_thinking_multiline_block(self):
        """Block with newlines stripped correctly (re.DOTALL)."""
        text = "<think>\nline one\nline two\nline three\n</think>\n\nAnswer here."
        clean, flag = strip_thinking(text)
        assert flag is True
        assert "line one" not in clean
        assert "Answer here." in clean

    def test_strip_thinking_no_block(self):
        """Plain text → (text.strip(), False)."""
        text = "Plain response."
        clean, flag = strip_thinking(text)
        assert flag is False
        assert clean == "Plain response."

    def test_strip_thinking_multiple_blocks(self):
        """Two think blocks both stripped."""
        text = "<think>first</think> middle <think>second</think> end"
        clean, flag = strip_thinking(text)
        assert flag is True
        assert "first" not in clean
        assert "second" not in clean
        assert "end" in clean

    def test_strip_thinking_empty_block(self):
        """Empty think block followed by answer → (answer, True)."""
        text = "<think></think>Direct answer."
        clean, flag = strip_thinking(text)
        assert flag is True
        assert "Direct answer." in clean

    def test_strip_thinking_leading_newline(self):
        """Leading newline after block stripped → clean text (strip() applied)."""
        text = "<think>reasoning</think>\n\nResult text."
        clean, flag = strip_thinking(text)
        assert flag is True
        assert clean.startswith("Result text.")
