#!/usr/bin/env python3
"""Per-model boundary probe through the Ollarma /chat bridge.

For each candidate model, ask the bridge to answer a trivial prompt while
forcing that specific model. Records: HTTP ok, effective vs requested model,
reason_code, decode placement (gpu/cpu from ollama ps), latency. Maps which
models the bridge can actually serve on THIS host vs which it degrades/refuses.

Sequential by design (no timing contamination; one model resident at a time).

Usage: python3 model_matrix_probe.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

sys.path.insert(0, "<repo>/clients")
import ollarma_client as oc  # noqa: E402

# Roster spanning tiny → large to find the host's serve ceiling.
MODELS = [
    "qwen2.5:1.5b", "qwen3:1.7b", "qwen3.5:2b", "phi4-mini:latest",
    "qwen3.5:4b", "qwen2.5-coder:7b", "granite4.1:8b", "qwen3.5:9b",
    "qwen3.5:9b-mlx", "deepseek-r1:14b", "phi4-reasoning:14b",
    "mistral-small3.2:24b", "gemma4:26b", "qwen3.6:27b", "qwen3-coder:30b",
]
PROMPT = "Reply with exactly the word: READY"


def ps_placement() -> dict:
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/ps")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return {m.get("model", ""): m.get("processor", "?") for m in data.get("models", [])}
    except Exception:
        return {}


def main() -> int:
    print(f"Per-model bridge probe via /chat | {len(MODELS)} models\n")
    print(f"{'model':22s} {'ok':>3s} {'lat_s':>7s} {'placement':>10s} {'effective_model':22s} reason")
    print("-" * 92)
    rows = []
    for m in MODELS:
        t0 = time.monotonic()
        ok, eff, reason, placement = False, "-", "-", "-"
        try:
            r = oc.chat_full(m and PROMPT, model=m, timeout=300)
            ok = bool(r.get("response"))
            eff = r.get("model", "-") or r.get("effective_model", "-")
            reason = r.get("reason_code", "-") or r.get("status", "-")
            placement = ps_placement().get(eff, ps_placement().get(m, "-"))
        except Exception as exc:  # noqa: BLE001
            reason = f"{type(exc).__name__}: {exc}"[:50]
        lat = time.monotonic() - t0
        rows.append({"model": m, "ok": ok, "lat_s": round(lat, 1),
                     "effective_model": eff, "reason": reason, "placement": placement})
        print(f"{m:22s} {('Y' if ok else 'N'):>3s} {lat:7.1f} {str(placement):>10s} {str(eff):22s} {reason}")
        time.sleep(1.5)
    print()
    print(json.dumps(rows, indent=2))
    served = sum(1 for r in rows if r["ok"])
    print(f"\nSERVED {served}/{len(rows)} models through the bridge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
