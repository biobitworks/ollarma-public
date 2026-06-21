"""scorers.py — Pure scoring functions for each benchmark suite.

Each scorer takes a BenchmarkResult and returns a float in [0.0, 1.0].
No side effects. No Ollama calls. No network. All offline.

Scoring contract:
  score(result: BenchmarkResult) -> float
  - Returns 0.0 on any parse failure or unrecognized task
  - Returns 1.0 for perfect score (all assertions pass, exact routing match with full confidence)
  - BenchmarkResult is frozen — scorers do not mutate it; bench.py uses model_copy() in Phase 4

TTFT note (SCORE-03): TTFT is derivable as (1 / prefill_tps * 1000) ms from existing fields.
A dedicated ttft_ms schema field is flagged for Phase 4 discussion. This scorer returns
quality float only.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import textwrap

from ollarma.guards import strip_thinking
from ollarma.executor import BenchmarkResult


# ---------------------------------------------------------------------------
# Routing oracle — synthetic ground truth mapping task_id → correct model.
# Kept in sync with tasks/swarm/*.yml — every swarm task MUST have an entry here.
# swarm_route_01: "Parse the attached Python traceback..." → code/debug task → qwen3-coder:7b
# ---------------------------------------------------------------------------

ROUTING_ORACLE: dict[str, str] = {
    "swarm_route_01": "qwen3-coder:7b",
}


# ---------------------------------------------------------------------------
# Per-task code test harnesses — hardcoded for Phase 3; Phase 4 migrates to YAML.
# ---------------------------------------------------------------------------

_CODE_TEST_HARNESS: dict[str, str] = {
    "code_generate_01": textwrap.dedent("""\
        assert flatten([1, [2, [3, 4]], 5]) == [1, 2, 3, 4, 5], "nested flatten failed"
        assert flatten([]) == [], "empty list failed"
        assert flatten([1, 2, 3]) == [1, 2, 3], "flat list failed"
        assert flatten([[[[1]]]]) == [1], "deeply nested failed"
    """),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)


def _extract_code(raw: str) -> str:
    """Extract Python code from an LLM response.

    Calls strip_thinking first to remove <think>...</think> blocks, then strips
    markdown fences (```python or ```) if present.  Returns plain stripped text
    if no fence is found.
    """
    text, _ = strip_thinking(raw)
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


# ---------------------------------------------------------------------------
# score_science
# ---------------------------------------------------------------------------

def score_science(result: BenchmarkResult) -> float:
    """Parse JSON verdict from raw_response; return confidence as score.

    Expected response format:
      {"verdict": "SUPPORT|REFUTE|NEUTRAL", "confidence": 0.0-1.0, "reasoning": "..."}

    Returns:
      float in [0.0, 1.0] — the confidence value for a valid verdict.
      0.0 on any parse failure, invalid verdict, or empty response.

    Threat T-03-03: json.loads wrapped in try/except — malformed responses cannot crash scorer.
    Threat T-03-05: confidence clamped to [0.0, 1.0] before return.
    """
    try:
        text, _ = strip_thinking(result.raw_response)
        data = json.loads(text)
        verdict = data.get("verdict", "")
        if verdict not in {"SUPPORT", "REFUTE", "NEUTRAL"}:
            return 0.0
        confidence = float(data.get("confidence", 0.0))
        return max(0.0, min(1.0, confidence))
    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
        return 0.0


# ---------------------------------------------------------------------------
# score_code
# ---------------------------------------------------------------------------

def score_code(result: BenchmarkResult) -> float:
    """Execute generated Python code against test assertions.

    Steps:
    1. Extract code from raw_response (strips thinking + fences).
    2. Validate syntax via ast.parse — return 0.0 on SyntaxError.
    3. Look up per-task test harness in _CODE_TEST_HARNESS — return 0.0 if unknown task.
    4. Run full_script = code + test_harness via subprocess — return 1.0 on exit 0, else 0.0.
    5. Catch TimeoutExpired — return 0.0.

    Threat T-03-01: subprocess isolation — generated code runs in a child process, not exec().
    Threat T-03-02: timeout=10 prevents infinite-loop denial of service.
    Uses sys.executable to ensure same virtualenv Python (Pitfall 6).
    """
    code = _extract_code(result.raw_response)
    if not code:
        return 0.0

    try:
        ast.parse(code)
    except SyntaxError:
        return 0.0

    test_body = _CODE_TEST_HARNESS.get(result.task_id)
    if test_body is None:
        return 0.0

    full_script = code + "\n\n" + test_body

    try:
        proc = subprocess.run(
            [sys.executable, "-c", full_script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return 1.0 if proc.returncode == 0 else 0.0
    except subprocess.TimeoutExpired:
        return 0.0


# ---------------------------------------------------------------------------
# score_swarm
# ---------------------------------------------------------------------------

def score_swarm(result: BenchmarkResult) -> float:
    """Validate routing decision against ROUTING_ORACLE.

    Expected response format:
      {"selected_model": "<model_name>", "confidence": 0.0-1.0, "reason": "..."}

    Returns:
      float in [0.0, 1.0] — confidence if selected_model matches oracle.
      0.0 if task_id not in oracle, model mismatch, or any parse failure.

    Threat T-03-03: json.loads wrapped in try/except — malformed responses cannot crash scorer.
    Threat T-03-05: confidence clamped to [0.0, 1.0] before return.
    """
    expected = ROUTING_ORACLE.get(result.task_id)
    if expected is None:
        return 0.0

    try:
        text, _ = strip_thinking(result.raw_response)
        data = json.loads(text)
        selected = data.get("selected_model", "")
        if selected != expected:
            return 0.0
        confidence = float(data.get("confidence", 0.0))
        return max(0.0, min(1.0, confidence))
    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
        return 0.0


# ---------------------------------------------------------------------------
# score_bigcodebench
# ---------------------------------------------------------------------------

def score_bigcodebench(result: BenchmarkResult) -> float:
    """Run BigCodeBench task code in macOS sandbox-exec. Returns 1.0 on pass, 0.0 on fail.

    Threat T-03-01: sandbox-exec isolation — generated code cannot access network.
    Threat T-03-02: timeout=10 prevents infinite-loop denial of service.
    """
    from ollarma.bench_refresh import run_sandboxed_code
    code = _extract_code(result.raw_response)
    if not code:
        return 0.0
    try:
        ast.parse(code)
    except SyntaxError:
        return 0.0
    rc, _, _ = run_sandboxed_code(code, timeout=10.0)
    return 1.0 if rc == 0 else 0.0


# ---------------------------------------------------------------------------
# score_mteb
# ---------------------------------------------------------------------------

def score_mteb(result: BenchmarkResult) -> float:
    """Run MTEB embedding benchmark in a subprocess. Returns 1.0 if subprocess succeeds."""
    from ollarma.bench_refresh import MtebSubprocessRunner
    runner = MtebSubprocessRunner()
    res = runner.run(result.model, ["STS17"], timeout=120.0)
    return 1.0 if res.get("status") == "ok" else 0.0
