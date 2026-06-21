# local-model-bench

### ollarma as a bounded local-execution substrate

- **Local-first Ollama helper + bounded execution** — autopilot runs only
  validated asset classes (notebook / script / pytest suite / pipeline step),
  never broad shell passthrough. See
  [docs/DETERMINISTIC_EXECUTION_BOUNDARY.md](docs/DETERMINISTIC_EXECUTION_BOUNDARY.md).
- **Receipt-chain audit** — admission, route, gateway, and recovery decisions
  each emit a typed, hash-chained JSONL receipt under `.ollarma/`. Operators
  can walk the chain with `ollarma receipts trace <escalation_receipt_id>`.
- **Persistent via launchd + startup readiness + GPU residency** — the
  substrate produces the same evidence from a chat session, a cron invocation,
  or a post-reboot launchd run. See
  [docs/OLLARMA_PERSISTENCE_PLAYBOOK.md](docs/OLLARMA_PERSISTENCE_PLAYBOOK.md).
- **Programmatic consumption surface for sibling projects** — stable HTTP
  endpoints, CLI verbs, Python module imports, and `schema_version=1` receipt
  schemas. See
  [docs/OLLARMA_SUBSTRATE_CONTRACT.md](docs/OLLARMA_SUBSTRATE_CONTRACT.md) and
  [docs/LOCAL_ADOPTION.md](docs/LOCAL_ADOPTION.md).
- **Bounded bridge between operator UIs and providers** — Ollarma sits between
  ChatGPT Codex, Claude Code, local Ollama, and future provider adapters
  (Gemini, Grok — **not live**). Ollarma owns local runtime receipts;
  Watchtower observes; Overwatch governs portfolio truth; gsigmad governs
  science / EXP / KG. See
  [docs/MULTI_AGENT_BRIDGE_CONTRACT.md](docs/MULTI_AGENT_BRIDGE_CONTRACT.md),
  [docs/PROVIDER_ROADMAP.md](docs/PROVIDER_ROADMAP.md), and
  [docs/SWARM_PROOF_RUNBOOK.md](docs/SWARM_PROOF_RUNBOOK.md).

For historical context + benchmarking-harness usage:
a reusable benchmarking harness for evaluating local Ollama models on
Biobitworks workloads — science reasoning, code generation, and swarm
orchestration. Produces scored, reproducible outputs that can be rerun as
models evolve, plus a model selection guide derived from those results. Runs
entirely offline on Apple M1 Pro 16GB.

## Quick Start

```bash
pip install -e .

# List available models and tasks
bench list

# Dry run (one model x one task, no guards, one result row)
bench run --dry-run

# Full benchmark run (all models x all tasks x 3 trials)
bench run

# Filtered run — single model, single suite
bench run --models qwen3:1.7b --suites science --trials 3

# Override context window size
bench run --num-ctx 8192

# Start the local HTTP surface for other projects
ollarma serve

# Single-turn chat over HTTP
curl -X POST http://127.0.0.1:8484/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Summarize the latest benchmark winner."}'

# Route a prompt through a registered project adapter
curl -X POST http://127.0.0.1:8484/route \
  -H "Content-Type: application/json" \
  -d '{"project":"example-project","prompt":"Describe your local role in one paragraph."}'
```

For programmatic consumption from a sibling repo, see [docs/OLLARMA_SUBSTRATE_CONTRACT.md](docs/OLLARMA_SUBSTRATE_CONTRACT.md).

## Autonomous Start

If you are resuming the repo for milestone work, read [AUTONOMOUS_START.md](<repo>/AUTONOMOUS_START.md) first. It tells you when to use `$gsd-next` versus `$gsd-autonomous` from the current v4.0-complete state.

## Service Surfaces

- `ollarma mcp` exposes MCP tools for Claude Code and other MCP clients, including:
  - `chat_with_model` for single-turn local-model chat
  - `route_prompt` for bounded project-scoped routing through the safe service-mode tool surface
  - `scan_recovery_state` and `read_recovery_report` for the recovery engine (v4.3)
- `ollarma serve` exposes the local HTTP API on `127.0.0.1:8484` with benchmark endpoints plus:
  - `POST /chat` for single-turn model chat
  - `POST /route` for project-scoped routing through fleet adapters
  - `GET /recovery/status` and `POST /recovery/scan` for the recovery engine (v4.3)
- `ollarma recover scan` and `ollarma recover report` — CLI recovery surface (v4.3)
- `ollarma chat --project <name>` remains the interactive CLI path when you want a local REPL with fleet tools.

### Recovery Engine (v4.3)

Before high-value entrypoints (`route_prompt`, workflow, autopilot, agents) run, ollarma checks the repo for stranded work from interrupted agent/model sessions and fails closed with a structured `RECOVERY_REQUIRED` blocker + exact fix commands. See `docs/RECOVERY_PROTOCOL.md` for the full contract and `docs/LOCAL_ADOPTION.md` for operator usage.

Service-mode routing is intentionally narrower than interactive CLI fleet chat. It is read-only by default and does not expose general remote edit/execute tools.

If the operator question is specifically "can Ollarma find a file in a project or run a Python script / notebook with deterministic guardrails?", use the execution-boundary guide in `docs/DETERMINISTIC_EXECUTION_BOUNDARY.md`. The short answer is:

- file finding and repo triage: yes, through `projects`, `/route`, MCP `route_prompt`, or `chat --project`
- deterministic local execution: yes, through `workflow` and `autopilot --run`
- broad coding / git mutation / high-stakes interpretation: no, escalate

## Local Operator Docs

The generic repo surface stays in this README. Machine-local operator guidance lives in:

- `docs/LOCAL_ADOPTION.md` for install, MCP, HTTP, routing, and smoke-test commands on this workstation
- `docs/DETERMINISTIC_EXECUTION_BOUNDARY.md` for the exact helper-vs-execution boundary and fallback policy
- `docs/DETERMINISTIC_EXECUTION_POLICY.json` for the machine-readable version of that policy
- `docs/OLLARMA_BACKUP_PUSH_MIRROR_PROMPT.md` for the external-drive backup / push / mirror workflow that Overwatch governs
- `docs/OLLARMA_PROJECT_HELPER_PROMPT.md` for a copy-pasteable bounded-helper prompt for sibling repos
- `docs/OLLARMA_TEST_PROMPT.md` for a copy-pasteable downstream testing brief
- `docs/REPLICATION_AND_ANTIGENCE.md` for the pinned baseline contract and the Antigence-sidecar fit

## CLI Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--dry-run` | flag | False | One model x one task, skips guards, writes one row |
| `--models` | text (repeat) | all | Filter to specific model names |
| `--suites` | text (repeat) | all | Filter to suites: `code`, `science`, `swarm` |
| `--trials` | int >=1 | 3 | Trials per model x task pair |
| `--num-ctx` | int | task YAML value | Override context window size for all tasks |

## Seed Determinism

Seed determinism was validated on April 9, 2026 on M1 Pro with Ollama 0.19 MLX.

**Configuration:** `seed=42`, `temperature=0.0` (hardcoded in harness/executor.py)

**Offline structure test:** `pytest tests/test_runner.py::TestDeterminism -q -m "not live"`
verifies that `seed` and `temperature` are passed to the Ollama API. Always green without Ollama.

**Empirical validation (requires live Ollama):**

```
bench run --models qwen3:1.7b --suites science --trials 5
```

Inspect sealed `results/run-*.json` and compare `raw_response` across the 5 trial rows
for the science task.

**Finding:** validated on 2026-04-09
- Trials: 5
- Model: qwen3:1.7b (smallest model, fastest for determinism check)
- Suite: science (`science_triage_01`)
- Identical raw_response: 5/5
- Variance: none observed

**Implication:** The current harness configuration (`seed=42`, `temperature=0.0`) produced byte-identical model output across 5 live science trials on this machine.

**Live pytest test (requires Ollama):**

```
pytest tests/test_runner.py::TestDeterminism::test_determinism_live -m live -v
```

## TTFT (Time to First Token)

TTFT is not a stored field in BenchmarkResult. It is derivable from existing fields:

```
ttft_ms = (1 / prefill_tps) * 1000
```

Where `prefill_tps = prompt_eval_count / (prompt_eval_duration / 1e9)` from the
Ollama API response. If `prefill_tps` is `None` (prompt_eval_duration == 0 on
very short prompts), TTFT cannot be computed for that row.

This formula is the authoritative derivation for Phase 5 reporting (REPT-01).
Phase 5 MUST use this formula — do not add a `ttft_ms` field to BenchmarkResult
without updating all existing sealed JSON files.

## Results Format

Active run: `results/run-{timestamp}.jsonl` (one JSON object per line, binary append)
Sealed run: `results/run-{timestamp}.json` (JSON array, written after completion)

Both files are preserved after sealing — the `.jsonl` is kept for partial run recovery.

## Test Suite

```bash
# Offline tests only (default — no Ollama required)
pytest tests/ -q -m "not live"

# Run live integration tests (requires Ollama)
pytest tests/ -m live -v
```

Use the commands above instead of relying on a fixed test-count snapshot; the suite changes as phases land.

## Hardware Requirements

- Apple M1 Pro, 16GB unified memory
- Ollama 0.19+ (MLX backend for Apple Silicon)
- Python 3.11+
- No internet required at benchmark runtime
