"""Tests for bench_refresh.py (Phase 36)."""
import os
import pytest
from unittest.mock import patch, MagicMock
from ollarma.bench_refresh import (
    SUITE_CONTAMINATION_LABELS,
    PRIMARY_SUITES,
    MMLU_PRO_SUITE,
    get_contamination_label,
    is_primary_suite,
    run_sandboxed_code,
    check_thermal_ok,
    log_thermal_state,
    enforce_hf_offline,
    HFDatasetPin,
    MtebSubprocessRunner,
    THERMAL_CHECK_THRESHOLD_C,
)


def test_humaneval_is_possibly_contaminated():
    assert get_contamination_label("humaneval") == "possibly contaminated"


def test_bigcodebench_is_uncontaminated():
    assert get_contamination_label("bigcodebench") == "uncontaminated"


def test_mteb_is_uncontaminated():
    assert get_contamination_label("mteb") == "uncontaminated"


def test_mmlu_pro_not_primary():
    assert not is_primary_suite(MMLU_PRO_SUITE)
    assert MMLU_PRO_SUITE not in PRIMARY_SUITES


def test_bigcodebench_is_primary():
    assert is_primary_suite("bigcodebench")


def test_run_sandboxed_code_calls_sandbox_exec():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "hello\n"
    mock_proc.stderr = ""
    with patch("ollarma.bench_refresh.subprocess.run", return_value=mock_proc) as mock_run:
        rc, out, err = run_sandboxed_code("print('hello')", timeout=5.0)
    assert rc == 0
    args = mock_run.call_args[0][0]
    assert args[0] == "sandbox-exec"


def test_run_sandboxed_code_strips_ollama_from_path():
    original_path = "/usr/bin:/usr/local/bin/ollama:/usr/local/bin"
    captured_env = {}

    def capture_env(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch.dict(os.environ, {"PATH": original_path}), \
         patch("ollarma.bench_refresh.subprocess.run", side_effect=capture_env):
        run_sandboxed_code("pass")

    assert "ollama" not in captured_env.get("PATH", "")


def test_check_thermal_ok_when_unavailable():
    with patch("ollarma.bench_refresh.read_soc_temperature_c", return_value=None):
        ok, temp = check_thermal_ok()
    assert ok is True
    assert temp is None


def test_check_thermal_ok_below_threshold():
    with patch("ollarma.bench_refresh.read_soc_temperature_c", return_value=55.0):
        ok, temp = check_thermal_ok(threshold_c=70.0)
    assert ok is True
    assert temp == 55.0


def test_check_thermal_not_ok_above_threshold():
    with patch("ollarma.bench_refresh.read_soc_temperature_c", return_value=80.0):
        ok, temp = check_thermal_ok(threshold_c=70.0)
    assert ok is False
    assert temp == 80.0


def test_log_thermal_state_has_required_fields():
    with patch("ollarma.bench_refresh.read_soc_temperature_c", return_value=52.0):
        state = log_thermal_state()
    assert "soc_temp_c" in state
    assert "thermal_ok" in state
    assert "threshold_c" in state
    assert state["threshold_c"] == THERMAL_CHECK_THRESHOLD_C


def test_enforce_hf_offline_sets_env():
    saved = os.environ.pop("HF_HUB_OFFLINE", None)
    try:
        enforce_hf_offline()
        assert os.environ.get("HF_HUB_OFFLINE") == "1"
    finally:
        if saved is not None:
            os.environ["HF_HUB_OFFLINE"] = saved
        else:
            os.environ.pop("HF_HUB_OFFLINE", None)


def test_mteb_runner_returns_error_on_subprocess_failure():
    runner = MtebSubprocessRunner()
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = "not-json"
    mock_proc.stderr = "ImportError: No module named mteb"
    with patch("ollarma.bench_refresh.subprocess.run", return_value=mock_proc):
        result = runner.run("nomic-embed-text", ["STS17"], timeout=5.0)
    assert result.get("status") == "error"
