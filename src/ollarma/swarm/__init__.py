"""ollarma.swarm -- Predictive deliberative persona swarm engine.

Sister to (but distinct from) Antigence's adjudicative swarm. See
PROMPT-OLLARMA-SWARM-001 (prompts/PROMPT_OLLARMA_SWARM_001_LOCAL_GPU.md)
for the pre-registered H0/MESI specification.

Phase 70 Wave 1 (commit e2b5b1f): schemas + persona_bank.
Phase 70 Wave 2 (this commit): engine + aggregator + throttle.
Phase 70 Wave 3 (70-03): tests for the runtime.
"""
from ollarma.swarm.engine import run_simulation
from ollarma.swarm.persona_bank import PersonaBank, PersonaPrompt
from ollarma.swarm.schemas import (
    DissentCluster,
    RoundArtifact,
    SimulationSummary,
    StanceResponse,
)

__all__ = [
    "DissentCluster",
    "PersonaBank",
    "PersonaPrompt",
    "RoundArtifact",
    "SimulationSummary",
    "StanceResponse",
    "run_simulation",
]
