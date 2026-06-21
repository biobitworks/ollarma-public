"""D3 — citation resolution against a LOCAL corpus (fail-closed on novel PMIDs).

Local-first (``docs/LOCAL_VS_ONLINE_BOUNDARY.md`` zone ①): a cited PMID is resolved
against a local abstract corpus (SciFact / CAFA abstracts, downloaded once). A PMID
ABSENT from the local corpus is **not** silently fetched online — it is flagged
``absolutely_necessary`` so the operator's explicit gate decides (zone ④a). The
resolved abstract is what the Antigence ``claim_entailment`` lane consumes.

No network in this module: resolution is pure local lookup. The optional online
fetch for a novel PMID is a separate, explicitly-gated step (mirrors
``dataset_registry.bootstrap_go_basic``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .go_guardrails import Verdict

_PMID_NUM_RE = re.compile(r"(\d{4,9})")


def normalize_pmid(raw: str) -> str | None:
    """'PMID: 1234567' / 'PMID1234567' / '1234567' -> '1234567'."""
    m = _PMID_NUM_RE.search(raw or "")
    return m.group(1) if m else None


class LocalAbstractCorpus:
    """In-memory pmid -> abstract store loaded from a local JSONL file.

    JSONL rows: {"pmid": "1234567", "abstract": "..."} (extra keys ignored).
    """

    def __init__(self, pmid_to_abstract: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(pmid_to_abstract or {})

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "LocalAbstractCorpus":
        store: dict[str, str] = {}
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            pid = normalize_pmid(str(row.get("pmid", "")))
            if pid and row.get("abstract"):
                store[pid] = row["abstract"]
        return cls(store)

    def get(self, pmid: str) -> str | None:
        pid = normalize_pmid(pmid)
        return self._store.get(pid) if pid else None

    def __contains__(self, pmid: str) -> bool:
        pid = normalize_pmid(pmid)
        return pid in self._store if pid else False

    def __len__(self) -> int:
        return len(self._store)


@dataclass
class CitationResolution:
    target: str
    verdict: Verdict
    resolved: dict[str, str] = field(default_factory=dict)   # pmid -> abstract (local)
    novel_pmids: list[str] = field(default_factory=list)     # absent locally
    absolutely_necessary: bool = False                       # any novel -> online gate
    network_calls: int = 0                                   # always 0 here


def resolve_citations(target: str, cited_pmids: list[str],
                      corpus: LocalAbstractCorpus) -> CitationResolution:
    """Resolve cited PMIDs against the LOCAL corpus. No network.

    - present locally -> resolved (abstract supplied to the entailment lane)
    - absent           -> novel; flagged absolutely_necessary (do NOT auto-fetch)
    """
    resolved: dict[str, str] = {}
    novel: list[str] = []
    for raw in cited_pmids:
        pid = normalize_pmid(raw)
        if pid is None:
            novel.append(raw)
            continue
        abs = corpus.get(pid)
        if abs is not None and abs:  # noqa: E714 - explicit truthiness for clarity
            resolved[pid] = abs
        else:
            novel.append(pid)
    status = "pass" if cited_pmids and not novel else "flag"
    detail = ("all citations resolved locally" if status == "pass"
              else f"{len(novel)} novel/unresolvable PMID(s) (online gate required)")
    return CitationResolution(
        target=target,
        verdict=Verdict("pmid_resolves_local", status, detail, novel[:50]),
        resolved=resolved,
        novel_pmids=novel,
        absolutely_necessary=bool(novel),
    )
