"""Minimal, stdlib-only Gene Ontology (OBO) loader + DAG.

Local-first by design: no third-party deps (no goatools / obonet / networkx).
The deterministic GO guardrails (``go_guardrails.py``) only need term existence,
namespace, obsolescence, alt-id remap, and ancestor traversal over ``is_a`` +
``part_of`` (the CAFA propagation relations). A ~1-file parser covers that and
keeps the verification substrate dependency-light and fully offline.

``goatools`` / ``CAFA-evaluator`` are reserved as an optional *validation oracle*
(cross-check this loader's ancestors against theirs) — not a runtime dependency.

Root terms (weights 0 in CAFA): GO:0008150 (BP), GO:0003674 (MF), GO:0005575 (CC).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# CAFA propagates over is_a and part_of. Namespace roots:
NAMESPACE_ROOTS = {
    "biological_process": "GO:0008150",
    "molecular_function": "GO:0003674",
    "cellular_component": "GO:0005575",
}
NAMESPACE_ABBR = {
    "biological_process": "BP",
    "molecular_function": "MF",
    "cellular_component": "CC",
}

_GO_ID_RE = re.compile(r"GO:\d{7}")


@dataclass
class GoTerm:
    id: str
    name: str = ""
    namespace: str = ""
    is_obsolete: bool = False
    parents: set[str] = field(default_factory=set)  # is_a + part_of targets


class GoDag:
    """Parsed GO DAG with deterministic ancestor traversal."""

    def __init__(self) -> None:
        self.terms: dict[str, GoTerm] = {}
        self.alt_to_primary: dict[str, str] = {}
        self._ancestor_cache: dict[str, frozenset[str]] = {}

    # ---- construction -----------------------------------------------------
    @classmethod
    def from_obo(cls, path: str | Path) -> "GoDag":
        dag = cls()
        cur: dict | None = None

        def flush(stanza: dict | None) -> None:
            if not stanza or stanza.get("_type") != "Term":
                return
            tid = stanza.get("id")
            if not tid:
                return
            term = GoTerm(
                id=tid,
                name=stanza.get("name", ""),
                namespace=stanza.get("namespace", ""),
                is_obsolete=stanza.get("is_obsolete", False),
                parents=set(stanza.get("parents", [])),
            )
            dag.terms[tid] = term
            for alt in stanza.get("alt_id", []):
                dag.alt_to_primary[alt] = tid

        for raw in Path(path).read_text().splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                flush(cur)
                cur = {"_type": line[1:-1], "parents": [], "alt_id": []}
                continue
            if cur is None or not line or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if key == "id":
                cur["id"] = val.split()[0] if val else val
            elif key == "name":
                cur["name"] = val
            elif key == "namespace":
                cur["namespace"] = val
            elif key == "is_obsolete":
                cur["is_obsolete"] = val.lower() == "true"
            elif key == "alt_id":
                m = _GO_ID_RE.search(val)
                if m:
                    cur["alt_id"].append(m.group(0))
            elif key == "is_a":
                m = _GO_ID_RE.search(val)
                if m:
                    cur["parents"].append(m.group(0))
            elif key == "relationship":
                # e.g. "relationship: part_of GO:0005634 ! nucleus"
                if val.startswith("part_of"):
                    m = _GO_ID_RE.search(val)
                    if m:
                        cur["parents"].append(m.group(0))
        flush(cur)
        return dag

    # ---- queries ----------------------------------------------------------
    def normalize(self, term_id: str) -> str | None:
        """Map an alt_id to its primary id; return None if unknown."""
        if term_id in self.terms:
            return term_id
        return self.alt_to_primary.get(term_id)

    def is_valid(self, term_id: str, *, allow_obsolete: bool = False) -> bool:
        primary = self.normalize(term_id)
        if primary is None:
            return False
        if not allow_obsolete and self.terms[primary].is_obsolete:
            return False
        return True

    def namespace_of(self, term_id: str) -> str | None:
        primary = self.normalize(term_id)
        return self.terms[primary].namespace if primary else None

    def subontology_of(self, term_id: str) -> str | None:
        ns = self.namespace_of(term_id)
        return NAMESPACE_ABBR.get(ns) if ns else None

    def ancestors(self, term_id: str) -> frozenset[str]:
        """All ancestors (is_a + part_of), excluding the term itself.

        Deterministic; cycle-safe; cached. Roots have no ancestors.
        """
        primary = self.normalize(term_id)
        if primary is None:
            return frozenset()
        if primary in self._ancestor_cache:
            return self._ancestor_cache[primary]
        seen: set[str] = set()
        stack = list(self.terms[primary].parents)
        while stack:
            p = stack.pop()
            if p in seen or p not in self.terms:
                continue
            seen.add(p)
            stack.extend(self.terms[p].parents)
        result = frozenset(seen)
        self._ancestor_cache[primary] = result
        return result

    def __len__(self) -> int:
        return len(self.terms)
