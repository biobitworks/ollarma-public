"""executor.py — Ollama inference executor and BenchmarkResult schema.

Single Ollama API call → structured BenchmarkResult with all mandatory fields.
No warmup, no scoring, no thinking-mode detection — those are Phase 2 / Phase 3.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

import ollama
from pydantic import BaseModel, ConfigDict

from ollarma.guards import strip_thinking


# ---------------------------------------------------------------------------
# Generic per-task output cap (max decode tokens). Bounds verbose / thinking
# models so a single un-capped chain-of-thought cannot blow the benchmark
# wall-time budget — one qwen3.5:9b science trial previously ran >15 min
# unsealed because run_inference passed no num_predict. The swarm engine has its
# own DEFAULT_NUM_PREDICT=200 (swarm/_runtime_contract.py); this is the
# equivalent guard for the generic benchmark executor. Tasks may override via
# TaskConfig.num_predict, and the CLI/service via --num-predict.
# ---------------------------------------------------------------------------
DEFAULT_NUM_PREDICT: int = 1024


# ---------------------------------------------------------------------------
# Threat T-01-01: validate model name before passing to subprocess
# ---------------------------------------------------------------------------
_MODEL_NAME_RE = re.compile(r"^[\w.:/-]+$")


def _validate_model_name(model: str) -> None:
    """Raise ValueError if model name contains characters unsafe for subprocess."""
    if not _MODEL_NAME_RE.fullmatch(model):
        raise ValueError(
            f"Invalid model name {model!r}. "
            "Only word characters, '.', ':', '/', and '-' are allowed."
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class BenchmarkResult(BaseModel):
    """Immutable record of one Ollama inference benchmark run.

    All 14 fields are mandatory. quality_score and prefill_tps are Optional[float]
    but must be explicitly set (None in Phase 1 for quality_score; None when
    prompt_eval_duration == 0 for prefill_tps).
    """

    model: str                    # Ollama model tag (e.g. "qwen3:8b")
    task_id: str                  # from task YAML
    suite: str                    # from task YAML
    num_ctx: int                  # context window size from task YAML (D-13)
    prefill_tps: Optional[float]  # None if prompt_eval_duration == 0 (D-20, PITFALL V1)
    decode_tps: float             # eval_count / (eval_duration / 1e9)
    quality_score: Optional[float]  # None in Phase 1 — intentional (D-07)
    model_digest: str             # sha256:... from ollama show (D-09, D-22)
    ollama_version: str           # e.g. "ollama version 0.19.0" from ollama --version (D-09)
    thinking_mode: bool = False   # hardcoded False in Phase 1 (D-24)
    prompt_hash: str              # SHA-256 hex of rendered prompt (D-21)
    schema_version: str = "1"     # hardcoded "1" for Phase 1 rows (D-23)
    run_ts: datetime              # UTC datetime of inference call
    raw_response: str             # full response text from Ollama

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_model_digest(model: str) -> str:
    """Return the sha256 digest for a model via `ollama list`.

    Ollama 0.19+ removed the digest line from `ollama show --verbose`.
    The digest is now only available in `ollama list` output.

    T-01-01: model is validated before passing to subprocess.
    T-01-04: timeout=10 prevents hanging if ollama is not running.
    """
    _validate_model_name(model)
    result = subprocess.run(
        ["ollama", "list"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    for line in result.stdout.splitlines():
        # Format: "qwen3:8b    500a1f067a9f    5.2 GB    11 hours ago"
        if line.startswith(model) or line.startswith(f"{model} "):
            parts = line.split()
            if len(parts) >= 2:
                return parts[1].strip()
    raise RuntimeError(f"No digest found for model {model!r} in ollama list output")


def list_present_models() -> list[str]:
    """Return model tags currently present in ``ollama list`` (the NAME column).

    Returns an empty list if ``ollama`` is unreachable — callers decide how to
    treat that (an empty roster intersection is handled upstream).
    """
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except Exception:
        return []
    names: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("NAME"):
            continue
        names.append(stripped.split()[0])
    return names


def model_is_present(model: str, present: list[str] | None = None) -> bool:
    """Whether *model* matches a tag in ``ollama list``.

    Mirrors the prefix match in :func:`_get_model_digest` so the benchmark's
    presence check and digest fetch agree: a roster entry like ``phi4-mini``
    matches an installed ``phi4-mini:latest``, while ``granite4.1:3b`` does not
    match an installed ``granite4.1:8b``.
    """
    tags = list_present_models() if present is None else present
    return any(tag == model or tag.startswith(model) for tag in tags)


def _get_ollama_version() -> str:
    """Return the ollama version string via `ollama --version`.

    T-01-02: fixed args, no user input, shell=False.
    T-01-04: timeout=10 prevents hanging.
    """
    try:
        result = subprocess.run(
            ["ollama", "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        # Do not crash — return raw output or unknown
        return result.stdout.strip() if hasattr(result, "stdout") else "unknown"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_inference(
    model: str,
    prompt: str,
    task_id: str,
    suite: str,
    num_ctx: int,
    num_predict: Optional[int] = None,
) -> BenchmarkResult:
    """Run a single Ollama inference and return a fully-populated BenchmarkResult.

    Steps:
    1. Validate model name (T-01-01).
    2. Compute prompt_hash (SHA-256 of rendered prompt).
    3. Fetch model_digest via subprocess ollama show.
    4. Fetch ollama_version via subprocess ollama --version.
    5. Call Ollama generate with fixed seed, temperature=0, and a bounded
       num_predict so verbose/thinking models cannot run unbounded.
    6. Extract timing fields; guard against divide-by-zero on prompt_eval_duration == 0.
    7. Return BenchmarkResult with all 14 fields populated.

    ``num_predict`` caps decode tokens; ``None`` falls back to
    ``DEFAULT_NUM_PREDICT``. A value <= 0 is treated as the default (never
    "unlimited") so the wall-time guard cannot be silently disabled.
    """
    _validate_model_name(model)
    effective_num_predict = (
        num_predict if isinstance(num_predict, int) and num_predict > 0 else DEFAULT_NUM_PREDICT
    )

    # D-21: prompt_hash is SHA-256 of the rendered prompt string
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()

    # D-22: model_digest fetched at runtime before inference
    model_digest = _get_model_digest(model)

    # D-09: ollama version recorded per run
    ollama_version = _get_ollama_version()

    # Inference
    client = ollama.Client()
    resp = client.generate(
        model=model,
        prompt=prompt,
        options={
            "num_ctx": num_ctx,
            "num_predict": effective_num_predict,
            "temperature": 0.0,
            "seed": 42,
        },
        stream=False,
    )

    # D-20 / PITFALL V1: prefill and decode are SEPARATE fields
    # Guard against divide-by-zero: prompt_eval_duration can be 0 on short prompts
    if resp.prompt_eval_duration == 0:
        prefill_tps: Optional[float] = None
    else:
        prefill_tps = resp.prompt_eval_count / (resp.prompt_eval_duration / 1e9)

    decode_tps = resp.eval_count / (resp.eval_duration / 1e9)

    # GUARD-03: detect and strip <think> blocks; set thinking_mode from content
    # clean_response is computed but not stored — Phase 3 scorers call strip_thinking
    # directly on raw_response. raw_response preserves original text (audit trail).
    _clean_response, thinking_mode = strip_thinking(resp.response)

    return BenchmarkResult(
        model=model,
        task_id=task_id,
        suite=suite,
        num_ctx=num_ctx,
        prefill_tps=prefill_tps,
        decode_tps=decode_tps,
        quality_score=None,      # Phase 1 — intentional null (D-07)
        model_digest=model_digest,
        ollama_version=ollama_version,
        thinking_mode=thinking_mode,  # GUARD-03: derived from response content
        prompt_hash=prompt_hash,
        schema_version="1",      # Phase 1 hardcoded (D-23)
        run_ts=datetime.now(timezone.utc),
        raw_response=resp.response,   # original unstripped text (audit trail)
    )
