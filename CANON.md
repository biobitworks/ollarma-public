# CANON

> **Status**: DRAFT
> **Version**: 0.1.0

## Purpose

Define the invariant rules that govern this project's scientific claims.

## Invariants

### Invariant 1
No fabricated data, claims, or values.

### Invariant 2
All claims must be traceable to an experiment with a pre-registered hypothesis.

### Invariant 3
Statistical results must include effect size and confidence intervals.

## Extensions

<!-- Project-specific invariants go here. Extensions may only add rules, never weaken core invariants. -->

### ollarma project notes

**Posture:** ollarma is a benchmarking + inference substrate, not a publication pipeline. CANON Invariants 1–3 apply, interpreted for this domain as follows.

**Invariant 1 mapping:**
- "No fabricated data" → no synthetic benchmark rows; every `BenchmarkResult` row carries measured Ollama API timing fields (`prompt_eval_count`, `eval_duration`, etc.) from a real inference call.
- Sealed `results/run-*.json` files and hash-chained `PipelineReceipt` entries are the canonical evidence surface.

**Invariant 2 mapping:**
- "Traceable to an experiment with pre-registered hypothesis" → for this substrate, every benchmark claim traces to (a) a pinned model (`models.yml` entry), (b) a task YAML (`tasks/*.yml`), and (c) a sealed result row keyed by `run_id`. The "pre-registration" equivalent is the committed task YAML + model registry entry that existed before the run.
- Pipeline operations (`warmup`/`pin`/`evict`) trace via `PipelineReceipt` hash chain — same invariant holds for operational state changes.

**Invariant 3 — known gap (tracked as `CANON-01` in `.planning/REQUIREMENTS.md`):**
- Current `BenchmarkResult` schema reports `decode_tps` mean and variance per row but does **not** report confidence intervals on summary/selection artifacts.
- Active decision required: either (a) extend `BenchmarkResult` / `ModelSelectionArtifact` with CI fields, (b) amend this section to carve a scoped exception for raw per-row observations while requiring CIs on derived selection artifacts, or (c) promote CI-bearing summary emission to a v4.3 requirement.
- Until resolved, benchmark selection outputs are **technically non-compliant** with Invariant 3. This must be closed before ollarma claims can be used as gsigmad-governed evidence in downstream scientific contexts.

### Scope clarifications

- `results/` at the repo root is shared between ollarma benchmark artifacts (`run-*.json`, `run-*.jsonl`) and the gsigmad scaffold's expected experiment output directory. This is a **known semantic overlap**. No claim files currently collide (ollarma runs use `run-*` prefixes; gsigmad experiments would use `EXP-*` prefixes). If collision risk grows, split into `results/benchmarks/` and `results/experiments/`.
- Ollarma explicitly **does not** own governance runtime policy or control-plane authority (per `.agent/MISSION_ANCHOR.md` forbidden drifts). CANON here governs ollarma's own scientific/benchmark claims; downstream governance attachment flows through Overwatch.
