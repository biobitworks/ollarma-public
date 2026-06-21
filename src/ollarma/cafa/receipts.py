"""D6 — verdict-provenance receipts for the CAFA antibody panel (RTB-02 shape).

Pure-local, deterministic, hash-chained. Turns a set of antibody :class:`Verdict`s
over an input into a tamper-evident receipt carrying the RTB-02 provenance fields
(target, antibody key, lane class, input/output hash, verdict, head type, claim
ceiling, authority status). Hash-chaining lets an auditor reconstruct the chain
(`prev_hash` -> `receipt_hash`) exactly like the rest of the Ollarma receipt
substrate. No model, no network.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

from .go_guardrails import Prediction, Verdict

GENESIS = "sha256:" + "0" * 64


def _sha256_json(obj) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _authority(status: str, calibration_status: str) -> str:
    """RTB-02: open/unknown calibration cannot hold authoritative block/reject."""
    if status == "block" and calibration_status in {"open", "unknown"}:
        return "advisory"          # downgraded — cannot authoritatively block
    return {"pass": "trusted", "flag": "advisory", "block": "trusted"}.get(status, "advisory")


@dataclass
class AntibodyReceipt:
    target: str                      # protein / submission id
    antibody: str
    lane_class: str                  # "core" for the deterministic rule_floor
    head_type: str                   # "rule_floor" (deterministic, no model)
    verdict: str                     # pass | flag | block
    authority: str                   # trusted | advisory | degraded | escalation_only
    input_hash: str
    output_hash: str
    detail: str
    n_offenders: int
    claim_ceiling: str = "MEASURED"  # deterministic check on a pinned ontology
    calibration_status: str = "closed"  # rule_floor is deterministic => closed
    model_role: str = "none"         # no model in the deterministic floor
    prev_hash: str = GENESIS
    receipt_hash: str = ""


def build_receipts(
    *,
    target: str,
    preds: list[Prediction],
    verdicts: list[Verdict],
    prev_hash: str = GENESIS,
) -> list[AntibodyReceipt]:
    """Hash-chain one receipt per antibody verdict over the same input."""
    input_hash = _sha256_json(
        [(p.protein_id, p.go_id, p.score) for p in sorted(
            preds, key=lambda x: (x.protein_id, x.go_id))]
    )
    out: list[AntibodyReceipt] = []
    chain = prev_hash
    for v in verdicts:
        rec = AntibodyReceipt(
            target=target,
            antibody=v.antibody,
            lane_class="core",
            head_type="rule_floor",
            verdict=v.status,
            authority=_authority(v.status, "closed"),
            input_hash=input_hash,
            output_hash=_sha256_json({"antibody": v.antibody, "status": v.status,
                                      "offenders": v.offenders}),
            detail=v.detail,
            n_offenders=len(v.offenders),
            prev_hash=chain,
        )
        body = {k: getattr(rec, k) for k in (
            "target", "antibody", "lane_class", "head_type", "verdict",
            "authority", "input_hash", "output_hash", "detail", "n_offenders",
            "claim_ceiling", "calibration_status", "model_role", "prev_hash")}
        rec.receipt_hash = _sha256_json(body)
        chain = rec.receipt_hash
        out.append(rec)
    return out


def verify_chain(receipts: list[AntibodyReceipt]) -> bool:
    """Re-derive each receipt hash and confirm the prev->receipt linkage holds."""
    expected_prev = receipts[0].prev_hash if receipts else GENESIS
    for rec in receipts:
        if rec.prev_hash != expected_prev:
            return False
        body = {k: getattr(rec, k) for k in (
            "target", "antibody", "lane_class", "head_type", "verdict",
            "authority", "input_hash", "output_hash", "detail", "n_offenders",
            "claim_ceiling", "calibration_status", "model_role", "prev_hash")}
        if rec.receipt_hash != _sha256_json(body):
            return False
        expected_prev = rec.receipt_hash
    return True


def receipts_to_jsonl(receipts: list[AntibodyReceipt]) -> str:
    return "\n".join(json.dumps(asdict(r)) for r in receipts)
