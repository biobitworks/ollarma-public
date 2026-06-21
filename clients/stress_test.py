#!/usr/bin/env python3
"""Bounded concurrency stress test for the local AI lane via ollarma_client.

Hammers a mix of access tiers (ask/chat/route/ollama/kb_search) under limited
concurrency and reports error rate + latency percentiles. Exits non-zero if any
request errors — i.e. "green" means a clean run. Bounded by design so it does not
worsen host memory pressure (Ollarma serializes inference internally anyway).

Usage: python3 stress_test.py [N_REQUESTS] [N_WORKERS]
"""
from __future__ import annotations

import concurrent.futures as cf
import sys
import time

sys.path.insert(0, "<repo>/clients")
import ollarma_client as oc  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 24
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 6

# Mixed workload. Each entry: (label, callable). Short prompts keep inference cheap.
WORK = [
    ("ask+project", lambda i: oc.ask(f"In one line, what is Watchtower? [{i}]", project="Watchtower")),
    ("chat", lambda i: oc.chat(f"Reply with exactly: OK{i}")),
    ("route", lambda i: oc.route("Ollarma", f"One line: what is Ollarma? [{i}]").get("lane", "?")),
    ("ollama", lambda i: oc.ollama(f"Reply with exactly: OK{i}", model="qwen3:1.7b")),
    ("kb_search", lambda i: oc.kb_search("Overwatch", "governance").get("status", "?")),
]


def main() -> int:
    h = oc.health()
    if not (h["ollama"] and h["ollarma"]):
        print(f"PREFLIGHT FAIL — ollama={h['ollama']} ollarma={h['ollarma']} detail={h['detail']}")
        return 2
    print(f"preflight OK (ollarma={h['detail'].get('ollarma')}); N={N} workers={WORKERS}")

    jobs = [(WORK[i % len(WORK)], i) for i in range(N)]
    results: list[tuple[str, bool, float, str]] = []

    def run(job):
        (label, fn), i = job
        t0 = time.monotonic()
        try:
            out = fn(i)
            return (label, True, time.monotonic() - t0, str(out)[:40])
        except Exception as exc:  # noqa: BLE001
            return (label, False, time.monotonic() - t0, f"{type(exc).__name__}: {exc}"[:120])

    t_start = time.monotonic()
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(run, jobs):
            results.append(r)
    wall = time.monotonic() - t_start

    ok = [r for r in results if r[1]]
    bad = [r for r in results if not r[1]]
    lat = sorted(r[2] for r in ok)

    def pct(p):
        return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0.0

    by_label: dict[str, list[int]] = {}
    for label, good, _, _ in results:
        by_label.setdefault(label, [0, 0])
        by_label[label][0 if good else 1] += 1

    print(f"\nrequests={len(results)} ok={len(ok)} errors={len(bad)} wall={wall:.1f}s")
    print(f"latency  p50={pct(0.5):.2f}s  p95={pct(0.95):.2f}s  max={lat[-1] if lat else 0:.2f}s")
    print("per-tier (ok/err):")
    for label, (g, b) in sorted(by_label.items()):
        print(f"  {label:14s} {g}/{b}")
    if bad:
        print("\nERRORS:")
        for label, _, _, detail in bad[:10]:
            print(f"  [{label}] {detail}")
        print("\nRESULT: RED")
        return 1
    print("\nRESULT: GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
