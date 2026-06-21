"""ollarma.swarm.lane -- Execution-lane swarm runtime (Phase 66+).

Sister to (but distinct from) ``ollarma.swarm`` (Phase 70 predictive
deliberative swarm). The execution lane is a bounded backup runtime for
local planner -> executor -> reviewer -> synthesizer chains; the predictive
swarm is an audience-reaction forecaster.

Phase 66 plan 01 (this commit): typed records + storage primitive +
shared runtime contract. No orchestrator, no lease manager, no Ollama
calls -- just the contracts every later plan in this phase depends on.

Plan 66-02 (this commit) adds the runtime trio:
- :class:`LeaseManager` -- atomic lease ownership over LaneStore SQLite.
- :class:`LeaseDecision` -- structured outcome of an acquire call.
- :func:`run_swarm_lane` -- the public sequential orchestrator.

Plan 66-03 will land integration tests + the first proof run.
"""
from ollarma.swarm.lane.lease import LeaseDecision, LeaseManager
from ollarma.swarm.lane.routing import select_lane_model
from ollarma.swarm.lane.runtime import (
    TokenExhaustedError,
    resume_swarm_lane,
    run_swarm_lane,
)
from ollarma.swarm.lane.schemas import (
    Checkpoint,
    ExecutorOutput,
    LaneOutcome,
    LaneOutputBase,
    LaneRoleLadder,
    LaneRun,
    LaneTransition,
    PlannerOutput,
    ResumeCandidate,
    ReviewerOutput,
    ROLE_CHAIN,
    Role,
    RunStatus,
    SwarmRun,
    SynthesizerOutput,
)
from ollarma.swarm.lane.store import LaneStore, LaneStoreError
from ollarma.swarm.lane.swap import live_swap_percent_provider

__all__ = [
    "Checkpoint",
    "ExecutorOutput",
    "LaneOutcome",
    "LaneOutputBase",
    "LaneRoleLadder",
    "LaneRun",
    "LaneStore",
    "LaneStoreError",
    "LaneTransition",
    "LeaseDecision",
    "LeaseManager",
    "PlannerOutput",
    "ROLE_CHAIN",
    "ResumeCandidate",
    "ReviewerOutput",
    "Role",
    "RunStatus",
    "SwarmRun",
    "SynthesizerOutput",
    "TokenExhaustedError",
    "live_swap_percent_provider",
    "resume_swarm_lane",
    "run_swarm_lane",
    "select_lane_model",
]
