"""CAFA-6 protein-function verification substrate (Ollarma lane).

Local-first deterministic guardrails + dataset registry. See
``docs/LOCAL_VS_ONLINE_BOUNDARY.md`` and ``prompts/PROMPT_CAFA_OLLARMA_LANE.md``.
"""
from .go_ontology import GoDag, GoTerm, NAMESPACE_ROOTS
from .go_guardrails import (
    Prediction,
    Verdict,
    go_score_range,
    go_term_validity,
    go_term_cap,
    subontology_partition,
    go_propagation_consistency,
    propagate_to_root,
    run_all,
)

from .cascade import (
    verify_go_submission,
    screen_text_claim,
    text_format_contract,
    pmid_citation_present,
    GoVerificationResult,
    TextFloorResult,
)
from .ml_antibodies import ClaimEntailmentAntibody, EntailmentVerdict

__all__ = [
    "GoDag", "GoTerm", "NAMESPACE_ROOTS",
    "Prediction", "Verdict",
    "go_score_range", "go_term_validity", "go_term_cap",
    "subontology_partition", "go_propagation_consistency",
    "propagate_to_root", "run_all",
    "verify_go_submission", "screen_text_claim",
    "text_format_contract", "pmid_citation_present",
    "GoVerificationResult", "TextFloorResult",
    "ClaimEntailmentAntibody", "EntailmentVerdict",
]
