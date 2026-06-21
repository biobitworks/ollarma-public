"""Deterministic GO-prediction guardrails — the rule_floor antibodies.

Pure logic over a local :class:`GoDag`. No model, no network, fully reproducible.
These implement the CAFA submission contract as fail-closed checks that run as the
cheapest cascade rung; reusable for any ontology-labeled prediction task
(xenodisorder DisProt/CAID, deltaprot, ...).

Each antibody returns a :class:`Verdict` with status ``pass`` / ``flag`` / ``block``
and the offending items, so a verdict-provenance receipt (RTB-02) can be built.

Contract (CAFA-6): score in (0, 1.0] with <=3 sig figs; <=1500 terms per protein
across MF+BP+CC combined; terms must be valid GO ids in the pinned version;
predictions must propagate to root (parent score >= max child score); tab-separated
rows, no header.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .go_ontology import GoDag

TERM_CAP = 1500  # CAFA-6: per-protein combined MF+BP+CC cap


@dataclass(frozen=True)
class Prediction:
    protein_id: str
    go_id: str
    score: float


@dataclass
class Verdict:
    antibody: str
    status: str  # "pass" | "flag" | "block"
    detail: str = ""
    offenders: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "pass"


def _round3sig(x: float) -> float:
    if x == 0:
        return 0.0
    from math import floor, log10
    return round(x, -int(floor(log10(abs(x)))) + 2)


# --- antibodies -----------------------------------------------------------

def go_score_range(preds: list[Prediction]) -> Verdict:
    """score in (0, 1.0]; <=3 significant figures."""
    bad = []
    for p in preds:
        if not (0.0 < p.score <= 1.0):
            bad.append((p.protein_id, p.go_id, p.score, "out_of_range"))
        elif abs(p.score - _round3sig(p.score)) > 1e-9:
            bad.append((p.protein_id, p.go_id, p.score, "too_many_sigfigs"))
    return Verdict("go_score_range", "block" if bad else "pass",
                   f"{len(bad)} score violation(s)", bad[:50])


def go_term_validity(preds: list[Prediction], dag: GoDag, *,
                     allow_obsolete: bool = False) -> Verdict:
    """Every GO id exists in the pinned ontology (alt_ids normalized; obsolete rejected)."""
    bad = [(p.protein_id, p.go_id) for p in preds
           if not dag.is_valid(p.go_id, allow_obsolete=allow_obsolete)]
    # invalid terms are *excluded* by the evaluator, so this is a flag (drop), not a hard block
    return Verdict("go_term_validity", "flag" if bad else "pass",
                   f"{len(bad)} invalid/obsolete term(s) (will be excluded)", bad[:50])


def go_term_cap(preds: list[Prediction], cap: int = TERM_CAP) -> Verdict:
    """<=cap terms per protein (combined subontologies)."""
    counts: dict[str, int] = {}
    for p in preds:
        counts[p.protein_id] = counts.get(p.protein_id, 0) + 1
    over = [(pid, n) for pid, n in counts.items() if n > cap]
    return Verdict("go_term_cap", "block" if over else "pass",
                   f"{len(over)} protein(s) over the {cap}-term cap", over[:50])


def subontology_partition(preds: list[Prediction], dag: GoDag) -> Verdict:
    """Each predicted term resolves to a known subontology (MF/BP/CC)."""
    bad = [(p.protein_id, p.go_id) for p in preds if dag.subontology_of(p.go_id) is None]
    return Verdict("subontology_partition", "flag" if bad else "pass",
                   f"{len(bad)} term(s) with unknown subontology", bad[:50])


def go_propagation_consistency(preds: list[Prediction], dag: GoDag) -> Verdict:
    """For each protein, every ancestor must be present with score >= the term's score.

    CAFA propagates predictions to the root (parent = max child score). A submission
    that is *not* internally consistent (an ancestor missing or scored below a
    descendant) will be silently re-propagated by the evaluator — surfacing it here
    lets the operator fix it deterministically before submitting.
    """
    by_protein: dict[str, dict[str, float]] = {}
    for p in preds:
        primary = dag.normalize(p.go_id)
        if primary is None:
            continue
        d = by_protein.setdefault(p.protein_id, {})
        d[primary] = max(d.get(primary, 0.0), p.score)

    violations = []
    for pid, scores in by_protein.items():
        for term, sc in list(scores.items()):
            for anc in dag.ancestors(term):
                if anc in NAMESPACE_ROOTS_SET:
                    continue  # roots carry weight 0; not required in the file
                anc_score = scores.get(anc)
                if anc_score is None or anc_score < sc - 1e-9:
                    violations.append((pid, term, anc, sc, anc_score))
    return Verdict("go_propagation_consistency", "flag" if violations else "pass",
                   f"{len(violations)} parent<child or missing-ancestor violation(s)",
                   violations[:50])


def propagate_to_root(preds: list[Prediction], dag: GoDag) -> list[Prediction]:
    """Deterministically fix propagation: add/raise each ancestor to max child score.

    Returns a new, propagation-consistent prediction list (excludes roots, which
    carry weight 0). Idempotent.
    """
    by_protein: dict[str, dict[str, float]] = {}
    for p in preds:
        primary = dag.normalize(p.go_id)
        if primary is None:
            continue
        d = by_protein.setdefault(p.protein_id, {})
        d[primary] = max(d.get(primary, 0.0), p.score)

    out: list[Prediction] = []
    for pid, scores in by_protein.items():
        propagated = dict(scores)
        for term, sc in scores.items():
            for anc in dag.ancestors(term):
                if anc in NAMESPACE_ROOTS_SET:
                    continue
                if propagated.get(anc, 0.0) < sc:
                    propagated[anc] = sc
        for term, sc in propagated.items():
            out.append(Prediction(pid, term, sc))
    return out


NAMESPACE_ROOTS_SET = frozenset({"GO:0008150", "GO:0003674", "GO:0005575"})


def run_all(preds: list[Prediction], dag: GoDag) -> list[Verdict]:
    """Run the full deterministic antibody panel. Order = cheapest first."""
    return [
        go_score_range(preds),
        go_term_cap(preds),
        go_term_validity(preds, dag),
        subontology_partition(preds, dag),
        go_propagation_consistency(preds, dag),
    ]
