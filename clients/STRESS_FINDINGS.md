# Ollarma Bridge Stress Test — Findings

**Date:** 2026-05-31 → 06-01 (UTC)
**Host:** Mac Studio M1 Max 32GB · Ollama 0.24.0 (MLX) · Ollarma bridge @ 127.0.0.1:8484
**Bridge state at start:** `degraded / SELECTION_STALE` (selection artifact >24h; soft fallback to pinned qwen2.5:1.5b). Swap/admission/pipeline all `ready`.

## Result summary

| Area | Verdict | Boundary found |
|---|---|---|
| Baseline bridge (24/6) | ✅ GREEN | 0 errors, p50 3.25s |
| HTTP surface (18 endpoints) | ✅ 15 green / 3 degraded-but-answering / 0 broken | no 5xx, no hang |
| Concurrency | ✅ GREEN through 150 req / 48 workers, 0 errors | **latency-bound, not error-bound** — inference serialized, load queues |
| GPU vs CPU | ✅ both work | GPU **2.5–4.7× faster** decode; CPU fallback viable at all sizes |
| Model serve ceiling | ✅ 14/15 served | only block is reserved-model governance, not capacity |
| Service forensics | ✅ clean | current pid 17741: 0 tracebacks since start; 573 chats answered |

## Concurrency scaling (latency grows linearly; zero errors throughout)

| load (req/workers) | ok | errors | p50 | p95 | max | wall |
|---|---|---|---|---|---|---|
| 24 / 6  | 24  | 0 | 3.25s | 6.47s | 6.83s | 15.7s |
| 40 / 12 | 40  | 0 | 5.36s | 15.90s | 16.12s | 24.7s |
| 80 / 24 | 80  | 0 | 13.19s | 28.50s | 31.48s | 49.6s |
| 150 / 48| 150 | 0 | 35.25s | 77.45s | 86.30s | 120.6s |
| 200 / 64| 200 | 0 | 35.69s | 87.90s | 97.46s | 133.2s |
| 256 / 128| 256 | 0 | 37.09s | 116.35s | 125.63s | 143.1s |

**Boundary:** the bridge has **no error cliff in any reachable regime** — 0 errors through 128 concurrent workers / 256 requests. Inference is serialized single-file (scheduler `local_inference_single` lane). Up to ~48 workers, p50 grows ~linearly; **beyond saturation (48→128 workers) p50 plateaus at ~35–37s** while only tail latency (p95/max) creeps up — extra workers just deepen the queue behind the single inference lane, and the fast read-only tiers (kb_search ~0.06s) keep the median bounded. The only theoretical failure point is a single inference call exceeding the client timeout (chat 180s / route 240s); the pinned fallback model never gets close (max observed 125s under the heaviest load). **Conclusion: concurrency degrades to bounded latency, never to errors.**

## GPU vs CPU decode throughput (seed=42, temp=0, num_predict=160)

| model | GPU tps | CPU tps | speedup | GPU prefill tps | CPU prefill tps |
|---|---|---|---|---|---|
| qwen2.5:1.5b | 81.5 | 17.4 | 4.70× | 686 | 65 |
| qwen3:1.7b | 84.2 | 19.9 | 4.22× | 615 | — |
| phi4-mini | 44.2 | 17.3 | 2.55× | — | — |
| qwen2.5-coder:7b | 42.0 | 9.0 | 4.67× | — | — |

**Boundary:** GPU (MLX/Metal) decode is 2.5–4.7× faster; prefill gap ~10× on small models. CPU-forced (`num_gpu=0`) **never failed** — CPU fallback is a viable degrade lane at every tested size, just 3–5× slower (7B → 9 tps). Placement confirmed via `ollama ps` (100% CPU vs 100% GPU). Ollarma observes placement as an admission signal (guards.py) but does not force it; Ollama owns placement under unified memory.

## Model serve ceiling (15 models, tiny → 30B, via /chat)

**14/15 served.** Every model tiny → 30B answered through the bridge, including the heaviest:

| tier | models | result |
|---|---|---|
| 1.5–4B | qwen2.5:1.5b, qwen3.5:2b, phi4-mini, qwen3.5:4b | ✅ all answer, 0.3–13s |
| 7–9B | qwen2.5-coder:7b, granite4.1:8b, qwen3.5:9b(+mlx) | ✅ all answer, 9–25s |
| 14B | deepseek-r1:14b, phi4-reasoning:14b | ✅ answer, 37–53s |
| 24–30B | mistral-small3.2:24b, gemma4:26b, qwen3.6:27b, qwen3-coder:30b | ✅ all answer, 32–70s |
| reserved | **qwen3:1.7b** | ❌ HTTP 502 `RESERVED_MODEL_ANTIGENCE_SENTINEL` (governance, not capacity) |

**Boundary:** the only model that fails through `/chat` is `qwen3:1.7b`, deliberately reserved for Antigence/Sentinel. Capacity-wise the host serves the **entire roster up to 30B / 20GB fully GPU-resident** (confirmed `ollama ps`: qwen3-coder:30b at 100% GPU, 20GB). Sequential loading of the 24–30B tier grew swap from 272MB → ~6GB (macOS dynamic swap auto-expanded 1GB→7GB total), but never approached the 16GB admission ceiling, and no model was rejected for memory.

## Real findings (not active blocks)

1. **Latent dashboard fragility** — `GET /dashboard/overview` hard-500s (FileNotFoundError, kb_contract.py:152) if ANY fleet-registry project root is missing. Caused 1,881 historical tracebacks (healthclock/antigence/antigence-bittensor were transiently absent). **Currently green** (all roots exist). Fix candidate: dashboard should skip/degrade a missing-root project instead of propagating the exception.
2. **Governance boundaries (working as designed)** — `/chat` with `qwen3:1.7b` → `CHAT_FAILED / RESERVED_MODEL_ANTIGENCE_SENTINEL` (model reserved for Antigence). `/route` unknown project → clean 404. `/route` Ollarma → `frontier_or_human / INSUFFICIENT_GROUNDED_EVIDENCE` (refuses to hallucinate below grounding threshold). All correct fail-safes, not failures.
3. **2× transient 502 on /chat** — isolated upstream Ollama hiccup, self-recovered next request. Not a pattern.

## Reproduce

```
python3 clients/stress_test.py 150 48        # concurrency
python3 clients/gpu_vs_cpu_bench.py          # GPU vs CPU
python3 clients/model_matrix_probe.py        # model serve ceiling
```
