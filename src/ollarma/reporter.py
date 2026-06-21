"""reporter.py -- Aggregation, composite scoring, Pareto frontier, Markdown generation.

Pure-function module. No side effects in core functions (file I/O in bench.py).
Follows existing codebase pattern: module-level constants with docstrings, pure functions.

Scoring contract:
  - Reads dicts from orjson-deserialized sealed JSON (not BenchmarkResult objects)
  - Returns Markdown strings for results.md and model_selection.md
  - All None handling explicit: None quality_score and None prefill_tps never crash
"""
from __future__ import annotations

import pathlib
import re
from collections import defaultdict
from statistics import fmean, stdev
from typing import Optional

import orjson
from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Design decision: composite score weights (REPT-03)
# These are TUNABLE PARAMETERS, not empirical facts.
# Science/code tiers: quality matters more than speed.
# Routing/swarm tier: speed matters more than quality (latency-sensitive orchestration).
# ---------------------------------------------------------------------------
QUALITY_WEIGHT_DEFAULT = 0.7   # science + code suites
SPEED_WEIGHT_DEFAULT = 0.3     # science + code suites
QUALITY_WEIGHT_ROUTING = 0.4   # swarm suite
SPEED_WEIGHT_ROUTING = 0.6     # swarm suite

# Minimum mean quality a model must clear to be eligible as a suite WINNER.
# TUNABLE PARAMETER, not an empirical fact. Without it the speed-weighted
# composite lets a fast-but-useless model win: e.g. qwen2.5:1.5b at quality 0.00
# beat granite4.1:8b (quality 0.95) on the swarm suite purely on decode TPS.
# A model below the floor is never chosen over one that clears it; if NO model
# clears the floor, selection falls back to highest *quality* (not composite),
# so speed can never dominate when every candidate is low-quality.
MIN_QUALITY_FLOOR = 0.5


# ---------------------------------------------------------------------------
# Model size lookup for names without explicit size tags
# ---------------------------------------------------------------------------
_MODEL_SIZE_LOOKUP: dict[str, float] = {
    "phi4-mini": 3.8,
    "smollm2": 1.7,
}

_SIZE_RE = re.compile(r"(\d+\.?\d*)[bB]")


# ---------------------------------------------------------------------------
# Pydantic model for aggregated stats
# ---------------------------------------------------------------------------

class ModelSuiteStats(BaseModel):
    """Aggregated statistics for one model in one suite."""

    model: str
    suite: str
    quality_mean: Optional[float]    # None if all trials had None quality_score
    quality_std: float
    decode_tps_mean: float
    decode_tps_std: float
    prefill_tps_mean: Optional[float]  # None if all trials had None prefill_tps
    prefill_tps_std: float
    ttft_ms: Optional[float]           # Derived: (1/prefill_tps_mean)*1000 if available
    trial_count: int
    composite_score: float             # Computed after TPS normalization

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_stdev(values: list[float]) -> float:
    """Return sample stdev, or 0.0 if fewer than 2 data points.

    Pitfall 1: statistics.stdev raises StatisticsError for N<2.
    """
    if len(values) < 2:
        return 0.0
    return stdev(values)


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a Markdown pipe-table from headers and row data."""
    lines = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _fmt_score(val: Optional[float]) -> str:
    """Format a score to 2 decimal places, or 'N/A' if None."""
    if val is None:
        return "N/A"
    return f"{val:.2f}"


def _fmt_tps(val: Optional[float]) -> str:
    """Format TPS/TTFT to 1 decimal place, or 'N/A' if None."""
    if val is None:
        return "N/A"
    return f"{val:.1f}"


def _fmt_score_pm(mean: Optional[float], std: float) -> str:
    """Format 'mean +/- std' for scores."""
    if mean is None:
        return "N/A"
    return f"{mean:.2f} +/- {std:.2f}"


def _fmt_tps_pm(mean: float, std: float) -> str:
    """Format 'mean +/- std' for TPS values."""
    return f"{mean:.1f} +/- {_fmt_tps(std)}"


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_sealed_results(path: pathlib.Path) -> list[dict]:
    """Read a sealed results JSON file and return list of dicts.

    Raises FileNotFoundError if path does not exist.
    T-05-03: uses orjson for consistency with store.py.
    """
    if not path.exists():
        raise FileNotFoundError(f"No sealed results at {path}")
    return orjson.loads(path.read_bytes())


def find_latest_sealed(results_dir: pathlib.Path = pathlib.Path("results")) -> pathlib.Path:
    """Find the most recent run-*.json file by lexicographic sort.

    Excludes derivative files (*.artifact.json, *.evidence.json) so that
    generated artifacts do not shadow the original sealed result files.
    ISO timestamps sort correctly lexicographically.
    Raises FileNotFoundError if no sealed files found.
    """
    sealed_files = sorted(
        p for p in results_dir.glob("run-*.json")
        if not (p.name.endswith(".artifact.json") or p.name.endswith(".evidence.json"))
    )
    if not sealed_files:
        raise FileNotFoundError(f"No sealed results found in {results_dir}")
    return sealed_files[-1]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_trials(rows: list[dict]) -> list[ModelSuiteStats]:
    """Group rows by (model, suite) and compute mean/std for key metrics.

    Returns list of ModelSuiteStats sorted by (suite, model).
    composite_score is initially 0.0 -- filled after normalization in render functions.

    T-05-03: uses dict.get() with defaults for optional fields; skips rows
    missing required fields (model, suite, decode_tps).
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        # T-05-03: skip rows missing required fields
        model = row.get("model")
        suite = row.get("suite")
        decode = row.get("decode_tps")
        if model is None or suite is None or decode is None:
            continue
        groups[(model, suite)].append(row)

    stats: list[ModelSuiteStats] = []
    for (model, suite), trials in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        # Pitfall 2: filter None from quality_score before fmean
        quality_scores = [t["quality_score"] for t in trials if t.get("quality_score") is not None]
        decode_vals = [t["decode_tps"] for t in trials]
        # Pitfall 3: filter None from prefill_tps before fmean
        prefill_vals = [t["prefill_tps"] for t in trials if t.get("prefill_tps") is not None]

        quality_mean = fmean(quality_scores) if quality_scores else None
        prefill_tps_mean = fmean(prefill_vals) if prefill_vals else None
        ttft_ms = (1.0 / prefill_tps_mean) * 1000.0 if prefill_tps_mean is not None else None

        stats.append(ModelSuiteStats(
            model=model,
            suite=suite,
            quality_mean=quality_mean,
            quality_std=_safe_stdev(quality_scores) if quality_scores else 0.0,
            decode_tps_mean=fmean(decode_vals),
            decode_tps_std=_safe_stdev(decode_vals),
            prefill_tps_mean=prefill_tps_mean,
            prefill_tps_std=_safe_stdev(prefill_vals) if prefill_vals else 0.0,
            ttft_ms=ttft_ms,
            trial_count=len(trials),
            composite_score=0.0,  # filled after normalization
        ))

    return stats


# ---------------------------------------------------------------------------
# Normalization and scoring
# ---------------------------------------------------------------------------

def normalize_min_max(values: list[float]) -> list[float]:
    """Min-max normalize to [0, 1]. If all values equal, return 1.0 for all.

    Pitfall 4: guard against division by zero when max == min.
    """
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def composite_score(
    quality: float,
    tps_normalized: float,
    suite: str,
) -> float:
    """Compute weighted composite score per REPT-03 weights.

    Science/code: quality * 0.7 + tps_normalized * 0.3 (design decision)
    Routing/swarm: quality * 0.4 + tps_normalized * 0.6 (design decision)
    """
    if suite == "swarm":
        return quality * QUALITY_WEIGHT_ROUTING + tps_normalized * SPEED_WEIGHT_ROUTING
    return quality * QUALITY_WEIGHT_DEFAULT + tps_normalized * SPEED_WEIGHT_DEFAULT


# ---------------------------------------------------------------------------
# Pareto frontier
# ---------------------------------------------------------------------------

def pareto_frontier_2d(points: list[tuple[float, float]]) -> list[int]:
    """Return indices of Pareto-optimal points (both objectives maximized).

    A point A dominates B iff A >= B in all dimensions and A > B in at least one.
    O(n^2) -- trivial for N <= 10 models.
    """
    frontier = []
    for i, (q_i, s_i) in enumerate(points):
        dominated = False
        for j, (q_j, s_j) in enumerate(points):
            if i == j:
                continue
            if q_j >= q_i and s_j >= s_i and (q_j > q_i or s_j > s_i):
                dominated = True
                break
        if not dominated:
            frontier.append(i)
    return frontier


# ---------------------------------------------------------------------------
# Model size heuristic
# ---------------------------------------------------------------------------

def extract_model_size(model_name: str) -> Optional[float]:
    """Parse model size (in billions) from Ollama model tag.

    Strategy:
    1. Check lookup table for names without size (phi4-mini -> 3.8, smollm2 -> 1.7).
    2. Regex for size pattern: digits + 'b' (e.g., qwen3:8b -> 8.0).
    3. Return None if unparseable.

    Pitfall 5: model sizes are heuristic, not stored in BenchmarkResult.
    """
    # Lookup table for models without size in name
    if model_name in _MODEL_SIZE_LOOKUP:
        return _MODEL_SIZE_LOOKUP[model_name]

    # Regex: match digits followed by b/B anywhere in the name
    match = _SIZE_RE.search(model_name)
    if match:
        return float(match.group(1))

    return None


# ---------------------------------------------------------------------------
# Markdown rendering: results.md (REPT-01)
# ---------------------------------------------------------------------------

def _compute_composites(stats: list[ModelSuiteStats]) -> list[ModelSuiteStats]:
    """Compute composite scores for each ModelSuiteStats entry.

    Normalizes decode_tps within each suite group, then applies REPT-03 weights.
    Returns new list with composite_score populated.
    """
    # Group by suite
    suite_groups: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(stats):
        suite_groups[s.suite].append(i)

    result = list(stats)  # shallow copy
    for suite, indices in suite_groups.items():
        decode_vals = [stats[i].decode_tps_mean for i in indices]
        normalized_tps = normalize_min_max(decode_vals)

        for idx, norm_tps in zip(indices, normalized_tps):
            s = stats[idx]
            if s.quality_mean is not None:
                comp = composite_score(s.quality_mean, norm_tps, suite)
            else:
                comp = 0.0  # Can't compute composite without quality
            result[idx] = s.model_copy(update={"composite_score": comp})

    return result


def select_suite_winner(
    entries: list[ModelSuiteStats],
    *,
    quality_floor: float = MIN_QUALITY_FLOOR,
) -> Optional[ModelSuiteStats]:
    """Pick the recommended model for one suite, honoring a minimum quality floor.

    Among models that clear *quality_floor*, the best composite wins. If none
    clear it, fall back to the highest-quality model (NOT the fastest composite),
    so a fast-but-useless model can never be recommended over a more accurate
    one. Returns ``None`` if no entry has a quality score.
    """
    scorable = [e for e in entries if e.quality_mean is not None]
    if not scorable:
        return None
    qualified = [e for e in scorable if e.quality_mean >= quality_floor]
    if qualified:
        return max(qualified, key=lambda x: x.composite_score)
    # No model clears the floor — rank by quality so speed cannot dominate.
    return max(scorable, key=lambda x: x.quality_mean)


def render_results_md(stats: list[ModelSuiteStats]) -> str:
    """Generate results.md content: raw data tables with methodology section.

    REPT-01: per-model per-suite pipe-tables with all metrics.
    REPT-03: composite weight labels as design decisions.
    """
    if not stats:
        return "No results to report."

    stats = _compute_composites(stats)

    lines: list[str] = []
    lines.append("# Benchmark Results")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append("Composite score weights (design decision -- tunable parameters):")
    lines.append("")
    lines.append(f"- **Science/Code:** quality x {QUALITY_WEIGHT_DEFAULT} + speed x {SPEED_WEIGHT_DEFAULT}")
    lines.append(f"- **Routing/Swarm:** quality x {QUALITY_WEIGHT_ROUTING} + speed x {SPEED_WEIGHT_ROUTING}")
    lines.append("")
    lines.append("Speed is min-max normalized decode TPS within each suite.")
    lines.append("")

    # Group by suite for per-suite tables
    suite_groups: dict[str, list[ModelSuiteStats]] = defaultdict(list)
    for s in stats:
        suite_groups[s.suite].append(s)

    headers = ["Model", "Quality (mean +/- std)", "Decode TPS (mean +/- std)",
               "Prefill TPS (mean +/- std)", "TTFT (ms)", "Composite", "Trials"]

    for suite in sorted(suite_groups):
        entries = suite_groups[suite]
        lines.append(f"## {suite.capitalize()} Suite")
        lines.append("")

        table_rows: list[list[str]] = []
        for s in sorted(entries, key=lambda x: x.composite_score, reverse=True):
            table_rows.append([
                s.model,
                _fmt_score_pm(s.quality_mean, s.quality_std),
                _fmt_tps_pm(s.decode_tps_mean, s.decode_tps_std),
                _fmt_tps_pm(s.prefill_tps_mean, s.prefill_tps_std) if s.prefill_tps_mean is not None else "N/A",
                _fmt_tps(s.ttft_ms),
                _fmt_score(s.composite_score) if s.quality_mean is not None else "N/A",
                str(s.trial_count),
            ])

        lines.append(_md_table(headers, table_rows))
        lines.append("")

    # Contamination status section (BENCH-02)
    from ollarma.bench_refresh import get_contamination_label
    seen_suites = sorted({s.suite for s in stats})
    lines.append("## Contamination Status")
    lines.append("")
    for suite in seen_suites:
        lines.append(f"- **{suite}**: {get_contamination_label(suite)}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown rendering: model_selection.md (REPT-02)
# ---------------------------------------------------------------------------

def render_selection_md(stats: list[ModelSuiteStats]) -> str:
    """Generate model_selection.md content: Pareto frontier, winners, recommendations.

    REPT-02: Pareto table, per-workload winner, small-model recommendation.
    REPT-03: composite weight labels as design decisions.
    """
    if not stats:
        return "No results to report."

    stats = _compute_composites(stats)

    lines: list[str] = []
    lines.append("# Model Selection Guide")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append("Composite score weights (design decision -- tunable parameters):")
    lines.append("")
    lines.append(f"- **Science/Code:** quality x {QUALITY_WEIGHT_DEFAULT} + speed x {SPEED_WEIGHT_DEFAULT}")
    lines.append(f"- **Routing/Swarm:** quality x {QUALITY_WEIGHT_ROUTING} + speed x {SPEED_WEIGHT_ROUTING}")
    lines.append("")

    # --- Per-workload winners ---
    lines.append("## Per-Workload Winners")
    lines.append("")

    suite_groups: dict[str, list[ModelSuiteStats]] = defaultdict(list)
    for s in stats:
        suite_groups[s.suite].append(s)

    winner_headers = ["Suite", "Best Model", "Composite Score", "Quality Mean", "Decode TPS"]
    winner_rows: list[list[str]] = []

    for suite in sorted(suite_groups):
        entries = suite_groups[suite]
        # Quality-floored winner: a fast-but-useless model never wins (REPT-03).
        winner = select_suite_winner(entries)
        if winner is not None:
            below_floor = (
                winner.quality_mean is not None and winner.quality_mean < MIN_QUALITY_FLOOR
            )
            winner_rows.append([
                suite,
                winner.model + (" (below quality floor)" if below_floor else ""),
                _fmt_score(winner.composite_score),
                _fmt_score(winner.quality_mean),
                _fmt_tps(winner.decode_tps_mean),
            ])
        else:
            winner_rows.append([suite, "N/A", "N/A", "N/A", "N/A"])

    lines.append(_md_table(winner_headers, winner_rows))
    lines.append("")

    # --- Pareto Frontier ---
    lines.append("## Pareto Frontier")
    lines.append("")
    lines.append("Non-dominated models on the quality vs. decode TPS tradeoff:")
    lines.append("")

    # Build points from all stats with valid quality
    scorable_all = [s for s in stats if s.quality_mean is not None]
    if scorable_all:
        points = [(s.quality_mean, s.decode_tps_mean) for s in scorable_all]
        frontier_indices = pareto_frontier_2d(points)

        pareto_headers = ["Model", "Suite", "Quality Mean", "Decode TPS", "Composite"]
        pareto_rows: list[list[str]] = []
        for idx in frontier_indices:
            s = scorable_all[idx]
            pareto_rows.append([
                s.model,
                s.suite,
                _fmt_score(s.quality_mean),
                _fmt_tps(s.decode_tps_mean),
                _fmt_score(s.composite_score),
            ])
        lines.append(_md_table(pareto_headers, pareto_rows))
    else:
        lines.append("No models with quality scores available for Pareto analysis.")
    lines.append("")

    # --- Small-Model Orchestrator Recommendation ---
    lines.append("## Small-Model Orchestrator Recommendation (<4B)")
    lines.append("")

    small_models = [s for s in stats if extract_model_size(s.model) is not None
                    and extract_model_size(s.model) < 4.0  # type: ignore[operator]
                    and s.quality_mean is not None]

    if small_models:
        # Prefer swarm suite, fall back to all small models — quality-floored so a
        # zero-quality small model is never recommended over a usable one.
        swarm_small = [s for s in small_models if s.suite == "swarm"]
        best_small = select_suite_winner(swarm_small) or select_suite_winner(small_models)

        size = extract_model_size(best_small.model)
        lines.append(
            f"**Recommended:** {best_small.model} "
            f"({size}B, composite={_fmt_score(best_small.composite_score)}, "
            f"suite={best_small.suite})"
        )
        lines.append("")
        lines.append(
            f"Quality: {_fmt_score(best_small.quality_mean)} | "
            f"Decode TPS: {_fmt_tps(best_small.decode_tps_mean)} | "
            f"Trials: {best_small.trial_count}"
        )
    else:
        lines.append("No models under 4B in evaluation set.")
    lines.append("")

    return "\n".join(lines)
