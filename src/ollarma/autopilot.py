"""autopilot.py -- Asset discovery, classification, and execution for the autopilot runner.

Discovers runnable assets (notebooks, scripts, Snakefiles, pytest suites)
in a project directory using entrypoint heuristics (D-01), recursive scanning
(D-03), notebook code cell filtering (D-04), and include/exclude pattern
matching (D-02). Classifies each asset into a task tier using Antigence
antibodies as primary classifier (D-05), keyword heuristics as fallback (D-06),
and "code" as ultimate fallback (D-07). Maps task tiers to smallest model
above a configurable pass rate threshold (D-09), with degraded confidence
fallback (D-08). Executes assets via papermill (notebooks), subprocess
(scripts/pytest), or snakemake CLI (Snakefiles) with configurable timeouts
(D-12). Orchestrates full discovery-classify-execute pipeline with tiered
model escalation (D-13).

Exports:
    DiscoveredAsset     -- Pydantic model for a single discovered asset
    AssetInventory      -- Pydantic model for the full asset inventory
    TierMapping         -- Pydantic model for tier-to-model mapping result
    AssetResult         -- Pydantic model for execution result of one asset
    AutopilotReport     -- Pydantic model for full autopilot run report
    has_entrypoint      -- Detect entrypoint patterns in Python source
    count_code_cells    -- Count code cells in a Jupyter notebook
    discover_assets     -- Recursively discover all runnable assets
    classify_with_antibodies -- Classify content via Antigence antibodies (D-05)
    classify_with_keywords   -- Classify content via keyword heuristics (D-06)
    classify_asset           -- Three-layer classification chain (D-05/D-06/D-07)
    build_tier_model_map     -- Map tiers to models from benchmark data (AUTO-02)
    execute_asset            -- Execute a single asset with timeout (D-12)
    run_autopilot            -- Full autopilot orchestration loop (D-13)
    ANTIBODY_TIER_MAP   -- Antibody system to task tier mapping
    KEYWORD_TIER_MAP    -- Keyword patterns to task tier mapping
    TIER_SUITE_MAP      -- Task tier to benchmark suite mapping
    MODEL_TIERS         -- Model tier escalation chain
    TIER_SIZE_RANGES    -- Model tier size ranges in billions
    TIER_TIMEOUT_DEFAULTS -- Default timeouts per asset type in seconds
"""
from __future__ import annotations

import fnmatch
import datetime
import importlib
import json
import logging
import pathlib
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

from ollarma.execution_policy import WorkloadClass, resolve_selection
from ollarma.run_ledger import (
    CheckpointState,
    RunReceipt,
    append_run_receipt,
    apply_receipt_to_checkpoint,
    write_checkpoint_state,
)
from ollarma.stage_router import WorkflowStage, determine_next_stage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entrypoint detection patterns (D-01)
# ---------------------------------------------------------------------------

_ENTRYPOINT_PATTERNS: list[re.Pattern[str]] = [
    # __main__ guard (double or single quotes)
    re.compile(r'''if\s+__name__\s*==\s*['"]__main__['"]'''),
    # argparse import
    re.compile(r"^(?:import\s+argparse|from\s+argparse\s+import)", re.MULTILINE),
    # typer import
    re.compile(r"^(?:import\s+typer|from\s+typer\s+import)", re.MULTILINE),
    # click import
    re.compile(r"^(?:import\s+click|from\s+click\s+import)", re.MULTILINE),
]

_SHEBANG_PYTHON = re.compile(r"^#!.*python", re.MULTILINE)

# DEBT-56.1: imports that imply the script loads a local/remote model.
# Conservative: any match flips requires_model=True. Missing all => False.
_MODEL_USE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^(?:import\s+ollama|from\s+ollama\s+import)", re.MULTILINE),
    re.compile(r"^(?:import\s+ollarma|from\s+ollarma\s+import)", re.MULTILINE),
    re.compile(r"^(?:import\s+openai|from\s+openai\s+import)", re.MULTILINE),
    re.compile(r"^(?:import\s+anthropic|from\s+anthropic\s+import)", re.MULTILINE),
    re.compile(r"^(?:import\s+litellm|from\s+litellm\s+import)", re.MULTILINE),
    re.compile(r"^(?:import\s+transformers|from\s+transformers\s+import)", re.MULTILINE),
    re.compile(r"^(?:import\s+mlx_lm|from\s+mlx_lm\s+import)", re.MULTILINE),
    re.compile(r"^(?:import\s+llama_cpp|from\s+llama_cpp\s+import)", re.MULTILINE),
    re.compile(r"#\s*@uses-model\b"),
    re.compile(r"^\s*requires_model\s*:\s*true\s*$", re.MULTILINE | re.IGNORECASE),
]

# Test file patterns (AUTO-01: exclude test files from script discovery)
_TEST_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^test_.*\.py$"),
    re.compile(r"^.*_test\.py$"),
    re.compile(r"^conftest\.py$"),
]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DiscoveredAsset(BaseModel):
    """Single discovered runnable asset."""

    model_config = ConfigDict(frozen=True)

    path: str
    """Absolute path to the asset file."""

    asset_type: str
    """One of: notebook, script, snakefile, pytest_suite."""

    has_entrypoint: bool
    """D-01: True if __main__ guard, argparse, shebang, etc."""

    code_cell_count: int = 0
    """D-04: notebooks only; 0 for non-notebooks."""

    requires_model: bool = True
    """DEBT-56.1: True if this asset invokes a local/remote model.

    Conservative default. When False, the scheduler's SWAP_DEGRADED guard
    is bypassed for this asset (pure-Python scripts with no model import
    don't contend for the model swap budget). Other degraded-mode signals
    (RESOURCE_BUDGET_EXCEEDED) still apply.
    """


class AssetInventory(BaseModel):
    """Full asset inventory for a project."""

    model_config = ConfigDict(frozen=True)

    project_name: str
    project_root: str
    assets: tuple[DiscoveredAsset, ...]
    """Use tuple for frozen model compatibility."""

    counts: dict[str, int]
    """Per-type counts, e.g. {"notebook": 3, "script": 5}."""


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def has_entrypoint(source: str) -> bool:
    """Detect whether Python source has an executable entrypoint (D-01).

    Checks for:
    - Shebang with 'python' on first line
    - ``if __name__ == '__main__'`` or ``if __name__ == "__main__"``
    - ``import argparse`` or ``from argparse import``
    - ``import typer`` or ``from typer import``
    - ``import click`` or ``from click import``

    Args:
        source: Full Python source text.

    Returns:
        True if any entrypoint pattern matches.
    """
    if not source:
        return False

    # Check shebang (first line only)
    first_line = source.split("\n", 1)[0]
    if _SHEBANG_PYTHON.match(first_line):
        return True

    # Check compiled entrypoint patterns
    for pattern in _ENTRYPOINT_PATTERNS:
        if pattern.search(source):
            return True

    return False


def count_code_cells(notebook_path: str) -> int:
    """Count code cells in a Jupyter notebook (D-04).

    Parses .ipynb JSON and counts cells where ``cell_type == "code"``.
    Returns 0 on any error (JSONDecodeError, KeyError, OSError) per T-10-02.

    Args:
        notebook_path: Absolute path to the .ipynb file.

    Returns:
        Number of code cells, or 0 on error.
    """
    try:
        text = pathlib.Path(notebook_path).read_text(encoding="utf-8")
        nb = json.loads(text)
        cells = nb["cells"]
        return sum(1 for c in cells if c.get("cell_type") == "code")
    except (json.JSONDecodeError, KeyError, OSError, TypeError):
        return 0


def _is_test_file(filename: str) -> bool:
    """Check if a filename matches test file patterns (AUTO-01)."""
    for pattern in _TEST_FILE_PATTERNS:
        if pattern.match(filename):
            return True
    return False


def script_requires_model(source: str) -> bool:
    """DEBT-56.1: Decide whether a script loads a model.

    Returns True if the source imports any known LLM / model-runtime
    package, carries an ``# @uses-model`` hint, or declares a
    ``requires_model: true`` metadata line. Otherwise False.

    The default at the asset / job-request layer remains True
    (conservative); this heuristic only lowers it for scripts whose
    text contains no model-use signal.
    """
    if not source:
        return False
    for pattern in _MODEL_USE_PATTERNS:
        if pattern.search(source):
            return True
    return False


def _has_pytest_config(project_root: pathlib.Path) -> Optional[str]:
    """Check for pytest configuration files.

    Returns the config file path as a string if found, or None.
    Checks (in order):
    1. pyproject.toml with [tool.pytest.ini_options]
    2. pytest.ini
    3. setup.cfg with [tool:pytest]
    """
    # Check pyproject.toml
    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8")
            if "[tool.pytest.ini_options]" in text:
                return str(pyproject.resolve())
        except OSError:
            pass

    # Check pytest.ini
    pytest_ini = project_root / "pytest.ini"
    if pytest_ini.exists():
        return str(pytest_ini.resolve())

    # Check setup.cfg
    setup_cfg = project_root / "setup.cfg"
    if setup_cfg.exists():
        try:
            text = setup_cfg.read_text(encoding="utf-8")
            if "[tool:pytest]" in text:
                return str(setup_cfg.resolve())
        except OSError:
            pass

    return None


def discover_assets(
    project_root: str,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> AssetInventory:
    """Recursively discover all runnable assets in a project directory.

    Scans for:
    1. Notebooks (.ipynb) with at least one code cell (D-04)
    2. Python scripts with entrypoints, excluding test files (D-01, AUTO-01)
    3. Snakefiles and .smk files
    4. Pytest suite (one asset from config presence) (Pitfall 6)

    All paths resolved to absolute and verified within project_root (T-10-01).
    Include/exclude patterns applied via fnmatch on relative paths (D-02).

    Args:
        project_root: Absolute path to the project directory.
        include_patterns: If provided, only assets matching at least one
            pattern are included (fnmatch on relative path).
        exclude_patterns: If provided, assets matching any pattern are
            excluded (fnmatch on relative path).

    Returns:
        AssetInventory with discovered assets and per-type counts.
    """
    root = pathlib.Path(project_root).resolve()
    assets: list[DiscoveredAsset] = []

    def _is_within_root(p: pathlib.Path) -> bool:
        """T-10-01: verify resolved path starts with project_root.

        Uses Path.relative_to() instead of string startswith() to prevent
        prefix-collision bypasses (e.g. /tmp/project-evil matching /tmp/project).
        """
        try:
            p.resolve().relative_to(root)
            return True
        except ValueError:
            return False

    def _relative_path(p: pathlib.Path) -> str:
        """Get path relative to project root for pattern matching."""
        return str(p.resolve().relative_to(root))

    def _matches_include(rel_path: str) -> bool:
        """D-02: check if path matches at least one include pattern."""
        if include_patterns is None:
            return True
        return any(fnmatch.fnmatch(rel_path, pat) for pat in include_patterns)

    def _matches_exclude(rel_path: str) -> bool:
        """D-02: check if path matches any exclude pattern."""
        if exclude_patterns is None:
            return False
        return any(fnmatch.fnmatch(rel_path, pat) for pat in exclude_patterns)

    # 1. Notebooks (D-03: full recursive, D-04: code cell filter)
    for nb_path in root.rglob("*.ipynb"):
        if not _is_within_root(nb_path):
            continue
        rel = _relative_path(nb_path)
        if not _matches_include(rel) or _matches_exclude(rel):
            continue
        cell_count = count_code_cells(str(nb_path))
        if cell_count > 0:
            assets.append(DiscoveredAsset(
                path=str(nb_path.resolve()),
                asset_type="notebook",
                has_entrypoint=True,
                code_cell_count=cell_count,
            ))

    # 2. Python scripts (D-01: entrypoint filter, AUTO-01: exclude test files)
    for py_path in root.rglob("*.py"):
        if not _is_within_root(py_path):
            continue
        # Exclude test files
        if _is_test_file(py_path.name):
            continue
        rel = _relative_path(py_path)
        if not _matches_include(rel) or _matches_exclude(rel):
            continue
        try:
            source = py_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if has_entrypoint(source):
            assets.append(DiscoveredAsset(
                path=str(py_path.resolve()),
                asset_type="script",
                has_entrypoint=True,
                requires_model=script_requires_model(source),
            ))

    # 3. Snakefiles and .smk files
    for snake_path in root.rglob("Snakefile"):
        if not _is_within_root(snake_path):
            continue
        rel = _relative_path(snake_path)
        if not _matches_include(rel) or _matches_exclude(rel):
            continue
        assets.append(DiscoveredAsset(
            path=str(snake_path.resolve()),
            asset_type="snakefile",
            has_entrypoint=True,
        ))

    for smk_path in root.rglob("*.smk"):
        if not _is_within_root(smk_path):
            continue
        rel = _relative_path(smk_path)
        if not _matches_include(rel) or _matches_exclude(rel):
            continue
        assets.append(DiscoveredAsset(
            path=str(smk_path.resolve()),
            asset_type="snakefile",
            has_entrypoint=True,
        ))

    # 4. Pytest suite detection (Pitfall 6: one asset, not individual test files)
    pytest_config = _has_pytest_config(root)
    if pytest_config is not None:
        assets.append(DiscoveredAsset(
            path=pytest_config,
            asset_type="pytest_suite",
            has_entrypoint=True,
        ))

    # Build counts from asset types
    type_counter = Counter(a.asset_type for a in assets)
    counts = dict(type_counter)

    return AssetInventory(
        project_name=root.name,
        project_root=str(root),
        assets=tuple(assets),
        counts=counts,
    )


# ---------------------------------------------------------------------------
# Task-tier classification constants (Plan 02)
# ---------------------------------------------------------------------------

# Antibody system -> task tier mapping (D-05)
ANTIBODY_TIER_MAP: dict[str, str] = {
    "data_analysis": "data-analysis",
    "logic": "science",
    "methodology": "science",
    "citation": "science",
    "infra": "code",
}

# Keyword heuristic patterns (D-06) -- SECONDARY signal
KEYWORD_TIER_MAP: dict[str, list[re.Pattern[str]]] = {
    "data-analysis": [
        re.compile(r"import\s+(?:pandas|numpy|matplotlib|seaborn|scipy|statsmodels)"),
        re.compile(r"from\s+(?:pandas|numpy|matplotlib|seaborn|scipy|statsmodels)\s+import"),
        re.compile(r"\.(?:read_csv|DataFrame|ndarray|plot|hist|describe)\b"),
    ],
    "science": [
        re.compile(r"import\s+(?:biopython|Bio|rdkit|mdtraj|prody)"),
        re.compile(r"from\s+(?:Bio|rdkit|mdtraj|prody)\s+import"),
        re.compile(r"(?:PMID|DOI|pubmed|citation|hypothesis|experiment)\b", re.I),
    ],
    "test-suite": [
        re.compile(r"import\s+pytest"),
        re.compile(r"from\s+pytest\s+import"),
        re.compile(r"def\s+test_\w+"),
    ],
    "pipeline-step": [
        re.compile(r"(?:rule\s+\w+:|snakemake|nextflow|cwltool)"),
        re.compile(r"(?:input:|output:|shell:)"),
    ],
}

# Task tier -> benchmark suite mapping
TIER_SUITE_MAP: dict[str, str] = {
    "science": "science",
    "code": "code",
    "data-analysis": "science",    # closest benchmark proxy
    "test-suite": "code",          # test code is code
    "pipeline-step": "code",       # pipeline steps are code-like
}

# Model tier escalation chain (D-13)
MODEL_TIERS: list[str] = ["tiny", "small", "medium", "frontier"]

# Model tier size ranges in billions of parameters
TIER_SIZE_RANGES: dict[str, tuple[float, float]] = {
    "tiny": (0, 2.0),       # <=2B (qwen3:1.7b, smollm2)
    "small": (2.0, 5.0),    # 2-5B (qwen3:4b, phi4-mini)
    "medium": (5.0, 15.0),  # 5-15B (qwen3:8b, qwen2.5-coder:7b, deepseek-r1:8b)
}


ASSET_WORKLOAD_MAP: dict[str, WorkloadClass] = {
    "notebook": WorkloadClass.NOTEBOOK,
    "script": WorkloadClass.VALIDATED_SCRIPT,
    "snakefile": WorkloadClass.PIPELINE_STEP,
    "pytest_suite": WorkloadClass.PYTEST_SUITE,
}

WORKFLOW_QUEUE_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# TierMapping Pydantic model
# ---------------------------------------------------------------------------


class TierMapping(BaseModel):
    """Mapping of task tier to selected model with metadata."""

    model_config = ConfigDict(frozen=True)

    tier: str
    """One of: science, code, data-analysis, test-suite, pipeline-step."""

    model: str
    """Ollama model name (e.g. 'qwen3:8b')."""

    model_size_b: float | None
    """Size in billions (None if unparseable)."""

    quality_mean: float | None
    """Quality score from benchmark."""

    degraded_confidence: bool
    """D-08: True if no model met threshold -- best available used instead."""


# ---------------------------------------------------------------------------
# Antibody lazy import helper
# ---------------------------------------------------------------------------


def _import_antibody_system(key: str) -> type | None:
    """Lazy-import an antibody system class from ANTIBODY_REGISTRY.

    Returns the class (not instance) if importable, or None on any error.
    T-10-04: all import errors caught and logged.
    """
    from ollarma.guardrail import ANTIBODY_REGISTRY

    if key not in ANTIBODY_REGISTRY:
        return None

    dotted_path = ANTIBODY_REGISTRY[key]
    module_path, _, class_name = dotted_path.rpartition(".")

    try:
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to import antibody system %s: %s", key, exc)
        return None


# ---------------------------------------------------------------------------
# Classification functions
# ---------------------------------------------------------------------------


def _is_antigence_available() -> bool:
    """Check Antigence availability via guardrail module.

    Separate function so tests can patch harness.autopilot.ANTIGENCE_AVAILABLE
    at module level without circular import issues.
    """
    from ollarma.guardrail import ANTIGENCE_AVAILABLE as _avail
    return _avail


def _workload_for_asset(asset: DiscoveredAsset) -> WorkloadClass:
    """Map discovered asset types to explicit workflow execution classes."""
    return ASSET_WORKLOAD_MAP.get(asset.asset_type, WorkloadClass.VALIDATED_SCRIPT)


# Module-level flag for patchability in tests
ANTIGENCE_AVAILABLE: bool = False
try:
    from ollarma.guardrail import ANTIGENCE_AVAILABLE  # type: ignore[assignment]
except ImportError:
    pass


# Domain-specific verify method names per antibody key.
# Real Antigence antibody systems do NOT have a generic verify() --
# each uses a domain-specific method (e.g. verify_logic, verify_analysis).
_ANTIBODY_VERIFY_METHODS: dict[str, str] = {
    "data_analysis": "verify_analysis",
    "logic": "verify_logic",
    "methodology": "verify_methodology",
    "infra": "verify_infra",
    # "citation" omitted: verify_citation takes Dict[str, str], not str
}


def classify_with_antibodies(content: str) -> tuple[str | None, float]:
    """Classify content via Antigence antibodies (D-05 -- PRIMARY).

    Lazy-imports each antibody system from ANTIBODY_REGISTRY keys that appear
    in ANTIBODY_TIER_MAP. For each system, calls its domain-specific verify
    method and collects a classification_confidence score. Returns the tier
    corresponding to the antibody with highest affinity, and that score.

    Returns (None, 0.0) when Antigence is not available or no antibody
    produces confident binding.

    T-10-04: all antibody calls wrapped in try/except.
    """
    if not ANTIGENCE_AVAILABLE:
        return (None, 0.0)

    best_tier: str | None = None
    best_affinity: float = 0.0

    for ab_key, tier in ANTIBODY_TIER_MAP.items():
        verify_method = _ANTIBODY_VERIFY_METHODS.get(ab_key)
        if verify_method is None:
            # No compatible verify method for this antibody key (e.g. citation)
            continue

        try:
            system_class = _import_antibody_system(ab_key)
            if system_class is None:
                continue

            system = system_class()
            method = getattr(system, verify_method, None)
            if method is None:
                logger.warning("Antibody %s has no method %s", ab_key, verify_method)
                continue

            result = method(content)

            # Real Antigence results use classification_confidence (not confidence)
            affinity = getattr(result, "classification_confidence", 0.0)
            if affinity is None:
                affinity = 1.0 if result else 0.0

            if affinity > best_affinity:
                best_affinity = affinity
                best_tier = tier
        except Exception as exc:
            # T-10-04: never crash on antibody failure
            logger.warning("Antibody %s failed: %s", ab_key, exc)
            continue

    if best_affinity <= 0.0:
        return (None, 0.0)

    return (best_tier, best_affinity)


def classify_with_keywords(content: str) -> tuple[str | None, float]:
    """Classify content via keyword heuristics (D-06 -- SECONDARY).

    Iterates KEYWORD_TIER_MAP entries, counts regex matches per tier.
    Returns tier with most matches and a confidence proportional to
    match count (matches / total_patterns for that tier).

    Returns (None, 0.0) if no patterns match.
    """
    best_tier: str | None = None
    best_confidence: float = 0.0

    for tier, patterns in KEYWORD_TIER_MAP.items():
        match_count = sum(1 for p in patterns if p.search(content))
        if match_count > 0:
            confidence = match_count / len(patterns)
            if confidence > best_confidence:
                best_confidence = confidence
                best_tier = tier

    if best_tier is None:
        return (None, 0.0)

    return (best_tier, best_confidence)


def classify_asset(asset: DiscoveredAsset, content: str) -> str:
    """Classify an asset into a task tier using three-layer chain.

    Classification priority:
    1. Asset type override: snakefile -> "pipeline-step", pytest_suite -> "test-suite"
    2. Antibody classification (D-05) -- if confident (> 0.5), use it
    3. Keyword classification (D-06) -- if any match, use it
    4. Ultimate fallback: "code" (D-07)

    Args:
        asset: The discovered asset to classify.
        content: Source content of the asset for analysis.

    Returns:
        Task tier string: science, code, data-analysis, test-suite, or pipeline-step.
    """
    # 1. Asset type overrides
    if asset.asset_type == "snakefile":
        return "pipeline-step"
    if asset.asset_type == "pytest_suite":
        return "test-suite"

    # 2. Antibody classification (D-05 -- primary)
    ab_tier, ab_confidence = classify_with_antibodies(content)
    if ab_tier is not None and ab_confidence > 0.5:
        return ab_tier

    # 3. Keyword classification (D-06 -- secondary)
    kw_tier, _kw_confidence = classify_with_keywords(content)
    if kw_tier is not None:
        return kw_tier

    # 4. Ultimate fallback (D-07)
    return "code"


# ---------------------------------------------------------------------------
# Tier-model mapping from benchmark data (AUTO-02)
# ---------------------------------------------------------------------------


def build_tier_model_map(
    threshold: float = 0.9,
    results_dir: pathlib.Path | None = None,
) -> dict[str, TierMapping]:
    """Map task tiers to smallest model above threshold from benchmark data.

    Steps:
    1. Find latest sealed results (return {} on FileNotFoundError)
    2. Aggregate trials via reporter.aggregate_trials()
    3. For each benchmark suite, find smallest model above threshold
    4. If no model meets threshold (D-08): use best composite_score with degraded flag
    5. Map back to all task tiers sharing that suite (via TIER_SUITE_MAP)

    Args:
        threshold: Minimum quality_mean to select model (D-09, default 0.9).
        results_dir: Path to results directory (default: Path("results")).

    Returns:
        Dict mapping tier name to TierMapping, or empty dict if no data.
    """
    from ollarma.reporter import (
        aggregate_trials,
        extract_model_size,
        find_latest_sealed,
        load_sealed_results,
    )

    effective_dir = results_dir or pathlib.Path("results")

    # Step 1: find latest sealed results
    try:
        sealed_path = find_latest_sealed(effective_dir)
    except FileNotFoundError:
        return {}

    # Step 2: load and aggregate
    rows = load_sealed_results(sealed_path)
    all_stats = aggregate_trials(rows)

    if not all_stats:
        return {}

    # Step 3-4: per-suite model selection
    # Deduplicate suites from TIER_SUITE_MAP
    unique_suites = set(TIER_SUITE_MAP.values())
    suite_selection: dict[str, tuple[str, float | None, float | None, bool]] = {}

    for suite in unique_suites:
        suite_stats = [s for s in all_stats if s.suite == suite]
        if not suite_stats:
            continue

        # Filter models above threshold
        above_threshold = [
            s for s in suite_stats
            if s.quality_mean is not None and s.quality_mean >= threshold
        ]

        if above_threshold:
            # Sort by model size ascending, pick smallest
            above_threshold.sort(
                key=lambda s: extract_model_size(s.model) or float("inf")
            )
            selected = above_threshold[0]
            degraded = False
        else:
            # D-08: no model meets threshold -- use best composite_score
            selected = max(suite_stats, key=lambda s: s.composite_score)
            degraded = True

        suite_selection[suite] = (
            selected.model,
            extract_model_size(selected.model),
            selected.quality_mean,
            degraded,
        )

    # Step 5: map back to all tiers
    result: dict[str, TierMapping] = {}
    for tier, suite in TIER_SUITE_MAP.items():
        if suite in suite_selection:
            model_name, model_size, quality, degraded = suite_selection[suite]
            result[tier] = TierMapping(
                tier=tier,
                model=model_name,
                model_size_b=model_size,
                quality_mean=quality,
                degraded_confidence=degraded,
            )

    return result


# ---------------------------------------------------------------------------
# Tier-based timeout defaults in seconds (D-12)
# ---------------------------------------------------------------------------

TIER_TIMEOUT_DEFAULTS: dict[str, int] = {
    "notebook": 300,
    "script": 120,
    "pytest_suite": 180,
    "snakefile": 600,
}


# ---------------------------------------------------------------------------
# AssetResult Pydantic model
# ---------------------------------------------------------------------------


class AssetResult(BaseModel):
    """Result of executing one asset."""

    model_config = ConfigDict(frozen=True)

    asset_path: str
    asset_type: str
    task_tier: str
    model_used: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    tokens_consumed: int = 0
    escalated: bool = False
    """True if this was a retry with escalated model."""
    escalation_needed: bool = False
    """True if all tiers failed."""
    degraded_confidence: bool = False
    """D-08: True if model was below threshold."""


# ---------------------------------------------------------------------------
# Asset runners
# ---------------------------------------------------------------------------


def _run_notebook(path: str, timeout: int = 300) -> tuple[int, str, str, float]:
    """Execute a notebook via papermill with deferred import (D-10).

    Returns (exit_code, stdout, stderr, duration_s).
    exit_code: 0=success, 1=execution error, 2=papermill not installed/other.
    """
    start = time.monotonic()
    try:
        import papermill as pm  # noqa: F811 — deferred import
    except (ImportError, ModuleNotFoundError):
        return (2, "", "papermill not installed -- escalate", 0.0)

    try:
        output_path = str(Path(path).with_suffix(".autopilot.ipynb"))
        pm.execute_notebook(
            path,
            output_path,
            execution_timeout=timeout,
            cwd=str(Path(path).parent),
            progress_bar=False,
        )
        duration = time.monotonic() - start
        return (0, "Notebook executed successfully", "", duration)
    except pm.PapermillExecutionError as exc:
        duration = time.monotonic() - start
        return (1, "", str(exc), duration)
    except Exception as exc:
        duration = time.monotonic() - start
        return (2, "", str(exc), duration)


def _run_script(path: str, timeout: int = 120) -> tuple[int, str, str, float]:
    """Execute a Python script via subprocess with timeout (D-12).

    T-10-05: no shell=True, cwd restricted to asset parent directory.
    Returns (exit_code, stdout, stderr, duration_s).
    exit_code: -1 on timeout.
    """
    start = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path(path).parent),
        )
        duration = time.monotonic() - start
        return (result.returncode, result.stdout, result.stderr, duration)
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return (-1, "", f"Timeout after {timeout}s", duration)


def _run_pytest(project_root: str, timeout: int = 180) -> tuple[int, str, str, float]:
    """Execute pytest as subprocess in the project root (Pitfall 6).

    Returns (exit_code, stdout, stderr, duration_s).
    """
    start = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--tb=short", "-q"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=project_root,
        )
        duration = time.monotonic() - start
        return (result.returncode, result.stdout, result.stderr, duration)
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return (-1, "", f"Timeout after {timeout}s", duration)


def _run_snakefile(path: str, timeout: int = 600) -> tuple[int, str, str, float]:
    """Execute a Snakefile via snakemake --dryrun (T-10-08).

    Returns (exit_code, stdout, stderr, duration_s).
    exit_code: 2 if snakemake not installed.
    """
    if shutil.which("snakemake") is None:
        return (2, "", "snakemake not installed -- escalate", 0.0)

    start = time.monotonic()
    try:
        result = subprocess.run(
            ["snakemake", "--dryrun", "-s", path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path(path).parent),
        )
        duration = time.monotonic() - start
        return (result.returncode, result.stdout, result.stderr, duration)
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return (-1, "", f"Timeout after {timeout}s", duration)


def execute_asset(
    asset: DiscoveredAsset,
    task_tier: str,
    model: str,
    timeout: int | None = None,
    degraded: bool = False,
    escalated: bool = False,
) -> AssetResult:
    """Execute a single asset by routing to the correct runner.

    Routes by asset_type:
    - "notebook" -> _run_notebook
    - "script" -> _run_script
    - "pytest_suite" -> _run_pytest (uses parent dir as project_root)
    - "snakefile" -> _run_snakefile

    Timeout defaults from TIER_TIMEOUT_DEFAULTS when not provided (D-12).
    T-10-07: all runners have timeout parameter.

    Args:
        asset: The discovered asset to execute.
        task_tier: Classified task tier for this asset.
        model: Ollama model name assigned to this asset.
        timeout: Override timeout in seconds (default: from TIER_TIMEOUT_DEFAULTS).
        degraded: D-08 flag -- True if model was below quality threshold.
        escalated: True if this is an escalation retry with a higher-tier model.

    Returns:
        AssetResult with all execution metadata populated.
    """
    effective_timeout = timeout if timeout is not None else TIER_TIMEOUT_DEFAULTS.get(asset.asset_type, 120)

    if asset.asset_type == "notebook":
        exit_code, stdout, stderr, duration = _run_notebook(
            asset.path, effective_timeout
        )
    elif asset.asset_type == "script":
        exit_code, stdout, stderr, duration = _run_script(
            asset.path, effective_timeout
        )
    elif asset.asset_type == "pytest_suite":
        project_root = str(Path(asset.path).parent)
        exit_code, stdout, stderr, duration = _run_pytest(
            project_root, effective_timeout
        )
    elif asset.asset_type == "snakefile":
        exit_code, stdout, stderr, duration = _run_snakefile(
            asset.path, effective_timeout
        )
    else:
        exit_code, stdout, stderr, duration = (
            2, "", f"Unknown asset_type: {asset.asset_type}", 0.0,
        )

    return AssetResult(
        asset_path=asset.path,
        asset_type=asset.asset_type,
        task_tier=task_tier,
        model_used=model,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_s=duration,
        degraded_confidence=degraded,
        escalated=escalated,
    )


# ---------------------------------------------------------------------------
# AutopilotReport Pydantic model
# ---------------------------------------------------------------------------


class AutopilotReport(BaseModel):
    """Report from an autopilot run."""

    model_config = ConfigDict(frozen=True)

    project_name: str
    project_root: str
    inventory: AssetInventory
    tier_map: dict[str, TierMapping]
    """May be empty if no benchmark data."""
    results: tuple[AssetResult, ...]
    """Empty if discovery-only."""
    total_assets: int
    passed: int
    failed: int
    escalation_needed: int
    tokens_consumed_local: int
    """D-14: raw token count served locally."""
    run_executed: bool
    """True if --run was used."""


# ---------------------------------------------------------------------------
# Escalation helpers
# ---------------------------------------------------------------------------


def _get_next_tier_model(
    current_model: str,
    tier_map: dict[str, TierMapping],
    task_tier: str,
) -> str | None:
    """Get the next-tier escalation model for a failed asset (D-13).

    Given the current model, determine its tier from TIER_SIZE_RANGES,
    look up the next tier in MODEL_TIERS, and find a model for the
    corresponding benchmark suite.

    Returns None if already at "frontier" or no next tier model available.
    """
    from ollarma.reporter import extract_model_size

    current_size = extract_model_size(current_model)
    if current_size is None:
        return None

    # Determine current tier
    current_tier_name: str | None = None
    for tier_name, (low, high) in TIER_SIZE_RANGES.items():
        if low <= current_size < high:
            current_tier_name = tier_name
            break

    if current_tier_name is None:
        # Size doesn't fit any local tier (could be frontier/very large)
        return None

    # Find next tier in escalation chain
    try:
        current_idx = MODEL_TIERS.index(current_tier_name)
    except ValueError:
        return None

    next_idx = current_idx + 1
    if next_idx >= len(MODEL_TIERS):
        return None  # Already at last tier

    next_tier_name = MODEL_TIERS[next_idx]

    if next_tier_name == "frontier":
        # No local model for frontier tier
        return None

    # Find a model in tier_map that is in the next tier's size range
    next_range = TIER_SIZE_RANGES.get(next_tier_name)
    if next_range is None:
        return None

    # Look through all tier_map entries for a model in the next size range
    for _tier_key, mapping in tier_map.items():
        if mapping.model_size_b is not None:
            if next_range[0] <= mapping.model_size_b < next_range[1]:
                return mapping.model

    return None


def _read_asset_content(asset: DiscoveredAsset) -> str:
    """Read asset content for classification.

    For notebooks, extract code cell sources joined by newlines.
    For .py/.smk files, read text.
    Returns empty string on error.
    """
    try:
        p = pathlib.Path(asset.path)
        if asset.asset_type == "notebook":
            text = p.read_text(encoding="utf-8")
            nb = json.loads(text)
            cells = nb.get("cells", [])
            sources = []
            for cell in cells:
                if cell.get("cell_type") == "code":
                    src = cell.get("source", [])
                    if isinstance(src, list):
                        sources.append("".join(src))
                    else:
                        sources.append(str(src))
            return "\n".join(sources)
        else:
            return p.read_text(encoding="utf-8", errors="replace")
    except (OSError, json.JSONDecodeError, TypeError):
        return ""


def _get_workflow_scheduler():
    """Lazy-import the shared workflow scheduler to avoid eager service imports."""
    from ollarma.service import get_scheduler

    return get_scheduler()


def _autopilot_run_paths(project_root: str, run_id: str) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Return bounded receipt and checkpoint paths for one autopilot run."""
    repo_root = pathlib.Path(project_root).resolve()
    run_root = repo_root / ".ollarma" / "autopilot" / run_id
    return repo_root, run_root / "receipts.json", run_root / "checkpoint.json"


def _record_autopilot_receipt(
    *,
    repo_root: pathlib.Path,
    receipts_path: pathlib.Path,
    checkpoint_path: pathlib.Path,
    checkpoint_state: CheckpointState,
    asset: DiscoveredAsset,
    stage: str,
    lane: str,
    status: str,
    retry_count: int,
    reason_code: str | None,
    duration_s: float,
    model: str,
) -> CheckpointState:
    """Persist one sanitized autopilot receipt and the resulting checkpoint."""
    relative_asset = pathlib.Path(asset.path).resolve().relative_to(repo_root).as_posix()
    checkpoint_ref = {"repo_relative": checkpoint_path.relative_to(repo_root).as_posix()}
    receipt = append_run_receipt(
        repo_root=repo_root,
        receipts_path=receipts_path,
        receipt=RunReceipt(
            run_id=checkpoint_state.run_id,
            stage=stage,
            step_id=relative_asset,
            task_or_command=f"{asset.asset_type}:{relative_asset}",
            lane=lane,
            status=status,
            inputs=({"repo_relative": relative_asset},),
            outputs=(
                {"model": model},
                checkpoint_ref,
            ),
            duration_s=duration_s,
            retry_count=retry_count,
            reason_code=reason_code,
            checkpoint_ref=checkpoint_ref,
        ),
    )
    updated = apply_receipt_to_checkpoint(checkpoint_state, receipt)
    write_checkpoint_state(
        repo_root=repo_root,
        checkpoint_path=checkpoint_path,
        state=updated,
    )
    return updated


# ---------------------------------------------------------------------------
# Autopilot orchestration loop
# ---------------------------------------------------------------------------


def run_autopilot(
    project_name: str,
    project_root: str,
    run: bool = False,
    threshold: float = 0.9,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    results_dir: pathlib.Path | None = None,
) -> AutopilotReport:
    """Orchestrate the full autopilot discovery-classify-execute pipeline.

    Steps:
    1. Discover assets via discover_assets() (D-01/D-02/D-03/D-04)
    2. Build tier-model map from benchmark data (D-09, AUTO-02)
    3. Classify each asset (D-05/D-06/D-07)
    4. If run=False: return discovery-only report
    5. If run=True: sort by tier (Pitfall 3), execute sequentially (D-11),
       retry failures with next-tier model (D-13), collect results

    Args:
        project_name: Human-readable project name.
        project_root: Absolute path to project directory.
        run: If True, execute assets. If False, discovery-only.
        threshold: Minimum quality_mean for model selection (D-09).
        include_patterns: Include filter patterns for discovery (D-02).
        exclude_patterns: Exclude filter patterns for discovery (D-02).
        results_dir: Path to benchmark results directory.

    Returns:
        AutopilotReport with full run results or discovery-only inventory.
    """
    # Step 1: discover assets
    inventory = discover_assets(project_root, include_patterns, exclude_patterns)

    # Step 2: build tier-model map
    tier_map = build_tier_model_map(threshold, results_dir)

    # Step 3: classify each asset
    classified: list[tuple[DiscoveredAsset, str, WorkloadClass]] = []
    for asset in inventory.assets:
        content = _read_asset_content(asset)
        tier = classify_asset(asset, content)
        classified.append((asset, tier, _workload_for_asset(asset)))

    # Step 4: discovery-only mode
    if not run:
        return AutopilotReport(
            project_name=project_name,
            project_root=project_root,
            inventory=inventory,
            tier_map=tier_map,
            results=(),
            total_assets=len(inventory.assets),
            passed=0,
            failed=0,
            escalation_needed=0,
            tokens_consumed_local=0,
            run_executed=False,
        )

    # Step 5: sort by model tier to minimize model swaps (Pitfall 3)
    from ollarma.scheduler import JobRequest, Lane, SchedulerAdmissionError

    scheduler = _get_workflow_scheduler()
    autopilot_run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    repo_root, receipts_path, checkpoint_path = _autopilot_run_paths(project_root, autopilot_run_id)
    checkpoint_state = CheckpointState(
        run_id=autopilot_run_id,
        current_stage="preflight",
        last_validated_stage=None,
        last_receipt_hash="0" * 64,
        retry_budget_remaining=1,
        resume_from_step=None,
    )

    def _tier_sort_key(item: tuple[DiscoveredAsset, str, WorkloadClass]) -> float:
        """Sort key: model size (smaller first) for tier-based grouping."""
        _asset, tier, _workload = item
        mapping = tier_map.get(tier)
        if mapping and mapping.model_size_b is not None:
            return mapping.model_size_b
        # Default: treat unmapped tiers as medium-sized
        return 5.0

    classified.sort(key=_tier_sort_key)

    # Step 6-8: execute with escalation
    all_results: list[AssetResult] = []

    for asset, tier, workload_class in classified:
        mapping = tier_map.get(tier)
        model = resolve_selection(
            workload_class,
            results_dir=results_dir or pathlib.Path("results"),
        )
        degraded = mapping.degraded_confidence if mapping else False

        request = JobRequest(
            project=project_name,
            lane=Lane.WORKFLOW_EXECUTION_QUEUE,
            model=model,
            requires_model=asset.requires_model,
        )
        try:
            execute_route = determine_next_stage(
                requested_stage=WorkflowStage.EXECUTE,
                checkpoint_state=checkpoint_state,
                reason_code=None,
            )
            with scheduler.lease(request, queue_timeout_s=WORKFLOW_QUEUE_TIMEOUT_S):
                result = execute_asset(asset, tier, model, degraded=degraded)
        except SchedulerAdmissionError as exc:
            checkpoint_state = _record_autopilot_receipt(
                repo_root=repo_root,
                receipts_path=receipts_path,
                checkpoint_path=checkpoint_path,
                checkpoint_state=checkpoint_state,
                asset=asset,
                stage="execute",
                lane=execute_route.owning_lane,
                status="rejected",
                retry_count=0,
                reason_code=exc.reason_code,
                duration_s=0.0,
                model=model,
            )
            result = AssetResult(
                asset_path=asset.path,
                asset_type=asset.asset_type,
                task_tier=tier,
                model_used=model,
                exit_code=2,
                stdout="",
                stderr=f"{exc.reason_code}: {exc.detail}",
                duration_s=0.0,
                escalated=False,
                escalation_needed=True,
                degraded_confidence=degraded,
            )
            all_results.append(result)
            continue

        if result.exit_code != 0:
            failure_status = "retryable_failure" if result.exit_code in {-1, 1} else "deterministic_failure"
            failure_route = determine_next_stage(
                requested_stage=WorkflowStage.EXECUTE,
                checkpoint_state=checkpoint_state,
                reason_code=f"EXIT_{result.exit_code}",
            )
            can_retry = failure_route.retry_allowed and checkpoint_state.retry_budget_remaining > 0
            checkpoint_state = _record_autopilot_receipt(
                repo_root=repo_root,
                receipts_path=receipts_path,
                checkpoint_path=checkpoint_path,
                checkpoint_state=checkpoint_state,
                asset=asset,
                stage="execute",
                lane=failure_route.owning_lane,
                status=failure_status,
                retry_count=0,
                reason_code=f"EXIT_{result.exit_code}",
                duration_s=result.duration_s,
                model=model,
            )
            # Step 7: try escalation (D-13) -- retry ONCE with next-tier model
            next_model = _get_next_tier_model(model, tier_map, tier)
            if can_retry and next_model is not None:
                retry_request = JobRequest(
                    project=project_name,
                    lane=Lane.WORKFLOW_EXECUTION_QUEUE,
                    model=next_model,
                )
                try:
                    with scheduler.lease(
                        retry_request,
                        queue_timeout_s=WORKFLOW_QUEUE_TIMEOUT_S,
                    ):
                        result = execute_asset(
                            asset, tier, next_model, degraded=degraded, escalated=True,
                        )
                except SchedulerAdmissionError as exc:
                    checkpoint_state = _record_autopilot_receipt(
                        repo_root=repo_root,
                        receipts_path=receipts_path,
                        checkpoint_path=checkpoint_path,
                        checkpoint_state=checkpoint_state,
                        asset=asset,
                        stage="execute",
                        lane=failure_route.owning_lane,
                        status="rejected",
                        retry_count=1,
                        reason_code=exc.reason_code,
                        duration_s=0.0,
                        model=next_model,
                    )
                    result = AssetResult(
                        asset_path=asset.path,
                        asset_type=asset.asset_type,
                        task_tier=tier,
                        model_used=next_model,
                        exit_code=2,
                        stdout="",
                        stderr=f"{exc.reason_code}: {exc.detail}",
                        duration_s=0.0,
                        escalated=True,
                        escalation_needed=True,
                        degraded_confidence=degraded,
                    )
                if result.exit_code == 0:
                    checkpoint_state = _record_autopilot_receipt(
                        repo_root=repo_root,
                        receipts_path=receipts_path,
                        checkpoint_path=checkpoint_path,
                        checkpoint_state=checkpoint_state,
                        asset=asset,
                        stage="validate",
                        lane=determine_next_stage(
                            requested_stage=WorkflowStage.VALIDATE,
                            checkpoint_state=checkpoint_state,
                            sidecar_candidate=True,
                        ).owning_lane,
                        status="completed",
                        retry_count=1,
                        reason_code=None,
                        duration_s=0.0,
                        model=next_model,
                    )
                    checkpoint_state = _record_autopilot_receipt(
                        repo_root=repo_root,
                        receipts_path=receipts_path,
                        checkpoint_path=checkpoint_path,
                        checkpoint_state=checkpoint_state,
                        asset=asset,
                        stage="summarize",
                        lane=determine_next_stage(
                            requested_stage=WorkflowStage.SUMMARIZE,
                            checkpoint_state=checkpoint_state,
                            sidecar_candidate=True,
                        ).owning_lane,
                        status="completed",
                        retry_count=1,
                        reason_code=None,
                        duration_s=0.0,
                        model=next_model,
                    )
                else:
                    checkpoint_state = _record_autopilot_receipt(
                        repo_root=repo_root,
                        receipts_path=receipts_path,
                        checkpoint_path=checkpoint_path,
                        checkpoint_state=checkpoint_state,
                        asset=asset,
                        stage="interpret/escalate",
                        lane=determine_next_stage(
                            requested_stage=WorkflowStage.INTERPRET_ESCALATE,
                            checkpoint_state=checkpoint_state,
                            reason_code=f"EXIT_{result.exit_code}",
                        ).owning_lane,
                        status="rejected",
                        retry_count=1,
                        reason_code=f"EXIT_{result.exit_code}",
                        duration_s=result.duration_s,
                        model=next_model,
                    )
                    # Escalation failed -- mark as needing external help
                    result = result.model_copy(update={
                        "escalation_needed": True,
                    })
            else:
                checkpoint_state = _record_autopilot_receipt(
                    repo_root=repo_root,
                    receipts_path=receipts_path,
                    checkpoint_path=checkpoint_path,
                    checkpoint_state=checkpoint_state,
                    asset=asset,
                    stage="interpret/escalate",
                    lane=determine_next_stage(
                        requested_stage=WorkflowStage.INTERPRET_ESCALATE,
                        checkpoint_state=checkpoint_state,
                        reason_code=f"EXIT_{result.exit_code}",
                    ).owning_lane,
                    status="rejected",
                    retry_count=0,
                    reason_code=f"EXIT_{result.exit_code}",
                    duration_s=result.duration_s,
                    model=model,
                )
                # No next tier available -- mark as escalation needed
                result = result.model_copy(update={"escalation_needed": True})
        else:
            checkpoint_state = _record_autopilot_receipt(
                repo_root=repo_root,
                receipts_path=receipts_path,
                checkpoint_path=checkpoint_path,
                checkpoint_state=checkpoint_state,
                asset=asset,
                stage="validate",
                lane=determine_next_stage(
                    requested_stage=WorkflowStage.VALIDATE,
                    checkpoint_state=checkpoint_state,
                    sidecar_candidate=True,
                ).owning_lane,
                status="completed",
                retry_count=0,
                reason_code=None,
                duration_s=0.0,
                model=model,
            )
            checkpoint_state = _record_autopilot_receipt(
                repo_root=repo_root,
                receipts_path=receipts_path,
                checkpoint_path=checkpoint_path,
                checkpoint_state=checkpoint_state,
                asset=asset,
                stage="summarize",
                lane=determine_next_stage(
                    requested_stage=WorkflowStage.SUMMARIZE,
                    checkpoint_state=checkpoint_state,
                    sidecar_candidate=True,
                ).owning_lane,
                status="completed",
                retry_count=0,
                reason_code=None,
                duration_s=0.0,
                model=model,
            )

        all_results.append(result)

    # Step 9-10: aggregate results
    results_tuple = tuple(all_results)
    tokens_total = sum(r.tokens_consumed for r in results_tuple)
    passed = sum(1 for r in results_tuple if r.exit_code == 0)
    failed = sum(1 for r in results_tuple if r.exit_code != 0)
    escalation_count = sum(1 for r in results_tuple if r.escalation_needed)

    # Step 11: return report
    return AutopilotReport(
        project_name=project_name,
        project_root=project_root,
        inventory=inventory,
        tier_map=tier_map,
        results=results_tuple,
        total_assets=len(inventory.assets),
        passed=passed,
        failed=failed,
        escalation_needed=escalation_count,
        tokens_consumed_local=tokens_total,
        run_executed=True,
    )
