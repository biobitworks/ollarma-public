"""test_schemas.py -- Round-trip + bounds + frozen tests for swarm schemas.

Covers PROMPT-OLLARMA-SWARM-001 T2 acceptance: pydantic v2 frozen models with
schema_version=1, bounded fields, and stance Literal enforcement.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ollarma.swarm.schemas import (
    DissentCluster,
    RoundArtifact,
    SimulationSummary,
    StanceResponse,
)


# ---------------------------------------------------------------------------
# StanceResponse
# ---------------------------------------------------------------------------

def _make_stance(**overrides) -> StanceResponse:
    base = dict(
        persona_id="analyst_00",
        round_idx=0,
        stance="agree",
        confidence=0.7,
        rationale="Evidence supports the framing.",
        post="I find this position reasonable given the evidence presented.",
    )
    base.update(overrides)
    return StanceResponse(**base)


def test_stance_response_round_trip():
    original = _make_stance()
    dumped = original.model_dump()
    restored = StanceResponse.model_validate(dumped)
    assert restored == original
    assert restored.schema_version == 1


def test_stance_response_round_trip_via_json_mode():
    original = _make_stance()
    dumped = original.model_dump(mode="json")
    restored = StanceResponse.model_validate(dumped)
    assert restored == original


def test_stance_response_rejects_invalid_stance():
    with pytest.raises(ValidationError):
        _make_stance(stance="maybe")


def test_stance_response_accepts_all_six_stances():
    for stance in (
        "strongly_disagree",
        "disagree",
        "neutral",
        "agree",
        "strongly_agree",
        "refuse_to_engage",
    ):
        sr = _make_stance(stance=stance)
        assert sr.stance == stance


def test_stance_response_rejects_out_of_bounds_confidence():
    with pytest.raises(ValidationError):
        _make_stance(confidence=-0.1)
    with pytest.raises(ValidationError):
        _make_stance(confidence=1.1)


def test_stance_response_accepts_boundary_confidence():
    assert _make_stance(confidence=0.0).confidence == 0.0
    assert _make_stance(confidence=1.0).confidence == 1.0


def test_stance_response_post_length_cap():
    # 281 chars must fail; 280 must pass.
    with pytest.raises(ValidationError):
        _make_stance(post="x" * 281)
    assert _make_stance(post="x" * 280).post == "x" * 280


def test_stance_response_rationale_length_cap():
    with pytest.raises(ValidationError):
        _make_stance(rationale="x" * 241)
    assert _make_stance(rationale="x" * 240).rationale == "x" * 240


def test_stance_response_rejects_negative_round_idx():
    with pytest.raises(ValidationError):
        _make_stance(round_idx=-1)


def test_stance_response_is_frozen():
    sr = _make_stance()
    with pytest.raises(ValidationError):
        sr.confidence = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DissentCluster
# ---------------------------------------------------------------------------

def test_dissent_cluster_round_trip():
    original = DissentCluster(
        cluster_id=0,
        stance="strongly_disagree",
        sample_post="This is exactly the kind of overreach we warned about.",
        n_members=7,
    )
    restored = DissentCluster.model_validate(original.model_dump())
    assert restored == original


def test_dissent_cluster_rejects_invalid_stance():
    with pytest.raises(ValidationError):
        DissentCluster(
            cluster_id=0,
            stance="ambivalent",
            sample_post="x",
            n_members=1,
        )


# ---------------------------------------------------------------------------
# RoundArtifact
# ---------------------------------------------------------------------------

def _make_round_artifact(**overrides) -> RoundArtifact:
    base = dict(
        run_id="run_test_001",
        round_idx=0,
        stance_distribution={
            "strongly_disagree": 0.1,
            "disagree": 0.2,
            "neutral": 0.3,
            "agree": 0.2,
            "strongly_agree": 0.1,
            "refuse_to_engage": 0.1,
        },
        dissent_clusters=[
            DissentCluster(
                cluster_id=0,
                stance="strongly_disagree",
                sample_post="Strong objection.",
                n_members=5,
            ),
        ],
        sample_posts=["post a", "post b", "post c"],
        quarantined_count=2,
        n_personas=50,
    )
    base.update(overrides)
    return RoundArtifact(**base)


def test_round_artifact_round_trip():
    original = _make_round_artifact()
    restored = RoundArtifact.model_validate(original.model_dump())
    assert restored == original
    assert restored.schema_version == 1


def test_round_artifact_round_trip_json_mode():
    original = _make_round_artifact()
    dumped = original.model_dump(mode="json")
    restored = RoundArtifact.model_validate(dumped)
    assert restored == original


def test_round_artifact_sample_posts_cap_at_5():
    with pytest.raises(ValidationError):
        _make_round_artifact(sample_posts=["a", "b", "c", "d", "e", "f"])


def test_round_artifact_rejects_negative_counts():
    with pytest.raises(ValidationError):
        _make_round_artifact(quarantined_count=-1)
    with pytest.raises(ValidationError):
        _make_round_artifact(n_personas=-1)
    with pytest.raises(ValidationError):
        _make_round_artifact(round_idx=-1)


def test_round_artifact_is_frozen():
    ra = _make_round_artifact()
    with pytest.raises(ValidationError):
        ra.quarantined_count = 0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SimulationSummary
# ---------------------------------------------------------------------------

def _make_summary(**overrides) -> SimulationSummary:
    rounds = [
        _make_round_artifact(round_idx=r) for r in range(3)
    ]
    base = dict(
        run_id="run_test_001",
        scenario_id="scenario_press_release",
        n_personas=50,
        n_rounds=3,
        rounds=rounds,
        jsd_round_over_round=[0.0, 0.07, 0.04],
        wall_seconds=1234.5,
        quarantine_rate=0.02,
    )
    base.update(overrides)
    return SimulationSummary(**base)


def test_simulation_summary_round_trip():
    original = _make_summary()
    restored = SimulationSummary.model_validate(original.model_dump())
    assert restored == original
    assert restored.schema_version == 1


def test_simulation_summary_round_trip_json_mode():
    original = _make_summary()
    restored = SimulationSummary.model_validate(original.model_dump(mode="json"))
    assert restored == original


def test_simulation_summary_quarantine_rate_bounds():
    with pytest.raises(ValidationError):
        _make_summary(quarantine_rate=-0.01)
    with pytest.raises(ValidationError):
        _make_summary(quarantine_rate=1.01)


def test_simulation_summary_wall_seconds_non_negative():
    with pytest.raises(ValidationError):
        _make_summary(wall_seconds=-1.0)


def test_simulation_summary_is_frozen():
    summary = _make_summary()
    with pytest.raises(ValidationError):
        summary.wall_seconds = 0.0  # type: ignore[misc]
