# Replication And Antigence

## Pinned Baseline

`ollarma`'s preserved replication baseline is the sealed run below:

- `run_id`: `2026-04-09T17:52:14Z`
- `model`: `qwen3:1.7b`
- `suite`: `science`
- `row_count`: `5`
- `unique_raw_responses`: `1`
- `model_digest`: `8f68893c685c`
- `ollama_version`: `ollama version is 0.20.3`
- `prompt_hash`: `f3fea09efdba6e51fa3e39a56b80b912369a350ceb0110dc8ceb5c5a2f2882c5`
- `evidence_root`: `dbb990948aaeaa78361566719197d1b916ecd2ac58a1bbda2bdf5887de319065`

The preserved artifacts are:

- `results/run-2026-04-09T17:52:14Z.json`
- `results/run-2026-04-09T17:52:14Z.evidence.json`

## Verified Baseline Path

Validate the preserved baseline first:

```bash
ollarma verify 2026-04-09T17:52:14Z
```

Validated result on this machine: the command exits `0` and reports a valid evidence chain for 5 receipts.

## Reproduction Command

```bash
ollarma run --models qwen3:1.7b --suites science --trials 5
```

Use the rerun only after the preserved baseline passes verification.

## Comparison Contract

When comparing a rerun to the pinned baseline, compare all of the following:

- `model_digest`
- `ollama_version`
- `prompt_hash`
- uniqueness count of `raw_response`
- `evidence_root`

### Hard Failures

These invalidate the replication claim immediately:

- preserved artifacts are missing
- `ollarma verify 2026-04-09T17:52:14Z` fails
- `prompt_hash` does not match the pinned baseline
- `model_digest` does not match the pinned baseline

### Drift Findings

These should be recorded as environment drift and must not be silently narrated as a successful match:

- `ollama_version` differs
- rerun `raw_response` uniqueness count is not `1`
- rerun `evidence_root` differs from `dbb990948aaeaa78361566719197d1b916ecd2ac58a1bbda2bdf5887de319065`

If drift is present, record the mismatch explicitly and stop short of claiming successful baseline replication.

## How Antigence Fits

Use `ollarma` directly for:

- local inference
- project routing
- benchmark execution
- evidence verification

Use Antigence alongside `ollarma` when you want a second-pass verifier for:

- hallucination checks
- sandbox and prompt-injection checks
- citation, statistics, and methodology review
- post-hoc safety analysis of outputs

Antigence is a complementary verifier and optional sidecar. It is not a required dependency for basic `ollarma` adoption, and this guide does not require a live Antigence entrypoint to reproduce or verify the pinned `ollarma` baseline.
