"""D4 — cascade orchestration for protein-function claim verification.

Encodes the local-first routing policy (``docs/LOCAL_VS_ONLINE_BOUNDARY.md``):

- **GO-term submissions** verify on the deterministic rule_floor ONLY — no model,
  no network, ever. ``verify_go_submission`` runs the antibody panel, deterministically
  propagates to root, and emits hash-chained receipts. This whole path is zone ①.

- **Free-text claims** run the local deterministic checks first (format, citation
  present), then — only if they pass the floor and need semantic judgement —
  produce an **escalation request to the Antigence local-antibody lane** (one rung
  per step). Frontier/online is NOT reached here; it is the Antigence lane's
  ``absolutely_necessary`` last resort. The escalation request IS the bridge handoff.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .go_ontology import GoDag
from .go_guardrails import Prediction, Verdict, run_all, propagate_to_root
from .receipts import AntibodyReceipt, build_receipts

_PMID_RE = re.compile(r"\bPMID:?\s*\d{4,9}\b", re.I)


@dataclass
class GoVerificationResult:
    target: str
    status: str                       # "clean" | "blocked" | "fixed"
    verdicts: list[Verdict]
    receipts: list[AntibodyReceipt]
    propagated: list[Prediction]
    network_calls: int = 0            # always 0 — deterministic local path
    frontier_calls: int = 0           # always 0


def verify_go_submission(target: str, preds: list[Prediction], dag: GoDag,
                         *, auto_propagate: bool = True) -> GoVerificationResult:
    """End-to-end deterministic GO verification. Zero model / network / frontier."""
    working = propagate_to_root(preds, dag) if auto_propagate else list(preds)
    verdicts = run_all(working, dag)
    receipts = build_receipts(target=target, preds=working, verdicts=verdicts)
    if any(v.status == "block" for v in verdicts):
        status = "blocked"
    elif auto_propagate and len(working) != len(preds):
        status = "fixed"             # propagation added ancestor rows
    else:
        status = "clean"
    return GoVerificationResult(target, status, verdicts, receipts, working)


# --- free-text path -------------------------------------------------------

@dataclass
class TextFloorResult:
    target: str
    floor_verdicts: list[Verdict]
    needs_escalation: bool
    escalation_request: dict | None = field(default=None)
    network_calls: int = 0
    frontier_calls: int = 0


def text_format_contract(text: str) -> Verdict:
    """ASCII 33-126 + space, no tab, <=3000 chars per protein (CAFA free-text)."""
    bad = []
    if "\t" in text:
        bad.append("contains_tab")
    if len(text) > 3000:
        bad.append("over_3000_chars")
    if any(not (c == " " or 33 <= ord(c) <= 126) for c in text):
        bad.append("non_ascii_printable")
    return Verdict("text_format_contract", "block" if bad else "pass",
                   ", ".join(bad), bad)


def pmid_citation_present(text: str) -> Verdict:
    """Cheap recall-floor: is there at least one PMID-shaped citation?"""
    has = bool(_PMID_RE.search(text))
    return Verdict("pmid_citation_present", "pass" if has else "flag",
                   "no PMID citation found" if not has else "", [])


def screen_text_claim(target: str, text: str) -> TextFloorResult:
    """Run the LOCAL deterministic floor on a free-text claim, then decide.

    If the floor blocks (format) -> stop, no escalation. Otherwise emit an
    escalation REQUEST for the Antigence local-antibody lane (claim_entailment /
    open_world_overclaim). This is a bridge handoff, NOT a model call here, and
    NEVER a frontier call.
    """
    floor = [text_format_contract(text), pmid_citation_present(text)]
    blocked = any(v.status == "block" for v in floor)
    needs = not blocked  # semantic judgement is the Antigence lane's job
    req = None
    if needs:
        pmids = _PMID_RE.findall(text)
        req = {
            "to_lane": "antigence",
            "antibodies": ["claim_entailment", "open_world_overclaim"],
            "target": target,
            "text": text,
            "cited_pmids": pmids,
            "next_rung": "structured_verifier",   # one rung per step; local model
            "frontier_allowed": False,            # absolutely_necessary gate is downstream
            "reason": "deterministic floor passed; semantic entailment/overclaim "
                      "needs a local model judge",
        }
    return TextFloorResult(target, floor, needs, req)
