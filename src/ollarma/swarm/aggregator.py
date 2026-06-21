"""aggregator.py -- Round-level stance aggregation, JSD, and dissent clusters.

Implements PROMPT-OLLARMA-SWARM-001 task T5. Pure functions, stdlib-only math
(``math.log2`` + ``collections.Counter``); no numpy, scipy, or sklearn. The
dependency surface is kept flat on purpose so the aggregator can be reasoned
about and unit-tested in isolation (Wave 3 plan 70-03).

The dissent clusterer is deliberately cheap: v1 groups by stance literal and
exposes a stub hook ``_ngram_subcluster(posts)`` that currently returns the
whole list as one cluster. Later plans can swap in a real n-gram clustering
algorithm without touching the engine.
"""
from __future__ import annotations

from collections import Counter
from math import log2
from typing import Final, get_args

from ollarma.swarm.schemas import DissentCluster, Stance, StanceResponse


# ---------------------------------------------------------------------------
# Stance vocabulary -- pulled directly from the Literal alias so this stays
# in lock-step with the schema. If the PROMPT ever extends the stance set
# (which would require a new PROMPT registration), this list updates for free.
# ---------------------------------------------------------------------------

STANCE_VALUES: Final[tuple[str, ...]] = tuple(get_args(Stance))
"""The 6 stance values from PROMPT-OLLARMA-SWARM-001 lines 122-130."""


# ---------------------------------------------------------------------------
# Stance distribution
# ---------------------------------------------------------------------------

def stance_distribution(responses: list[StanceResponse]) -> dict[str, float]:
    """Proportion of each stance value over a list of (non-quarantined) responses.

    Args:
        responses: Per-persona StanceResponse objects for one round, with
            quarantined responses already filtered out by the engine.

    Returns:
        A dict whose keys are exactly the 6 ``STANCE_VALUES`` and whose values
        sum to 1.0 (within ``1e-9``) when ``responses`` is non-empty. When
        ``responses`` is empty, every value is ``0.0`` (no NaN, no division
        by zero -- this lets RoundArtifact serialize cleanly even if every
        response in a round is quarantined).
    """
    # Always emit all 6 keys so downstream JSD can assume a fixed support.
    distribution: dict[str, float] = {stance: 0.0 for stance in STANCE_VALUES}
    if not responses:
        return distribution

    counts = Counter(r.stance for r in responses)
    total = sum(counts.values())  # equals len(responses) by construction
    for stance, n in counts.items():
        distribution[stance] = n / total
    return distribution


# ---------------------------------------------------------------------------
# Jensen-Shannon divergence
# ---------------------------------------------------------------------------

def jensen_shannon_divergence(
    p: dict[str, float],
    q: dict[str, float],
) -> float:
    """Symmetric Jensen-Shannon divergence over the 6 stance values, log base 2.

    Defined as ``0.5 * KL(P || M) + 0.5 * KL(Q || M)`` with
    ``M = 0.5 * (P + Q)``. Using base-2 logarithms bounds the result in
    ``[0.0, 1.0]`` for any two distributions over the same support, which is
    what PROMPT-OLLARMA-SWARM-001 H0 (``JSD > 0.05`` detectability) and MESI
    gate 5 (``JSD < 0.02`` falsification on inert control) implicitly assume.

    KL convention: ``0 * log(0 / x) == 0`` (the limit holds for any finite
    ``x > 0``); zero-probability terms are skipped.

    Args:
        p, q: Stance distributions as returned by ``stance_distribution``.
            Must share the same key set; missing keys are treated as 0.0.

    Returns:
        JSD in ``[0.0, 1.0]``. Returns 0.0 when both distributions have
        zero mass (e.g., both rounds had every response quarantined).
    """
    # Build the union of supports. In practice both inputs come from
    # ``stance_distribution`` and already have the full STANCE_VALUES key set,
    # but we don't enforce that -- treating missing keys as 0.0 keeps the
    # function total.
    keys = set(p) | set(q)

    # Mixture distribution M = 0.5 * (P + Q).
    m = {k: 0.5 * (p.get(k, 0.0) + q.get(k, 0.0)) for k in keys}

    # Both distributions empty (or both zero everywhere) -> no divergence.
    if all(v == 0.0 for v in m.values()):
        return 0.0

    def _kl(a: dict[str, float], b: dict[str, float]) -> float:
        """KL(a || b) over a shared support, base-2, skipping zero-a terms."""
        total = 0.0
        for k in keys:
            ak = a.get(k, 0.0)
            if ak == 0.0:
                # 0 * log(0 / b) = 0 by KL convention.
                continue
            bk = b.get(k, 0.0)
            # bk == 0 here would imply m[k] == 0 too (since m[k] = 0.5*(a+b)),
            # but ak > 0 forces m[k] > 0, so this branch is unreachable when
            # b == m. Guard anyway in case a caller passes a non-mixture b.
            if bk == 0.0:
                # KL is formally infinite here; in our usage this never fires.
                return float("inf")
            total += ak * log2(ak / bk)
        return total

    jsd = 0.5 * _kl(p, m) + 0.5 * _kl(q, m)

    # Numerical floor: tiny negative values from float rounding -> 0.0.
    if jsd < 0.0 and jsd > -1e-12:
        return 0.0
    # Numerical ceiling: log2-based JSD is bounded by 1.0; clamp drift.
    if jsd > 1.0 and jsd < 1.0 + 1e-12:
        return 1.0
    return jsd


# ---------------------------------------------------------------------------
# Dissent clusters
# ---------------------------------------------------------------------------

def _ngram_subcluster(posts: list[str]) -> list[list[str]]:
    """Stub hook for n-gram-based subclustering inside a stance bucket.

    v1 returns the whole bucket as a single subcluster -- the dissent
    dimension that matters for H0 is stance, and stance is already split.
    Later plans can swap in a real n-gram overlap algorithm without touching
    ``dissent_clusters`` or the engine.

    Returns a list of subclusters; each subcluster is a list of post strings.
    """
    if not posts:
        return []
    return [list(posts)]


def dissent_clusters(
    responses: list[StanceResponse],
    top_k: int = 5,
) -> list[DissentCluster]:
    """Group responses by stance and emit up to ``top_k`` dissent clusters.

    For each stance bucket, picks the highest-confidence response as the
    cluster's ``sample_post``. Returns clusters sorted by descending
    ``n_members`` (with ``cluster_id`` reflecting that sorted order, 0-based).

    Args:
        responses: Per-persona StanceResponse objects for one round (already
            filtered to non-quarantined).
        top_k: Maximum number of clusters to emit (PROMPT default 5).

    Returns:
        Up to ``top_k`` DissentCluster objects. Empty list if no responses.
    """
    if not responses:
        return []

    # Bucket by stance.
    buckets: dict[str, list[StanceResponse]] = {}
    for r in responses:
        buckets.setdefault(r.stance, []).append(r)

    clusters_unsorted: list[tuple[str, list[StanceResponse]]] = []
    for stance, members in buckets.items():
        # The stub subclusterer currently returns one subcluster per stance.
        # If a later implementation splits a bucket, we'd flatten here.
        for _ in _ngram_subcluster([r.post for r in members]):
            clusters_unsorted.append((stance, members))

    # Sort by n_members desc, then by stance name asc for stable ties so the
    # output is reproducible across runs (matters for evidence hashing).
    clusters_unsorted.sort(key=lambda kv: (-len(kv[1]), kv[0]))

    out: list[DissentCluster] = []
    for cid, (stance, members) in enumerate(clusters_unsorted[:top_k]):
        # Highest-confidence post is the cluster sample. Tie-break by post
        # text so the choice is deterministic.
        sample = max(members, key=lambda r: (r.confidence, r.post))
        out.append(
            DissentCluster(
                cluster_id=cid,
                stance=stance,  # type: ignore[arg-type]
                sample_post=sample.post,
                n_members=len(members),
            )
        )
    return out
