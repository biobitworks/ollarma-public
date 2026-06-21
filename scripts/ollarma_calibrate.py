#!/usr/bin/env python3
"""`ollarma calibrate` (U1 first cut) — self-calibrate the cascade to the user's
own local models.

This is the concrete first step of the unification plan
(`docs/UNIFICATION_SINGLE_PACKAGE_PLAN.md` §2, loop A "roster profiling"). It is
what "a user can download and **train themselves** based on their unique needs"
means in practice: point Ollarma at the models you already run locally, and it
assigns each to a role in the verification cascade and emits a per-user policy
bundle — no fine-tuning, no cloud, no private data.

It reads the live local roster from the Ollama API (`/api/tags`), classifies each
model by parameter size into a cascade role using the measured size→role ladder
(`docs/ANTIGENCE_MODEL_SIZE_CELL_ROLE_FINDINGS.md`), warns on missing roles, and
writes a portable policy bundle JSON. Offline; talks only to localhost Ollama.

Roles (proper terms; metaphor in parens) — see docs/publication/TERMINOLOGY_EQUIVALENCE.md:
  retrieval_embedder (antigen-bank index) | screen_sensor (recall sensor) |
  structured_verifier (cell-type floor)   | judge (LLM-as-a-judge)        |
  router (cascade dispatcher)              | escalation_rung               |
  reasoning_rung (load-on-demand)          | ceiling_rung (benchmark-only) |
  frontier (cloud deferral — configured separately, not local)

Usage:
  python scripts/ollarma_calibrate.py                 # live roster -> policy bundle
  python scripts/ollarma_calibrate.py --out my.json   # custom output path
  python scripts/ollarma_calibrate.py --host http://127.0.0.1:11434
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def _params_billions(model: dict) -> float | None:
    """Best-effort parameter count in billions from /api/tags entry."""
    det = model.get("details") or {}
    psize = det.get("parameter_size")  # e.g. "7.6B", "137M"
    if isinstance(psize, str):
        s = psize.strip().upper()
        try:
            if s.endswith("B"):
                return float(s[:-1])
            if s.endswith("M"):
                return float(s[:-1]) / 1000.0
        except ValueError:
            pass
    # Fallback: estimate from disk size (~ Q4 ≈ 0.5 GB per B). Coarse.
    size = model.get("size")
    if isinstance(size, (int, float)) and size > 0:
        return round((size / 1e9) / 0.6, 1)
    return None


def classify(model: dict) -> tuple[str, str]:
    """Return (role, residency) for a model entry."""
    name = (model.get("name") or "").lower()
    family = ((model.get("details") or {}).get("family") or "").lower()
    b = _params_billions(model)

    if "embed" in name or "embed" in family:
        return "retrieval_embedder", "pinned"
    if b is None:
        return "unknown", "load_on_demand"
    if b < 2.0:
        return "screen_sensor", "pinned"          # high recall, rescue/fallback
    if b < 3.0:
        return "structured_verifier", "warm"      # cell-type floor (json_mode)
    if b < 5.0:
        # 3.8B phi-style → judge; other ~4B → structured verifier
        return ("judge" if "phi" in name else "structured_verifier"), "warm_if_room"
    if b < 8.0:
        return "router", "warm_if_room"           # needs json_grammar to block
    if b < 11.0:
        return "escalation_rung", "warm_if_room"
    if b < 18.0:
        return "reasoning_rung", "load_on_demand"  # evict after
    return "ceiling_rung", "benchmark_only"        # one at a time, evict after


REQUIRED_ROLES = ["screen_sensor", "structured_verifier", "router", "escalation_rung"]


def fetch_roster(host: str) -> list[dict]:
    with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=5) as r:
        return json.loads(r.read()).get("models", [])


def build_bundle(models: list[dict]) -> dict:
    assignments = []
    for m in sorted(models, key=lambda x: _params_billions(x) or 0):
        role, residency = classify(m)
        assignments.append({
            "model": m.get("name"),
            "params_b": _params_billions(m),
            "role": role,
            "residency": residency,
            "strict_output": (
                "json_grammar" if role == "router"
                else "json_mode" if role in {"structured_verifier", "judge",
                                             "escalation_rung", "reasoning_rung",
                                             "ceiling_rung"}
                else "none"
            ),
        })
    roles_present = {a["role"] for a in assignments}
    gaps = [r for r in REQUIRED_ROLES if r not in roles_present]
    return {
        "schema_version": 1,
        "kind": "ollarma_calibration_policy_bundle",
        "note": "Self-calibration of the verification cascade to the local roster. "
                "Roles from the measured size->role ladder; not fine-tuning.",
        "n_models": len(assignments),
        "assignments": assignments,
        "role_gaps": gaps,
        "escalation_policy": {
            "ladder": ["screen_sensor", "structured_verifier", "judge", "router",
                       "escalation_rung", "reasoning_rung", "ceiling_rung",
                       "frontier"],
            "one_rung_per_step": True,
            "max_models_ge_14b_resident": 1,
            "frontier": "configured separately (cloud deferral; opt-in, receipted)",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Self-calibrate the cascade to local models.")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--out", default="ollarma_policy_bundle.json")
    args = ap.parse_args()

    try:
        models = fetch_roster(args.host)
    except Exception as e:  # noqa: BLE001
        print(f"[calibrate] could not reach Ollama at {args.host}: {e}", file=sys.stderr)
        print("[calibrate] start Ollama (`ollama serve`) and pull at least a "
              "screen + verifier + router + escalation model.", file=sys.stderr)
        return 2

    if not models:
        print("[calibrate] no local models found. `ollama pull` some first.", file=sys.stderr)
        return 2

    bundle = build_bundle(models)
    with open(args.out, "w") as f:
        json.dump(bundle, f, indent=2)

    print(f"[calibrate] profiled {bundle['n_models']} local models -> {args.out}\n")
    for a in bundle["assignments"]:
        pb = f"{a['params_b']}B" if a["params_b"] else "?"
        print(f"  {a['model']:<28} {pb:>6}  ->  {a['role']:<20} "
              f"[{a['residency']}, {a['strict_output']}]")
    if bundle["role_gaps"]:
        print(f"\n[calibrate] WARNING — missing cascade roles: "
              f"{', '.join(bundle['role_gaps'])}. The cascade will fall back / "
              f"escalate earlier. `ollama pull` a model for each gap.")
    else:
        print("\n[calibrate] all required cascade roles are covered.")
    print("[calibrate] hard cases beyond the local ceiling escalate to a frontier "
          "model (opt-in, receipted) — that is the only step that leaves your machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
