"""bench_refresh.py — D5 Benchmark Harness Refresh utilities."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from typing import Optional

# ---------------------------------------------------------------------------
# Contamination labels (BENCH-02)
# ---------------------------------------------------------------------------

SUITE_CONTAMINATION_LABELS: dict[str, str] = {
    "humaneval": "possibly contaminated",
    "bigcodebench": "uncontaminated",
    "mteb": "uncontaminated",
    "code": "uncontaminated",
    "science": "uncontaminated",
    "swarm": "uncontaminated",
}

MMLU_PRO_SUITE = "mmlu_pro"

PRIMARY_SUITES: frozenset[str] = frozenset(
    {"code", "science", "swarm", "bigcodebench", "mteb", "humaneval"}
)


def get_contamination_label(suite: str) -> str:
    """Return the contamination label for a benchmark suite."""
    return SUITE_CONTAMINATION_LABELS.get(suite, "unknown")


def is_primary_suite(suite: str) -> bool:
    """Return True if suite is in the primary selection set (mmlu_pro excluded)."""
    return suite in PRIMARY_SUITES


# ---------------------------------------------------------------------------
# BigCodeBench sandbox (BENCH-01)
# ---------------------------------------------------------------------------

BIGCODEBENCH_SANDBOX_PROFILE = """\
(version 1)
(deny default)
(allow process*)
(allow file-read*)
(allow file-write (subpath (param "TMPDIR")))
(deny network*)
"""


def run_sandboxed_code(
    code: str,
    *,
    timeout: float = 10.0,
    python_bin: str = "python3",
) -> tuple[int, str, str]:
    """Execute Python code inside macOS sandbox-exec with deny-network profile.

    Strips 'ollama' from PATH. Returns (returncode, stdout, stderr).
    """
    env = dict(os.environ)
    path_parts = [p for p in env.get("PATH", "").split(":") if "ollama" not in p.lower()]
    env["PATH"] = ":".join(path_parts)

    script_fd, script_path = tempfile.mkstemp(suffix=".py")
    profile_fd, profile_path = tempfile.mkstemp(suffix=".sb")
    try:
        with os.fdopen(script_fd, "w") as f:
            f.write(code)
        with os.fdopen(profile_fd, "w") as pf:
            pf.write(BIGCODEBENCH_SANDBOX_PROFILE)

        proc = subprocess.run(
            ["sandbox-exec", "-f", profile_path, python_bin, script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass
        try:
            os.unlink(profile_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Thermal state (BENCH-05)
# ---------------------------------------------------------------------------

THERMAL_CHECK_THRESHOLD_C: float = 70.0
COOLDOWN_SECONDS: float = 30.0


def read_soc_temperature_c() -> Optional[float]:
    """Read Apple SoC temperature via powermetrics. Returns None if unavailable."""
    try:
        result = subprocess.run(
            ["sudo", "powermetrics", "--samplers", "smc", "-n", "1", "-i", "100"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        for line in result.stdout.splitlines():
            lower = line.lower()
            if "cpu die temperature" in lower or "soc die temperature" in lower:
                parts = line.split(":")
                if len(parts) >= 2:
                    temp_str = parts[1].strip().split()[0]
                    return float(temp_str)
    except Exception:
        pass
    return None


def check_thermal_ok(
    threshold_c: float = THERMAL_CHECK_THRESHOLD_C,
) -> tuple[bool, Optional[float]]:
    """Return (ok, temp_c). ok=True if temp < threshold or unavailable."""
    temp = read_soc_temperature_c()
    if temp is None:
        return True, None
    return temp < threshold_c, temp


def log_thermal_state() -> dict:
    """Return dict with thermal state for benchmark run metadata."""
    ok, temp = check_thermal_ok()
    return {
        "soc_temp_c": temp,
        "thermal_ok": ok,
        "threshold_c": THERMAL_CHECK_THRESHOLD_C,
    }


def enforce_cooldown(seconds: float = COOLDOWN_SECONDS) -> None:
    """Sleep for cooldown period between benchmark runs."""
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# HF dataset pinning (BENCH-04)
# ---------------------------------------------------------------------------


def enforce_hf_offline() -> None:
    """Set HF_HUB_OFFLINE=1 to prevent accidental HF downloads."""
    os.environ["HF_HUB_OFFLINE"] = "1"


class HFDatasetPin:
    """Pinned HuggingFace dataset with commit SHA."""

    def __init__(self, dataset_name: str, commit_sha: str) -> None:
        self.dataset_name = dataset_name
        self.commit_sha = commit_sha

    def download(self, cache_dir: Optional[str] = None) -> str:
        """Download pinned dataset. Enforces HF_HUB_OFFLINE=1."""
        enforce_hf_offline()
        try:
            from huggingface_hub import snapshot_download  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError("huggingface_hub not installed") from exc
        return snapshot_download(
            self.dataset_name,
            revision=self.commit_sha,
            cache_dir=cache_dir,
        )


# ---------------------------------------------------------------------------
# MTEB subprocess runner (BENCH-01)
# ---------------------------------------------------------------------------


class MtebSubprocessRunner:
    """Run MTEB benchmark in a dedicated subprocess per model."""

    def run(
        self,
        model: str,
        tasks: list[str],
        *,
        timeout: float = 300.0,
    ) -> dict:
        """Run MTEB tasks for model in a fresh subprocess. Returns status dict."""
        import json
        import sys

        tasks_json = json.dumps(tasks)
        model_escaped = model.replace('"', '\\"')
        script = (
            "import json, sys\n"
            "try:\n"
            "    import mteb\n"
            f"    m = mteb.get_model('{model_escaped}')\n"
            f"    ev = mteb.MTEB(tasks={tasks_json})\n"
            "    ev.run(m, output_folder='/tmp/mteb_results')\n"
            f"    print(json.dumps({{'status': 'ok', 'model': '{model_escaped}', 'tasks': {tasks_json}}}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'status': 'error', 'error': str(e)}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        try:
            return json.loads(proc.stdout.strip())
        except Exception:
            return {
                "status": "error",
                "returncode": proc.returncode,
                "stderr": proc.stderr,
            }
