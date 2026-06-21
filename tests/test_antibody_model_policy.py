"""RTB-02 antibody model-role policy tests."""
from __future__ import annotations

import pytest

from ollarma.antibody_model_policy import (
    AntibodyModelPolicy,
    resolve_antibody_model_policy,
    validate_antibody_model_policy,
)


def test_tiny_cell_fanout_and_sequential_rungs_are_split() -> None:
    recall = AntibodyModelPolicy(
        role="recall_sensor",
        model="qwen2.5:1.5b",
        residency="pinned",
        strict_output="none",
        max_parallel_group="tiny_cell_fanout",
    )
    structured = AntibodyModelPolicy(
        role="structured_cell_type",
        model="qwen3.5:2b",
        residency="pinned",
        strict_output="json_mode",
        max_parallel_group="tiny_cell_fanout",
    )
    escalation = AntibodyModelPolicy(
        role="escalation_rung",
        model="qwen3.5:9b",
        residency="warm_if_room",
        strict_output="json_mode",
        max_parallel_group="sequential_only",
    )

    plan = resolve_antibody_model_policy((recall, structured, escalation))

    assert [item.model for item in plan.tiny_cell_fanout] == ["qwen2.5:1.5b", "qwen3.5:2b"]
    assert [item.model for item in plan.sequential_rungs] == ["qwen3.5:9b"]


def test_router_requires_json_grammar() -> None:
    router = AntibodyModelPolicy(
        role="router",
        model="granite4.1:8b",
        residency="warm_if_room",
        strict_output="json_mode",
        max_parallel_group="sequential_only",
    )

    with pytest.raises(ValueError, match="ROUTER_REQUIRES_JSON_GRAMMAR"):
        validate_antibody_model_policy(router)


def test_verdict_lanes_require_structured_output() -> None:
    lane = AntibodyModelPolicy(
        role="structured_cell_type",
        model="qwen3.5:2b",
        residency="pinned",
        strict_output="none",
        max_parallel_group="tiny_cell_fanout",
    )

    with pytest.raises(ValueError, match="VERDICT_LANE_REQUIRES_STRUCTURED_OUTPUT"):
        validate_antibody_model_policy(lane)


def test_big_model_rungs_cannot_fan_out() -> None:
    rung = AntibodyModelPolicy(
        role="reasoning_rung",
        model="phi4-reasoning:14b",
        residency="load_on_demand",
        strict_output="json_mode",
        max_parallel_group="tiny_cell_fanout",
    )

    with pytest.raises(ValueError, match="ONLY_TINY_CELL_ROLES_CAN_FAN_OUT"):
        validate_antibody_model_policy(rung)
