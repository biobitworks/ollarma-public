"""schemas.py -- Frozen pydantic v2 models for the persona swarm runtime.

All models are immutable (`frozen=True`) and carry an explicit `schema_version`
field so future schema migrations can be detected by downstream consumers
(vitaology Phase 7 audience-reaction sidecar, etc.).

Bounds rationale (from PROMPT-OLLARMA-SWARM-001):

- ``stance``: exactly the 6 Literal values from PROMPT lines 122-130.
- ``confidence``: float in [0.0, 1.0].
- ``rationale``: <=240 chars (~30 words at 8 chars/word; the PROMPT specifies
  a word bound, encoded here as a char bound because pydantic does not natively
  count words).
- ``post``: <=280 chars (twitter-like; PROMPT line 129).

All hashing of these models flows through ``ollarma.evidence.canonical_hash``;
no new hashing primitive is introduced here.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Stance vocabulary -- pre-registered, do not extend without a new PROMPT.
# ---------------------------------------------------------------------------

Stance = Literal[
    "strongly_disagree",
    "disagree",
    "neutral",
    "agree",
    "strongly_agree",
    "refuse_to_engage",
]
"""The 6 stance values from PROMPT-OLLARMA-SWARM-001 lines 122-130."""


# ---------------------------------------------------------------------------
# Per-persona, per-round response.
# ---------------------------------------------------------------------------

class StanceResponse(BaseModel):
    """One persona's response in one round.

    Pre-registered schema from PROMPT-OLLARMA-SWARM-001. Persisted under
    ``runs/<run_id>/round_<r>/<persona_id>.json`` by the engine (Wave 2).
    """

    model_config = ConfigDict(frozen=True)

    persona_id: str
    round_idx: int = Field(ge=0)
    stance: Stance
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=240)
    post: str = Field(max_length=280)
    schema_version: int = 1


# ---------------------------------------------------------------------------
# Per-round aggregate.
# ---------------------------------------------------------------------------

class DissentCluster(BaseModel):
    """One dissent cluster surfaced by the aggregator (Wave 2).

    Lightweight container; kept here so RoundArtifact stays strictly typed
    rather than carrying ``list[dict]``.
    """

    model_config = ConfigDict(frozen=True)

    cluster_id: int = Field(ge=0)
    stance: Stance
    sample_post: str = Field(max_length=280)
    n_members: int = Field(ge=0)


class RoundArtifact(BaseModel):
    """Aggregate state for one round across all personas.

    Persisted to ``runs/<run_id>/round_<r>.json`` after the round finishes.
    The engine writes per-persona StanceResponse files first; the aggregator
    collapses them into this artifact.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    round_idx: int = Field(ge=0)
    stance_distribution: dict[str, float]
    """stance -> proportion in [0, 1]; keys must be valid Stance values."""

    dissent_clusters: list[DissentCluster]
    sample_posts: list[str] = Field(max_length=5)
    """<=5 representative posts (engine-selected; aggregator-defined order)."""

    quarantined_count: int = Field(ge=0)
    n_personas: int = Field(ge=0)
    schema_version: int = 1


# ---------------------------------------------------------------------------
# Whole-simulation summary.
# ---------------------------------------------------------------------------

class SimulationSummary(BaseModel):
    """Final summary across all M rounds; one per simulation.

    Persisted to ``runs/<run_id>/summary.json`` at the end of
    ``swarm.run_simulation(...)``. ``jsd_round_over_round[r]`` is the
    Jensen-Shannon divergence between rounds (r-1) and r; index 0 is fixed
    at 0.0 by convention so the list length equals ``n_rounds``.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    scenario_id: str
    n_personas: int = Field(ge=0)
    n_rounds: int = Field(ge=0)
    rounds: list[RoundArtifact]
    jsd_round_over_round: list[float]
    wall_seconds: float = Field(ge=0.0)
    quarantine_rate: float = Field(ge=0.0, le=1.0)
    schema_version: int = 1
