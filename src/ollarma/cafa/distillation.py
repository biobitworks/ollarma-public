"""D5 — distillation-pair collection (RTB-05 collection side).

Ollarma *collects*; Antigence *trains*. When a claim escalates to a large local
(or, last-resort, frontier) judge, the (input, verdict, rationale) tuple is logged
as a distillation training pair so a smaller LOCAL antibody can later be trained to
match the larger cell — keeping the swarm cheap (local→local distillation).

Pure-local, append-only JSONL, deduplicated by input+teacher hash. No model, no
network. Consumes the ``escalation_request`` shape emitted by ``cascade.screen_text_claim``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_STORE = ".ollarma/cafa_distillation_pairs.jsonl"


def _hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


@dataclass
class DistillationPair:
    target: str
    antibody: str
    teacher_model: str          # the large local cell (or frontier, if absolutely_necessary)
    teacher_tier: str           # "reasoning_rung" | "ceiling_rung" | "frontier"
    input_text: str
    cited_pmids: list[str]
    verdict: str                # teacher's label (e.g. SUPPORT/REFUTE/NOINFO)
    rationale: str
    frontier_used: bool         # was this an absolutely_necessary frontier call?
    pair_id: str = ""
    input_hash: str = ""

    def finalize(self) -> "DistillationPair":
        self.input_hash = _hash({"t": self.input_text, "ab": self.antibody})
        self.pair_id = _hash({"in": self.input_hash, "teacher": self.teacher_model,
                              "v": self.verdict})
        return self


class DistillationCollector:
    """Append-only, deduplicated collector for distillation pairs."""

    def __init__(self, store: str | Path = DEFAULT_STORE) -> None:
        self.store = Path(store)
        self._seen: set[str] = set()
        if self.store.exists():
            for line in self.store.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        self._seen.add(json.loads(line).get("pair_id", ""))
                    except json.JSONDecodeError:
                        continue

    def collect(self, *, escalation_request: dict, teacher_model: str,
                teacher_tier: str, verdict: str, rationale: str,
                frontier_used: bool = False) -> DistillationPair | None:
        """Record one teacher verdict for an escalation request. Returns the pair,
        or None if it's a duplicate (already collected)."""
        pair = DistillationPair(
            target=escalation_request.get("target", ""),
            antibody=(escalation_request.get("antibodies") or ["?"])[0],
            teacher_model=teacher_model,
            teacher_tier=teacher_tier,
            input_text=escalation_request.get("text", ""),
            cited_pmids=list(escalation_request.get("cited_pmids", [])),
            verdict=verdict,
            rationale=rationale,
            frontier_used=frontier_used,
        ).finalize()
        if pair.pair_id in self._seen:
            return None
        self._seen.add(pair.pair_id)
        self.store.parent.mkdir(parents=True, exist_ok=True)
        with open(self.store, "a") as f:
            f.write(json.dumps(asdict(pair)) + "\n")
        return pair

    def count(self) -> int:
        return len(self._seen)

    def frontier_fraction(self) -> float:
        """Share of collected pairs that needed a frontier call (should stay ~0)."""
        if not self.store.exists():
            return 0.0
        rows = [json.loads(l) for l in self.store.read_text().splitlines() if l.strip()]
        if not rows:
            return 0.0
        return sum(1 for r in rows if r.get("frontier_used")) / len(rows)
