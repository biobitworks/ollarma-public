<!-- GSD:project-start source:PROJECT.md -->
## Project

**local-model-bench**

A reusable benchmarking harness for evaluating local Ollama models on Biobitworks workloads — science reasoning, code generation, and swarm orchestration. Produces scored, reproducible outputs that can be rerun as models evolve, plus a model selection guide derived from those results. Runs entirely offline on Apple M1 Pro 16GB.

**Core Value:** A scripted, repeatable harness that maps each Biobitworks workload tier to its best available local model, with evidence — so model selection decisions are data-driven rather than intuition-based.

### Constraints

- **Hardware**: M1 Pro 16GB — max ~12GB effective model VRAM budget
- **Runtime**: Ollama 0.19+ with MLX backend — no CUDA, no bfloat16 workarounds
- **Offline**: No internet required at benchmark runtime — prompts and models local
- **Reproducibility**: Each benchmark run must be deterministic (fixed seed where supported)
- **Scope**: Harness only — no production integration until model selection is validated
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

## Recommended Stack
| Component | Library / Tool | Version | Rationale | Confidence |
|-----------|---------------|---------|-----------|------------|
| **LLM Runtime** | Ollama | 0.19+ (MLX preview) | Native Apple Silicon MLX backend as of 0.19; 57% faster prefill, 93% faster decode vs 0.18 llama.cpp backend. Only production-grade offline runtime for M1 with model management built in. | HIGH |
| **Ollama client** | `ollama` (Python SDK) | 0.6.1 | Official SDK; exposes both sync and async clients; returns structured response objects with all timing fields. Lower friction than raw HTTP for harness iteration. | HIGH |
| **HTTP fallback** | `httpx` | >=0.27 | For streaming raw ndjson when SDK abstraction is insufficient (e.g., token-level timing of each chunk). `requests` works but lacks native async. | MEDIUM |
| **Data validation** | `pydantic` | v2 (>=2.6) | Define typed `BenchmarkResult`, `ModelMetrics`, `RunConfig` schemas. Validates harness output before serialization. V2 is ~5-10x faster than v1 for model instantiation. | HIGH |
| **CLI entrypoint** | `typer` | >=0.12 | Typer wraps Click with type annotations; generates `--help` automatically; pairs cleanly with Pydantic config models. Async support added in 0.10. | MEDIUM |
| **Terminal output** | `rich` | >=13.7 | Progress bars, live tables, and formatted markdown output in terminal. No external deps. Pairs with Typer naturally. | HIGH |
| **JSON serialization** | `orjson` | >=3.10 | 2-5x faster than stdlib `json` for writing large result files; handles datetime and Pydantic models natively. | MEDIUM |
| **LLM judge / scoring** | `deepeval` | >=1.4 | Only major eval framework with first-class Ollama integration for local judge. G-Eval supports custom rubrics (science reasoning, code quality). Works fully offline. | MEDIUM |
| **Reference datasets** | HumanEval (`openai/human-eval` on HuggingFace) | static | 164 Python problems with unit test pass@k evaluation. Canonical code generation benchmark. Available offline after one-time download. | HIGH |
| **Reference datasets** | MMLU-Pro subset | static | Reasoning-focused MCQ benchmark; biology/chemistry/physics subsets relevant to Biobitworks workloads. Download once, evaluate offline. | MEDIUM |
| **Test runner** | `pytest` | >=8.0 | DeepEval's pytest plugin runs eval suites as unit tests; enables CI integration later. Also useful for regression testing harness code itself. | HIGH |
| **Results store** | Flat JSON + Markdown | — | Machine-readable: one `.json` per run (orjson); human-readable: `rich` rendered Markdown table written to file. SQLite (via `sqlite3` stdlib) is the upgrade path for run history queries — no extra dep. | HIGH |
| **Python version** | CPython | 3.11 or 3.12 | 3.12 shows measurable perf gains over 3.10; both tested by Ollama SDK team. Avoid 3.13 (too new for deepeval deps as of early 2026). | MEDIUM |
## Ollama Integration
### How to call the API
### Measuring tokens/sec reliably
| Field | Meaning |
|-------|---------|
| `prompt_eval_count` | Input tokens processed |
| `prompt_eval_duration` | Time to prefill (ns) |
| `eval_count` | Output tokens generated |
| `eval_duration` | Time to decode (ns) |
| `load_duration` | Model load time (ns) — only nonzero on cold start |
| `total_duration` | Wall clock total (ns) |
- `load_duration` is nonzero only on cold start (model not yet in memory). Always warm up with one dummy request before measuring, then discard the warmup result.
- `prompt_eval_duration` can be `0` on very short prompts where prefill completes faster than the timer resolution — treat 0 as unmeasurable, not infinite.
- `num_ctx` controls KV cache allocation; setting it too large on M1 16GB will push the model into swap. Standardize to `num_ctx=4096` for benchmarking unless the task specifically requires longer context. Larger `num_ctx` inflates `load_duration` and memory footprint but does not affect `decode_tps` once loaded.
- Set `temperature=0.0` and `seed=42` (via options) for reproducible outputs. Ollama 0.19+ respects `seed` on MLX backend.
### Concurrency
## Evaluation Framework
### Scoring strategy (three-layer approach)
- Pass@1 on HumanEval: execute generated code against test suite (`subprocess`, sandboxed)
- MCQ accuracy on MMLU-Pro subset: exact match on answer letter
- Latency metrics from Ollama API (decode_tps, ttft_ms)
- Judge model recommendation: `phi4-mini` (already in evaluation set; small enough to keep loaded alongside the model under test at ~3GB)
- Use G-Eval for custom rubrics:
- Export random sample of 5-10 outputs per model per task category to Markdown
- Manual review against domain expectations (Biobitworks context)
### Why not use lm-evaluation-harness directly
## Apple Silicon Specifics
### Ollama 0.19 MLX backend (as of March 2026)
- Ollama 0.19 ships MLX as a **preview** backend for Apple Silicon. The blog announcement benchmarked on M5/M5 Pro/M5 Max. M1 Pro should also benefit but performance gains may be smaller.
- MLX uses Apple's unified memory architecture: CPU and GPU share the same physical memory pool. This means a 16GB M1 Pro has roughly 12-13GB usable for model weights after macOS overhead (~2-3GB reserved).
- Effective VRAM budget for models: **~12GB**. Models that fit entirely in unified memory run at full MLX speed. Models that spill into system swap will show dramatic decode_tps degradation (10x or more).
| Model | Q4 size | Fits in 12GB? |
|-------|---------|---------------|
| qwen3:1.7b | ~1.1GB | Yes |
| smollm2 (1.7B) | ~1.1GB | Yes |
| qwen3:4b | ~2.4GB | Yes |
| phi4-mini (3.8B) | ~2.3GB | Yes |
| qwen3-coder:7b | ~4.5GB | Yes |
| qwen3:8b | ~5.0GB | Yes |
| qwen3.5:9b | ~5.5GB | Yes |
### Thermal throttling
- Apple Silicon throttles when the SoC hits ~85-90C (chip temp, not ambient).
- Sustained benchmark runs (10+ prompts per model back-to-back) can trigger throttle, causing decode_tps to drop mid-run.
- Mitigation: insert a 2-second sleep between each prompt. Monitor with `sudo powermetrics --samplers smc -n1` for temp readings before and after run. Log the thermal state in results metadata.
- For comparing models fairly: run each model in a separate session (restart Ollama between models) to normalize thermal state.
### Memory pressure monitoring
# Check memory pressure during benchmark run
# log mem.available and swap.used per benchmark step
### Concurrency limit for benchmarking
### MLX model format
## What NOT to Use
| Tool | Reason Rejected |
|------|----------------|
| **vLLM** | Requires CUDA (NVIDIA GPU). Apple Silicon support exists as experimental `vllm-mlx` but it is not the production path and adds significant setup complexity. Use Ollama 0.19 MLX instead. |
| **llama.cpp directly** | Ollama 0.19 already wraps MLX (faster than llama.cpp Metal). Direct llama.cpp access would require reimplementing model management, API layer, and format conversion that Ollama provides. |
| **mlx-lm (Apple's Python package)** | Bypasses Ollama entirely — no model registry, no REST API, no streaming abstraction. Suitable for research; not suitable for a reusable harness that should work with any Ollama-served model. |
| **LangChain / LlamaIndex** | Heavy abstraction layers with significant overhead and frequent breaking changes. For a benchmark harness that must be reproducible, direct Ollama SDK calls are more stable and transparent. |
| **RAGAS** | Designed for RAG evaluation pipelines. Not a fit for generic reasoning/code/routing benchmarks. Use DeepEval G-Eval instead, which is RAGAS-compatible but more flexible. |
| **OpenAI API as judge** | Requires internet, costs money, breaks offline requirement. |
| **Transformers (HuggingFace)** | Loading models via Transformers on MPS (Metal Performance Shaders) requires manual quantization and is slower than Ollama MLX. Also defeats the point of benchmarking Ollama specifically. |
| **lm-evaluation-harness (as primary harness)** | Heavyweight, designed for broad leaderboard sweeps. Ollama integration requires a custom provider plugin. Overkill for a fixed-task workload harness. Can import specific task datasets from it, but don't use it as the runner. |
| **asyncio concurrency during measurement** | Sending parallel requests to Ollama during a benchmark contaminates timing measurements. All measurement requests must be sequential. Async is fine for non-measurement tasks (loading configs, writing results). |
## Confidence Notes
| Area | Confidence | Notes |
|------|------------|-------|
| Ollama API timing fields | HIGH | Verified against official docs.ollama.com/api/usage. Fields and formulas confirmed. |
| Ollama Python SDK version | HIGH | PyPI confirmed 0.6.1 as latest (Nov 2025). |
| Ollama 0.19 MLX backend | MEDIUM | Announced March 31, 2026. Benchmarked on M5 hardware by Ollama team. M1 performance gains are extrapolated — verify decode_tps improvement empirically on M1 Pro before trusting numeric claims. MLX preview may not support all model architectures yet. |
| DeepEval Ollama integration | MEDIUM-HIGH | Official integration page exists; CLI command and Python API verified. Offline capability confirmed. Version not pinned — check `pip show deepeval` after install to confirm >=1.4. |
| Model memory fit estimates | MEDIUM | Based on Q4 quantization sizes from community reports and VRAM guides. Actual sizes depend on quantization level Ollama uses for each model. Verify with `ollama show <model> --verbose` after pulling. |
| Thermal throttling behavior | MEDIUM | Based on community reports for Apple Silicon LLM workloads; no Ollama-specific thermal docs exist. M1 Pro thermal limit is well-documented as a general Apple Silicon behavior. |
| HumanEval local execution | HIGH | Standard benchmark; official repo (openai/human-eval) provides local runner. Safety note: code execution requires sandbox — use `subprocess` with timeout or `restrictedpython`. |
| MMLU-Pro offline use | MEDIUM | Dataset available on HuggingFace; offline after one-time download. Evaluation is MCQ exact-match, so no judge model needed for this component. |
### Things to verify at harness build time
## Sources
- [Ollama API Usage Documentation](https://docs.ollama.com/api/usage)
- [Ollama 0.19 MLX announcement](https://ollama.com/blog/mlx)
- [ollama Python package on PyPI](https://pypi.org/project/ollama/) — confirmed 0.6.1
- [DeepEval Ollama integration](https://deepeval.com/integrations/models/ollama)
- [DeepEval G-Eval metric](https://deepeval.com/docs/metrics-llm-evals)
- [Ollama MLX Runner internals (DeepWiki)](https://deepwiki.com/ollama/ollama/5.7-mlx-runner-(apple-silicon))
- [Production-Grade LLM Inference on Apple Silicon — arxiv 2511.05502](https://arxiv.org/abs/2511.05502)
- [HumanEval benchmark repo](https://github.com/openai/human-eval)
- [MMLU-Pro benchmark repo](https://github.com/TIGER-AI-Lab/MMLU-Pro)
- [Ollama parallel request handling](https://www.glukhov.org/llm-performance/ollama/how-ollama-handles-parallel-requests/)
- [Ollama VRAM requirements guide](https://localllm.in/blog/ollama-vram-requirements-for-local-llms)
- [9to5Mac: Ollama adopts MLX for Apple Silicon](https://9to5mac.com/2026/03/31/ollama-adopts-mlx-for-faster-ai-performance-on-apple-silicon-macs/)
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, or `.github/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
