"""evidence.py -- Append-only SHA-256 hash chain for sealed benchmark results.

Implements EVID-01 (tamper-evident receipt chain), EVID-02 (ModelSelectionArtifact
with stable_decision_hash), and EVID-04 (deterministic evidence root from
identical inputs).

All functions are pure except write_evidence_file and write_artifact_file (I/O).
All hashing goes through canonical_hash() to avoid Pitfall 6 (forgetting
OPT_SORT_KEYS).
"""
from __future__ import annotations

import hashlib
import pathlib
from collections import defaultdict

import orjson
from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GENESIS_PARENT_HASH: str = "0" * 64
"""Parent hash for the first receipt in the chain (64 hex zeros)."""

DETERMINISTIC_FIELDS: frozenset[str] = frozenset({
    "model",
    "task_id",
    "suite",
    "num_ctx",
    "raw_response",
    "quality_score",
    "model_digest",
    "ollama_version",
    "thinking_mode",
    "prompt_hash",
    "schema_version",
})
"""BenchmarkResult fields included in evidence hashing (11 fields).

Excluded (non-deterministic): prefill_tps, decode_tps, run_ts.
"""

NON_DETERMINISTIC_FIELDS: frozenset[str] = frozenset({
    "prefill_tps",
    "decode_tps",
    "run_ts",
})
"""BenchmarkResult fields excluded from evidence hashing (timing + timestamp)."""


# ---------------------------------------------------------------------------
# Canonical hashing -- single entry point (Pitfall 6)
# ---------------------------------------------------------------------------

def canonical_hash(obj: dict | list) -> str:
    """SHA-256 hex digest of canonical JSON (sorted keys, compact separators).

    This is the ONLY function that calls orjson.dumps for hashing purposes.
    All evidence chain code must go through this function to guarantee
    OPT_SORT_KEYS is always applied (Pitfall 6).
    """
    canonical_bytes = orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(canonical_bytes).hexdigest()


# ---------------------------------------------------------------------------
# Deterministic field extraction
# ---------------------------------------------------------------------------

def deterministic_row(row: dict) -> dict:
    """Extract only DETERMINISTIC_FIELDS from a BenchmarkResult dict.

    Timing fields (prefill_tps, decode_tps) and timestamp (run_ts) are
    excluded so that identical inputs produce identical evidence hashes
    regardless of performance variation (EVID-04).
    """
    return {k: v for k, v in row.items() if k in DETERMINISTIC_FIELDS}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class EvidenceReceipt(BaseModel):
    """One link in the append-only evidence hash chain."""

    sequence: int
    """0-indexed position in the chain."""

    row_hash: str
    """SHA-256 of canonical JSON of deterministic row fields."""

    parent_hash: str
    """Previous receipt's receipt_hash (or GENESIS_PARENT_HASH for seq 0)."""

    receipt_hash: str
    """SHA-256 of canonical JSON of {row_hash, parent_hash, sequence}."""

    model_config = ConfigDict(frozen=True)


class EvidenceChain(BaseModel):
    """Complete evidence chain for one sealed benchmark run."""

    run_id: str
    schema_version: str = "1"
    evidence_root: str
    """Hash of the last receipt (or GENESIS_PARENT_HASH if 0 rows)."""

    receipt_count: int
    receipts: list[EvidenceReceipt]

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Chain construction
# ---------------------------------------------------------------------------

def build_receipt_chain(rows: list[dict], run_id: str = "") -> EvidenceChain:
    """Build an append-only receipt chain from sealed result rows.

    Takes rows in sealed JSON array order (no re-sorting -- array order is
    deterministic because the runner loop is sequential).

    For each row:
    1. Compute row_hash = canonical_hash(deterministic_row(row))
    2. Build receipt_data = {row_hash, parent_hash, sequence}
    3. Compute receipt_hash = canonical_hash(receipt_data)
    4. Create EvidenceReceipt

    Returns EvidenceChain with evidence_root = last receipt_hash
    (or GENESIS_PARENT_HASH if 0 rows).
    """
    parent = GENESIS_PARENT_HASH
    receipts: list[EvidenceReceipt] = []

    for seq, row in enumerate(rows):
        row_hash = canonical_hash(deterministic_row(row))
        receipt_data = {
            "row_hash": row_hash,
            "parent_hash": parent,
            "sequence": seq,
        }
        receipt_hash = canonical_hash(receipt_data)
        receipt = EvidenceReceipt(
            sequence=seq,
            row_hash=row_hash,
            parent_hash=parent,
            receipt_hash=receipt_hash,
        )
        receipts.append(receipt)
        parent = receipt_hash

    evidence_root = parent  # last receipt_hash, or GENESIS if no rows

    return EvidenceChain(
        run_id=run_id,
        evidence_root=evidence_root,
        receipt_count=len(receipts),
        receipts=receipts,
    )


# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------

def verify_evidence_chain(rows: list[dict], receipts: list[dict]) -> str:
    """Replay evidence chain from rows and receipts.  Return evidence_root.

    Raises ValueError if:
    - Receipt count != row count
    - Any row_hash mismatch (tampered row data -- EVID-01)
    - Any receipt_hash mismatch (tampered receipt)
    """
    if len(rows) != len(receipts):
        raise ValueError(
            f"Receipt count ({len(receipts)}) != row count ({len(rows)})"
        )

    parent = GENESIS_PARENT_HASH
    for seq, (row, receipt) in enumerate(zip(rows, receipts)):
        # Recompute row hash from deterministic fields
        expected_row_hash = canonical_hash(deterministic_row(row))
        if expected_row_hash != receipt["row_hash"]:
            raise ValueError(
                f"Row {seq}: row_hash mismatch "
                f"(expected {expected_row_hash[:16]}..., "
                f"got {receipt['row_hash'][:16]}...)"
            )

        # Recompute receipt hash
        expected_receipt_data = {
            "row_hash": expected_row_hash,
            "parent_hash": parent,
            "sequence": seq,
        }
        expected_receipt_hash = canonical_hash(expected_receipt_data)
        if expected_receipt_hash != receipt["receipt_hash"]:
            raise ValueError(
                f"Row {seq}: receipt_hash mismatch "
                f"(expected {expected_receipt_hash[:16]}..., "
                f"got {receipt['receipt_hash'][:16]}...)"
            )

        parent = expected_receipt_hash

    return parent  # evidence_root


# ---------------------------------------------------------------------------
# Evidence file I/O
# ---------------------------------------------------------------------------

def write_evidence_file(
    chain: EvidenceChain,
    results_dir: pathlib.Path,
    run_id: str,
) -> pathlib.Path:
    """Write evidence chain to results/run-{run_id}.evidence.json.

    Uses orjson with OPT_INDENT_2 for human-readable output.
    Returns path to the written file.
    """
    evidence_path = results_dir / f"run-{run_id}.evidence.json"
    data = chain.model_dump(mode="json")
    evidence_path.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2))
    return evidence_path


# ---------------------------------------------------------------------------
# EVID-02: Model selection artifact
# ---------------------------------------------------------------------------

class ModelSelectionArtifact(BaseModel):
    """Frozen record of a model selection decision, traceable to evidence.

    The stable_decision_hash is SHA-256 of canonical JSON of all fields
    EXCEPT stable_decision_hash itself.  Changing any selection parameter
    (weights, winners, evidence root) produces a different decision hash.
    """

    run_id: str
    schema_version: str = "1"
    """Artifact schema version. Default "1" for backward compat; new runs emit "2"."""

    evidence_root: str
    quality_weight_default: float
    speed_weight_default: float
    quality_weight_routing: float
    speed_weight_routing: float
    min_quality_floor: float = 0.0
    """Minimum mean quality a model must clear to win a suite. Default 0.0 keeps
    pre-floor (schema <= "2") artifacts loadable; new runs emit MIN_QUALITY_FLOOR."""

    per_suite_winners: dict[str, str]
    """Suite name -> winning model name (quality-floored best composite per suite)."""

    pareto_frontier: list[str]
    """Sorted model names on the Pareto frontier (quality vs decode TPS)."""

    stable_decision_hash: str
    """SHA-256 of canonical JSON of all other fields."""

    model_config = ConfigDict(frozen=True)


def build_selection_artifact(
    run_id: str,
    evidence_root: str,
    stats: list,
    *,
    schema_version: str = "3",
) -> ModelSelectionArtifact:
    """Build a frozen selection artifact with deterministic decision hash.

    Args:
        run_id: Sealed run identifier.
        evidence_root: Evidence chain root hash from build_receipt_chain.
        stats: List of ModelSuiteStats from aggregate_trials (with composites).
        schema_version: Artifact schema version string (default "2" for new runs).

    Returns:
        ModelSelectionArtifact with stable_decision_hash derived from all
        other fields via canonical_hash.
    """
    from ollarma.reporter import (
        QUALITY_WEIGHT_DEFAULT,
        SPEED_WEIGHT_DEFAULT,
        QUALITY_WEIGHT_ROUTING,
        SPEED_WEIGHT_ROUTING,
        MIN_QUALITY_FLOOR,
        _compute_composites,
        select_suite_winner,
        pareto_frontier_2d,
    )

    # Composite scores must be populated before ranking: aggregate_trials leaves
    # composite_score=0.0, so without this the artifact winner would be
    # order-dependent (first qualified) instead of the best-composite model.
    # render_*_md already recompute internally; the artifact must agree.
    stats = _compute_composites(stats)

    # Compute per-suite winners: group by suite, pick the quality-floored winner
    # (a fast-but-zero-quality model must never bind the runtime tier).
    suite_groups: dict[str, list] = defaultdict(list)
    for s in stats:
        suite_groups[s.suite].append(s)

    per_suite_winners: dict[str, str] = {}
    for suite in sorted(suite_groups):
        winner = select_suite_winner(suite_groups[suite])
        if winner is not None:
            per_suite_winners[suite] = winner.model

    # Compute Pareto frontier model names (sorted alphabetically)
    scorable_all = [s for s in stats if s.quality_mean is not None]
    pareto_models: list[str] = []
    if scorable_all:
        points = [(s.quality_mean, s.decode_tps_mean) for s in scorable_all]
        indices = pareto_frontier_2d(points)
        pareto_models = sorted({scorable_all[i].model for i in indices})

    # Build artifact data dict (everything except stable_decision_hash)
    artifact_data = {
        "run_id": run_id,
        "schema_version": schema_version,
        "evidence_root": evidence_root,
        "quality_weight_default": QUALITY_WEIGHT_DEFAULT,
        "speed_weight_default": SPEED_WEIGHT_DEFAULT,
        "quality_weight_routing": QUALITY_WEIGHT_ROUTING,
        "speed_weight_routing": SPEED_WEIGHT_ROUTING,
        "min_quality_floor": MIN_QUALITY_FLOOR,
        "per_suite_winners": per_suite_winners,
        "pareto_frontier": pareto_models,
    }
    stable_decision_hash = canonical_hash(artifact_data)

    return ModelSelectionArtifact(
        **artifact_data,
        stable_decision_hash=stable_decision_hash,
    )


def write_artifact_file(
    artifact: ModelSelectionArtifact,
    results_dir: pathlib.Path,
    run_id: str,
) -> pathlib.Path:
    """Write selection artifact to results/run-{run_id}.artifact.json.

    Uses orjson with OPT_INDENT_2 for human-readable output.
    Returns path to the written file.
    """
    artifact_path = results_dir / f"run-{run_id}.artifact.json"
    data = artifact.model_dump(mode="json")
    artifact_path.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2))
    return artifact_path
