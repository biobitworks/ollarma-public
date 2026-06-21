"""Model-role policy for RTB-02 antibody lanes.

This module is metadata only. It does not load models or run review lanes.
The policy records which models may fan out as tiny cell-type lanes and which
rungs must run sequentially on the 32 GB local host.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


AntibodyModelRole = Literal[
    "recall_sensor",
    "structured_cell_type",
    "router",
    "escalation_rung",
    "reasoning_rung",
    "ceiling_rung",
]
ModelResidency = Literal["pinned", "warm_if_room", "load_on_demand", "benchmark_only"]
StrictOutputMode = Literal["none", "json_mode", "json_grammar"]
ParallelGroup = Literal["tiny_cell_fanout", "sequential_only"]


class AntibodyModelPolicy(BaseModel):
    """How an antibody lane may use a local model."""

    role: AntibodyModelRole
    model: str
    residency: ModelResidency
    strict_output: StrictOutputMode
    max_parallel_group: ParallelGroup = "sequential_only"
    confidence_floor: float | None = None

    model_config = ConfigDict(frozen=True)


class AntibodyModelPolicyPlan(BaseModel):
    """Resolved model-role plan split by safe fan-out and sequential rungs."""

    tiny_cell_fanout: tuple[AntibodyModelPolicy, ...] = ()
    sequential_rungs: tuple[AntibodyModelPolicy, ...] = ()

    model_config = ConfigDict(frozen=True)


def validate_antibody_model_policy(policy: AntibodyModelPolicy) -> None:
    """Fail closed on policy combinations that cannot carry trusted verdicts."""

    if policy.role == "router" and policy.strict_output != "json_grammar":
        raise ValueError("ROUTER_REQUIRES_JSON_GRAMMAR")
    if policy.role in {"structured_cell_type", "escalation_rung", "reasoning_rung", "ceiling_rung"}:
        if policy.strict_output == "none":
            raise ValueError("VERDICT_LANE_REQUIRES_STRUCTURED_OUTPUT")
    if policy.max_parallel_group == "tiny_cell_fanout" and policy.role not in {
        "recall_sensor",
        "structured_cell_type",
    }:
        raise ValueError("ONLY_TINY_CELL_ROLES_CAN_FAN_OUT")
    if policy.role in {"router", "escalation_rung", "reasoning_rung", "ceiling_rung"}:
        if policy.max_parallel_group != "sequential_only":
            raise ValueError("BIG_MODEL_RUNG_MUST_BE_SEQUENTIAL")


def resolve_antibody_model_policy(
    policies: tuple[AntibodyModelPolicy, ...],
) -> AntibodyModelPolicyPlan:
    """Validate policies and split tiny fan-out lanes from sequential rungs."""

    tiny: list[AntibodyModelPolicy] = []
    sequential: list[AntibodyModelPolicy] = []
    for policy in policies:
        validate_antibody_model_policy(policy)
        if policy.max_parallel_group == "tiny_cell_fanout":
            tiny.append(policy)
        else:
            sequential.append(policy)
    return AntibodyModelPolicyPlan(
        tiny_cell_fanout=tuple(tiny),
        sequential_rungs=tuple(sequential),
    )
