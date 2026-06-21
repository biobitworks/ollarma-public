#!/usr/bin/env python3
"""GPU vs CPU decode-throughput benchmark for local Ollama models.

For each model: run the SAME prompt once on GPU (default) and once CPU-forced
(num_gpu=0), measuring decode_tps from Ollama's timing fields. Sequential by
design — no concurrent requests during measurement (contaminates timing).
Warms each (model, placement) combo once and discards the warmup.

decode_tps = eval_count / (eval_duration / 1e9)
prefill_tps = prompt_eval_count / (prompt_eval_duration / 1e9)

Usage: python3 gpu_vs_cpu_bench.py [model1 model2 ...]
Default roster picks small/mid models that fit alongside CPU runs safely.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
PROMPT = "Write a Python function that returns the nth Fibonacci number iteratively, then explain it in two sentences."
NUM_PREDICT = 160  # bounded output so CPU runs don't take forever

DEFAULT_MODELS = ["qwen2.5:1.5b", "qwen3:1.7b", "phi4-mini:latest", "qwen2.5-coder:7b"]


def gen(model: str, num_gpu: int | None, timeout: float) -> dict:
    opts = {"temperature": 0.0, "seed": 42, "num_ctx": 4096, "num_predict": NUM_PREDICT}
    if num_gpu is not None:
        opts["num_gpu"] = num_gpu
    body = json.dumps({"model": model, "prompt": PROMPT, "stream": False, "options": opts}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    out["_wall"] = time.monotonic() - t0
    return out


def metrics(out: dict) -> dict:
    ec = out.get("eval_count", 0)
    ed = out.get("eval_duration", 0)
    pc = out.get("prompt_eval_count", 0)
    pd = out.get("prompt_eval_duration", 0)
    return {
        "decode_tps": (ec / (ed / 1e9)) if ed else 0.0,
        "prefill_tps": (pc / (pd / 1e9)) if pd else 0.0,
        "eval_count": ec,
        "wall": out.get("_wall", 0.0),
        "load_ms": out.get("load_duration", 0) / 1e6,
    }


def bench_one(model: str) -> dict | None:
    row = {"model": model}
    for label, num_gpu, timeout in [("gpu", None, 180), ("cpu", 0, 600)]:
        try:
            gen(model, num_gpu, timeout=60)  # warmup, discarded
            time.sleep(1.0)
            out = gen(model, num_gpu, timeout=timeout)
            if "error" in out:
                row[label] = {"error": out["error"][:80]}
                continue
            row[label] = metrics(out)
        except Exception as exc:  # noqa: BLE001
            row[label] = {"error": f"{type(exc).__name__}: {exc}"[:100]}
        time.sleep(1.0)
    return row


def main() -> int:
    models = sys.argv[1:] or DEFAULT_MODELS
    print(f"GPU vs CPU decode benchmark | num_predict={NUM_PREDICT} seed=42 temp=0.0\n")
    rows = []
    for m in models:
        print(f"  benchmarking {m} ...", flush=True)
        rows.append(bench_one(m))

    print(f"\n{'model':22s} {'gpu_tps':>9s} {'cpu_tps':>9s} {'speedup':>8s} {'gpu_load_ms':>11s}")
    print("-" * 64)
    for r in rows:
        g = r.get("gpu", {})
        c = r.get("cpu", {})
        gt = g.get("decode_tps")
        ct = c.get("decode_tps")
        if gt and ct:
            sp = f"{gt / ct:.2f}x"
            print(f"{r['model']:22s} {gt:9.1f} {ct:9.1f} {sp:>8s} {g.get('load_ms', 0):11.0f}")
        else:
            note = g.get("error") or c.get("error") or "n/a"
            print(f"{r['model']:22s}  -- partial -- {note}")
    print()
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
