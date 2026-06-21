# SWE-bench Lite — operator live-run instructions (Phase 61-01)

This document is the live-run recipe for **EXP-1.2 (CONFIRMATORY)**, which is
pre-registered in `.gsigmad/experiments/EXP-1.2.yaml`. Phase 61-01 ships the
substrate (dataset fetch + subset selector + local-lane runner + hash-chained
per-run receipts); the measured pass@1 claim is produced by the **operator**
running the commands below and is signed into `.gsigmad/LAB_NOTEBOOK.md`
only after the live run completes.

## Preconditions

- **Local lane:** enough swap headroom for the chosen local model (autopilot
  returns `SWAP_DEGRADED` and the runner records `skipped_swap` when pressure
  is above threshold — this is invariant I-02, not a bug).
- **Frontier lane (Phase 62+):** `ANTHROPIC_API_KEY` installed in the
  operator Keychain via `security add-generic-password -s ollarma-anthropic
  -a $USER -w` (mirrors `ollarma gateway smoke-anthropic` setup).

## Commands

```bash
# 1. Fetch + cache SWE-bench Lite. Pinned SHA-256 sidecar is written next to
#    the JSONL. Re-running is idempotent; pass --force to refetch.
ollarma swe-bench fetch

# 2. Run the local lane over a subset. Three subset forms are supported:
#      first-N
#      ids=ID1,ID2,...
#      random-seed=SEED,count=N
ollarma swe-bench run --lane local --subset first-10 --project swe-bench-demo

# 3. Inspect cache + latest run.
ollarma swe-bench status

# 4. Trace a receipt chain (reuses Phase 57.1-03 gateway chain walker).
ollarma receipts trace <escalation_receipt_id>
```

## What the live run produces

Each invocation of `ollarma swe-bench run` writes:

- `.ollarma/benchmarks/swe-bench-lite/runs/<run_id>/receipts.jsonl` — a
  hash-chained append-only receipt stream (same `canonical_hash` primitive as
  the gateway receipts; parent-hash links form a tamper-evident chain).
- One `SWEBenchRun` record per problem, returned to the CLI and rendered as a
  Rich table. Status is one of `passed`, `failed`, `skipped_swap`,
  `skipped_timeout`, `errored`.

The lab-notebook entry signed by the operator after the live run MUST include:

- The run_id of every run included in the measurement.
- The evidence root of each `receipts.jsonl` chain.
- The pass@1 numerator/denominator computed over the non-skipped rows.
- A link back to `.gsigmad/experiments/EXP-1.2.yaml`.

## Frontier lane (Phase 62-01)

Phase 62-01 wires `FrontierLaneRunner` through the v5.0 gateway + provider
stack. The CLI defaults to `--no-live`, which short-circuits before any
provider HTTP call — tests only ever exercise this path. A real measured
run requires `--live` and a real Anthropic key in the Keychain.

```bash
# Dry-gated exit: the CLI loads problems, resolves the subset, and exits
# before contacting the provider. Useful for validating plumbing in CI.
ollarma swe-bench run \
    --lane frontier \
    --subset first-10 \
    --project swe-bench-demo \
    --provider anthropic \
    --model claude-haiku-4-5-latest \
    --virtual-key-id vk_anthropic \
    --no-live

# Operator live-run (pass@1 measurement). This is the only command that
# contacts Anthropic; the test suite never invokes it.
ollarma swe-bench run \
    --lane frontier \
    --subset first-300 \
    --project swe-bench-demo \
    --provider anthropic \
    --model claude-haiku-4-5-latest \
    --virtual-key-id vk_anthropic \
    --live

# After both lanes have run, build the leaderboard + verify the chain:
ollarma swe-bench leaderboard <run_id>
```

Invariant I-02 (no silent fallback): if the frontier provider fails, the
per-problem `SWEBenchRun` carries `status=failed` with the provider's
`reason_code` — the runner never re-dispatches on the local lane.
