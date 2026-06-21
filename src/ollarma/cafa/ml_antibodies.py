"""Local ML antibodies (Antigence-lane deliverable, scaffolded in the ollarma repo).

LOCAL-FIRST: every antibody here calls a **local** Ollama model only — no frontier,
no online DB (``docs/LOCAL_VS_ONLINE_BOUNDARY.md``). The model client is injected so
the logic is unit-tested offline with a stub; a live local model is used only at
run/smoke time.

A1 — ``claim_entailment``: given a CLAIM about a protein and the cited ABSTRACT
(resolved locally by ``citation.resolve_citations``), label SUPPORT / REFUTE /
NOINFO with confidence + rationale. This is the SciFact-style scientific claim
verification antibody (reuses the immunOS-preprint baseline framing). json-constrained,
fail-closed (unparseable/invalid output -> quarantined NOINFO, never a silent pass).

NOTE: this is the Antigence lane's antibody. It is scaffolded here by the Ollarma
lane (acting as Antigence) because the separate Codex peer launch is operator-gated;
a Codex peer can refine/own it. It consumes the Ollarma substrate verdict/receipt model.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

DEFAULT_MODEL = "qwen3.5:9b"
LABELS = ("SUPPORT", "REFUTE", "NOINFO")

# Ollama structured-output schema (constrained decoding).
_ENTAILMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["label", "confidence", "rationale"],
}

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _response_text(resp: Any) -> str | None:
    """Extract the generated text, falling back to `thinking` (thinking models
    emit structured output there when think is on)."""
    if isinstance(resp, dict):
        return resp.get("response") or resp.get("thinking")
    return getattr(resp, "response", None) or getattr(resp, "thinking", None)


@dataclass
class EntailmentVerdict:
    target: str
    antibody: str
    label: str                  # SUPPORT | REFUTE | NOINFO
    confidence: float
    rationale: str
    model: str
    head_type: str = "model_judge"
    calibration_status: str = "open"   # uncalibrated until A5 sets thresholds
    authority: str = "advisory"        # open calibration => advisory only (RTB-02 rule 11)
    quarantined: bool = False
    frontier_used: bool = False        # always False here (local-only)


def _build_prompt(claim: str, abstract: str) -> str:
    return (
        "You are a careful scientific claim verifier. Decide whether the ABSTRACT "
        "supports, refutes, or gives no information about the CLAIM.\n"
        "- SUPPORT: the abstract provides evidence the claim is true.\n"
        "- REFUTE: the abstract provides evidence the claim is false.\n"
        "- NOINFO: the abstract is irrelevant or insufficient to judge the claim.\n"
        "Do not use outside knowledge; judge ONLY from the abstract. Open-world: "
        "absence of evidence is NOINFO, never REFUTE.\n\n"
        f"CLAIM: {claim}\n\nABSTRACT: {abstract}\n\n"
        "Respond with JSON: {\"label\": one of [SUPPORT, REFUTE, NOINFO], "
        "\"confidence\": number in [0,1], \"rationale\": string <=30 words}."
    )


def _parse(text: str) -> dict | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = _JSON_RE.search(text or "")
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


class ClaimEntailmentAntibody:
    """Local NLI antibody. `client` must expose `.generate(model, prompt, format, options)`
    returning a mapping with a 'response' text field (the Ollama SDK shape)."""

    antibody = "claim_entailment"

    def __init__(self, client: Any, model: str = DEFAULT_MODEL) -> None:
        self.client = client
        self.model = model

    def judge(self, *, target: str, claim: str, abstract: str) -> EntailmentVerdict:
        if not abstract or not abstract.strip():
            # No locally-resolved abstract -> cannot verify; fail-closed NOINFO.
            return EntailmentVerdict(target, self.antibody, "NOINFO", 0.0,
                                     "no abstract available locally", self.model,
                                     quarantined=True)
        prompt = _build_prompt(claim, abstract)
        try:
            # think=False: thinking models (qwen3.x) otherwise emit the structured
            # verdict into the `thinking` field and leave `response` empty.
            resp = self.client.generate(
                model=self.model, prompt=prompt, format=_ENTAILMENT_SCHEMA,
                think=False, options={"temperature": 0.0, "seed": 42},
            )
        except TypeError:
            # older client without `think` kwarg
            resp = self.client.generate(
                model=self.model, prompt=prompt, format=_ENTAILMENT_SCHEMA,
                options={"temperature": 0.0, "seed": 42},
            )
        except Exception as e:  # noqa: BLE001 - local model/daemon failure -> fail-closed
            return EntailmentVerdict(target, self.antibody, "NOINFO", 0.0,
                                     f"model error: {type(e).__name__}", self.model,
                                     quarantined=True)
        text = _response_text(resp)
        data = _parse(text)
        if not data or data.get("label") not in LABELS:
            return EntailmentVerdict(target, self.antibody, "NOINFO", 0.0,
                                     "unparseable/invalid model output", self.model,
                                     quarantined=True)
        try:
            conf = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        if not math.isfinite(conf):
            return EntailmentVerdict(target, self.antibody, "NOINFO", 0.0,
                                     "invalid confidence from model output", self.model,
                                     quarantined=True)
        conf = min(1.0, max(0.0, conf))
        return EntailmentVerdict(
            target, self.antibody, data["label"], conf,
            str(data.get("rationale", ""))[:240], self.model,
        )
