"""D1 — local dataset registry + provenance for the CAFA verification substrate.

Local-first: datasets are a ONE-TIME download (zone ② of
``docs/LOCAL_VS_ONLINE_BOUNDARY.md``), then fully offline. This module registers
each artifact with path + sha256 + provenance into a manifest
(``docs/source_intake.manifest.jsonl``) so both lanes consume identical, hashed
inputs. The bootstrap fetch is explicit and operator-invoked — never automatic —
so a normal run touches the network zero times.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

# Canonical CAFA-relevant sources. Only ``go_basic`` has a stable public URL we
# fetch; CAFA annotations / SciFact / IA weights are operator-supplied local files
# (competition + dataset downloads), registered by path.
GO_BASIC_URL = "http://purl.obolibrary.org/obo/go/go-basic.obo"

KNOWN_SOURCES = {
    "go_basic": {"kind": "ontology", "url": GO_BASIC_URL, "evidence_class": "reference"},
    "cafa_train": {"kind": "annotations", "url": None, "evidence_class": "reference"},
    "scifact": {"kind": "claim_verification", "url": None, "evidence_class": "reference"},
    "ia_weights": {"kind": "weights", "url": None, "evidence_class": "reference"},
}


@dataclass
class DatasetRecord:
    name: str
    kind: str
    path: str
    sha256: str
    size_bytes: int
    source_url: str | None
    evidence_class: str
    claim_ceiling: str = "MEASURED"  # a registered local file is a measured fact
    note: str = ""


def sha256_file(path: str | Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def register(name: str, path: str | Path, *, manifest: str | Path,
             kind: str | None = None, source_url: str | None = None,
             evidence_class: str = "reference", note: str = "") -> DatasetRecord:
    """Hash a local file and append a provenance row to the manifest (JSONL)."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"dataset '{name}' not found at {p}")
    known = KNOWN_SOURCES.get(name, {})
    digest, size = sha256_file(p)
    # Store a repo/cwd-relative path when possible so the committed manifest is
    # portable and never carries an absolute home path.
    try:
        stored_path = str(p.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        stored_path = str(p)
    rec = DatasetRecord(
        name=name, kind=kind or known.get("kind", "unknown"),
        path=stored_path, sha256=f"sha256:{digest}", size_bytes=size,
        source_url=source_url or known.get("url"),
        evidence_class=evidence_class, note=note,
    )
    mp = Path(manifest)
    mp.parent.mkdir(parents=True, exist_ok=True)
    with open(mp, "a") as f:
        f.write(json.dumps(asdict(rec)) + "\n")
    return rec


def bootstrap_go_basic(dest: str | Path, *, allow_download: bool = False) -> Path:
    """One-time fetch of go-basic.obo (zone ② bootstrap).

    Fail-closed: refuses to hit the network unless ``allow_download=True`` is
    explicitly passed (the operator's "this online step is sanctioned" gate).
    No-op if the file already exists locally.
    """
    p = Path(dest)
    if p.is_file():
        return p
    if not allow_download:
        raise PermissionError(
            f"{p} absent; pass allow_download=True to perform the one-time "
            f"bootstrap fetch from {GO_BASIC_URL} (zone ② — sanctioned online-once)."
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    # Some GO mirrors 403 a UA-less request; send a plain UA + follow redirects.
    req = urllib.request.Request(
        GO_BASIC_URL,
        headers={"User-Agent": "ollarma-cafa/0.1 (+local verification substrate)"},
    )
    with urllib.request.urlopen(req, timeout=120) as r, open(p, "wb") as f:
        f.write(r.read())
    return p
