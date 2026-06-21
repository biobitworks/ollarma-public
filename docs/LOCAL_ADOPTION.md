# Local Adoption Guide

This is a local operator guide for the current workstation. It assumes sibling repos live under `<local-path>` and keeps machine-specific details out of the generic repo entry points.

## Install Modes

Base CLI only:

```bash
python -m pip install -e .
```

MCP + HTTP + routing surfaces on this machine:

```bash
python -m pip install -e '.[mcp,http,fleet]'
```

Expected signal: `ollarma --help` works and `ollarma projects` does not fail with missing extras.

## Adapter Discovery

Project routing depends on adapter discovery. `ollarma` resolves adapters in this order:

1. `--adapters-dir`
2. `OLLARMA_ADAPTERS_DIR`
3. `~/.config/ollarma/config.toml`
4. built-in default

Current local default:

```bash
export OLLARMA_ADAPTERS_DIR=<repo>/adapters
```

Check the registry:

```bash
ollarma projects --adapters-dir "$OLLARMA_ADAPTERS_DIR"
```

Expected signal: a table of registered projects appears, including `Overwatch` on this machine.

## KB Contract

Phase 21 adds the deterministic KB declaration contract for adapters.

- adapter-owned KB metadata now lives under `knowledge_base`
- the default repo-local artifact root is `.ollarma/kb`
- KB source paths must resolve under the adapter `project_root`
- undeclared, missing, or unbuilt KB state must fail closed with explicit
  reason codes instead of silent helper answers

Contract reference:

- `docs/OLLARMA_KB_CONTRACT.md`

Initial build command:

```bash
ollarma kb-build --project <name> --adapters-dir "$OLLARMA_ADAPTERS_DIR"
```

Read-only KB inspection:

```bash
ollarma kb-status --project <name> --adapters-dir "$OLLARMA_ADAPTERS_DIR"
ollarma kb-search --project <name> --query "manifest" --limit 5 --adapters-dir "$OLLARMA_ADAPTERS_DIR"
```

## Project Helper Mode

Use `ollarma` as the first stop for bounded local help that does not need a frontier coding agent:

- use `POST /chat` for generic script/log/explanation requests
- use `POST /route` or MCP `route_prompt` when the task needs a registered sibling-project adapter and you want a retrieval-first answer or explicit handoff receipt
- use `ollarma chat --project <name>` when the operator wants an interactive local REPL

The copy-paste prompt and escalation boundary live in `docs/OLLARMA_PROJECT_HELPER_PROMPT.md`.

If the operator needs the precise execution boundary for "find a file", "tell me which script to run", "run this Python script", or "run this notebook with guardrails", use:

- `docs/DETERMINISTIC_EXECUTION_BOUNDARY.md`
- `docs/DETERMINISTIC_EXECUTION_POLICY.json`

For commit / push / external-drive mirror workflows, use `docs/OLLARMA_BACKUP_PUSH_MIRROR_PROMPT.md`. That path is intentionally separate because it belongs on the interactive `ollarma chat --project Overwatch` lane rather than the read-only service surfaces.

## File Finding Vs Execution

Use the narrowest surface that matches the task:

- `Need to find where a file lives in a registered project?`
  Use `POST /route`, MCP `route_prompt`, or `ollarma chat --project <name>`.
- `Need to decide which script, notebook, test, or workflow should run next?`
  Use the same helper lane first, but verify the answer before trusting it.
- `Need to actually run a script or notebook under deterministic guardrails?`
  Use `ollarma workflow` for validated manifest-backed work or
  `ollarma autopilot <project> --run` for bounded asset execution.

Do not treat helper routing as a replacement for the bounded execution lanes.
Do not treat KB build as a helper-side effect either; it is an explicit local
operator action.

Helper routing now returns machine-readable metadata:

- `lane`: `kb_direct`, `grounded_local_synthesis`, `orchestrator_handoff`, or `frontier_or_human`
- `reason_code`: stable route outcome code such as `KB_DIRECT_ANSWER`,
  `GROUNDED_LOCAL_SYNTHESIS`, `EXECUTION_LANE_REQUIRED`, or
  `INSUFFICIENT_GROUNDED_EVIDENCE`
- `route_receipt`: compact receipt with query class, KB status, evidence count,
  next action, and selected model when synthesis occurred

## Bounded Manifest-Driven Workflow Adoption

Phase 19 narrows sibling-repo adoption to validated routine work only. The intended consumer contract is manifest-driven: sibling repos provide validated manifests, validation specs, and summary schemas through the bounded workflow surface as that surface lands, rather than treating `chat`, `route`, or MCP project routing as broad remote mutation tools.

Use this ownership split when preparing an adopter:

- `ollarma` owns bounded runtime behavior, queueing, retry/checkpoint behavior, generic summary references, and handoff manifests.
- The consumer repo owns its domain-specific manifest contents, validation specs, summary schemas, compact summaries tied to domain outputs, and downstream bundles.
- Public workflow references should stay export-safe: stable ids, digests, or repo-relative locators. Do not assume an absolute-path workflow API.

The dedicated workflow submission surface now ships through CLI, HTTP, and the
dashboard execution panel. Keep helper surfaces for read-oriented help and use
workflow/autopilot only when you intend deterministic local execution.

## Immediate next executable wave set

The first bounded consumer path is manifest-backed validated workflow submission for routine local stages only:

- provide a validated manifest, validation contract, summary schema, and adapter-scoped execution roots
- expect append-only receipts plus a mutable checkpoint file under a dedicated execution subtree
- keep all public workflow references export-safe: stable id, digest, or repo-relative ref only
- use `ollarma` for bounded runtime behavior, handoff metadata, and generic summary refs only
- keep domain summaries, interpretation, and downstream bundles in the science repo

Dependency contract for the immediate wave:

- `gsigmad`: adapter discovery and sibling-repo workflow governance
- `overwatch`: control-plane and local operator coordination surface
- `seedgraph`: bounded downstream consumer of generic summary refs and handoff manifests
- `antigence`: anomaly/contradiction signals that can force `frontier-only` escalation
- science-repo adapters: own validated manifests, validation specs, summary schemas, domain summaries, and downstream bundles

## HTTP Surface

Start the local API:

```bash
ollarma serve
```

Expected signal: startup output mentions `127.0.0.1:8484` and `/docs`.

The same local server now exposes the interactive operator dashboard at
`http://127.0.0.1:8484/dashboard`. This is the intended human review point for
Phase 28: inspect scheduler state, recent workflow runs, recent autopilot runs,
operator KB guidance, readiness findings, bounded helper interaction, and
guarded execution there before considering any later portfolio-dashboard links.

Dashboard interaction modes:

- `general_chat` for uncited single-turn local model questions
- `project_route` for grounded project help with route receipts and citations
- `workflow` for manifest-backed validated step submission
- `autopilot` for bounded asset discovery or policy-driven local execution
- deterministic operator KB cards for commands, docs, and "what am I missing?"
  guidance

Constraint: the dashboard is not helper-only anymore, but it is still bounded.
It may submit workflow/autopilot requests, but it does not trigger KB builds,
expose adapter override paths, or open the broad tool-using REPL lane.

Selection recovery:

- `GET /health` now reports whether strict `chat` and `route_prompt` selection
  are ready or blocked, plus whether generic helper chat is using a fallback
  model.
- If strict code-lane selection is blocked because the latest artifact is
  missing, stale, or lacks `code` suite coverage, recover with:

```bash
ollarma run --suites code --trials 3
ollarma report --run-id <fresh_run_id>
ollarma verify <fresh_run_id>
```

- Generic HTTP/dashboard chat may still work during that window by using an
  installed local fallback model. If no fallback model is reachable, `/chat`
  returns a structured `blocked` response with recovery commands instead of a
  generic dashboard failure.
- `/route`, workflow submission, and other strict code lanes remain fail-closed.

Single-turn chat:

```bash
curl -X POST http://127.0.0.1:8484/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Summarize the current ollarma surface.","model":"qwen3:1.7b"}'
```

Project routing with a known local sibling:

```bash
curl -X POST http://127.0.0.1:8484/route \
  -H "Content-Type: application/json" \
  -d '{"project":"Overwatch","prompt":"Summarize Overwatch as a control-plane repo."}'
```

Generic sibling-repo routing:

```bash
curl -X POST http://127.0.0.1:8484/route \
  -H "Content-Type: application/json" \
  -d '{"project":"<registered-project>","prompt":"Describe your local role in one paragraph."}'
```

Expected signal: JSON includes the resolved `project`, a `final_response`, and a non-negative `tool_calls_count`.
For routed helper calls, also expect `lane`, `reason_code`, and `route_receipt`.

Workflow manifest discovery:

```bash
curl http://127.0.0.1:8484/dashboard/workflows/<project>
```

Workflow submission:

```bash
curl -X POST http://127.0.0.1:8484/workflow \
  -H "Content-Type: application/json" \
  -d '{"project":"<registered-project>","manifest_ref":{"repo_relative":".ollarma/manifests/workflow.json"},"step_id":"execute-script"}'
```

Autopilot inventory:

```bash
curl -X POST http://127.0.0.1:8484/autopilot \
  -H "Content-Type: application/json" \
  -d '{"project":"<registered-project>","run_assets":false}'
```

Dashboard overview JSON:

```bash
curl http://127.0.0.1:8484/dashboard/overview
```

KB status JSON:

```bash
curl http://127.0.0.1:8484/kb/status/<project>
```

KB search JSON:

```bash
curl -X POST http://127.0.0.1:8484/kb/search \
  -H "Content-Type: application/json" \
  -d '{"project":"<registered-project>","query":"manifest","limit":5}'
```

Run drill-down JSON:

```bash
curl http://127.0.0.1:8484/dashboard/runs/<run-id>
```

Expected signal: dashboard payloads stay bounded, return scheduler plus
receipt/checkpoint metadata, and do not depend on `portfolio-dashboard`.

## MCP Surface

Start the stdio server:

```bash
ollarma mcp
```

Expected signal: the process starts without an import error and stays attached to stdio until the client exits.

Minimal local registration:

```json
{
  "mcpServers": {
    "ollarma": {
      "command": "ollarma",
      "args": ["mcp"],
      "env": {}
    }
  }
}
```

`route_prompt`, `kb_status`, and `kb_search` are part of the bounded service-mode MCP surface. They reuse the same safe project-routing and read-only KB paths as HTTP and do not expose general remote edit or shell execution. `route_prompt` is retrieval-first and returns lane/receipt metadata instead of silently widening authority.

## Smoke Checklist

Run these in order:

1. `ollarma projects --adapters-dir "$OLLARMA_ADAPTERS_DIR"`
2. `ollarma verify 2026-04-09T17:52:14Z`
3. `ollarma serve`
4. open `http://127.0.0.1:8484/dashboard`
5. `curl` `POST /chat`
6. `curl` `POST /route`
7. `curl` `GET /kb/status/<project>`
8. `curl` `POST /kb/search`
9. `ollarma mcp`

Minimum success signals:

- `ollarma projects` lists local adapters instead of reporting none found
- `ollarma verify 2026-04-09T17:52:14Z` exits `0`
- the dashboard loads at `/dashboard` and shows recent run metadata without a portfolio-dashboard dependency
- `POST /chat` returns JSON with a model response
- `POST /route` returns JSON for the requested project plus `lane`, `reason_code`, and `route_receipt`
- `GET /kb/status/<project>` returns deterministic KB readiness plus counts
- `POST /kb/search` returns bounded read-only hits or an explicit blocked reason
- `ollarma mcp` starts cleanly with no missing dependency error

## Persistent Service Baseline (v4.5 Phase 51)

### Install the LaunchAgent

```bash
ollarma start --install
```

The hardened installer (Plan 51-01):
1. Copies the repo plist to `~/Library/LaunchAgents/com.byron.ollarma.plist`.
2. Runs `plutil -lint` against the installed plist (refuses to continue on bad XML).
3. Creates `~/Library/Logs/ollarma/`.
4. Runs `launchctl bootout gui/<uid>/com.byron.ollarma` (tolerates 113/3 "not loaded").
5. `launchctl bootstrap gui/<uid> <plist>` — loud failure on non-zero.
6. `launchctl enable` + `kickstart -kp` to force-start.
7. `launchctl list` + `print` for operator verification.

All subprocess calls use `shell=False` and a 10-second timeout. The plist
now includes `ThrottleInterval=10` (deterministic crash-loop backoff) and
`ProcessType=Interactive` (so macOS doesn't throttle the operator daemon).

### Startup readiness contract

Every HTTP request to the service includes — or can fetch — a
deterministic `StartupReadinessPayload` (schema_version=1):

```bash
# Top-level folded status + embedded readiness
curl -sS http://127.0.0.1:8484/health | jq '.status, .startup_readiness.status'

# Just the readiness payload
curl -sS http://127.0.0.1:8484/startup/readiness | jq
```

Payload shape (all fields stable across v4.5):

| Field | Value |
|-------|-------|
| `schema_version` | 1 |
| `service_label` | `com.byron.ollarma` |
| `generated_at` | UTC ISO-8601 |
| `status` | one of `ready`, `degraded`, `blocked` (severity: blocked > degraded > ready) |
| `checks` | per-probe `{name, status, detail, reason_code}` — model_availability, swap_posture, admission_posture, pipeline_posture |
| `model_availability` | helper model resolution posture + `reason_code` (`MODEL_UNAVAILABLE` when helper is blocked) |
| `swap` | `swap_used_mb`, `threshold_mb`, `reason_code` (`SWAP_DEGRADED` above threshold, `SWAP_UNKNOWN` if telemetry unavailable) |
| `admission` | `{enabled: bool}` — reflects `OLLARMA_RECOVERY_ADMISSION` |
| `pipeline` | loaded-model count + names + telemetry source (read-only reporting; Phase 52 adds policy) |
| `next_fix_commands` | deterministic operator commands when `status != "ready"` |

Persisted copy: `.ollarma/startup/readiness.json` (refreshed once on service start, rewritten atomically with `OPT_SORT_KEYS | OPT_INDENT_2` for byte-deterministic output).

### Loud-on-degraded

`/health`'s top-level `status` is now `ready`, `degraded`, or `blocked` — **never `"ok"`**. The legacy `"ok"` value is gone precisely so a degraded startup can't hide. The dashboard readiness panel surfaces a top-level item (`"Startup readiness is degraded"` or `"... blocked"`) with the exact `next_fix_commands` to resolve it.

### Startup smoke command

After login or restart, validate the full persistent-service chain with:

```bash
ollarma startup-smoke
```

Exit code 0 means ready or degraded; exit code 1 means blocked, HTTP unreachable, or launchd service missing. Probes: `launchctl list`, `/health`, `/startup/readiness`, `/models/status`, `/metrics`. Never mutates launchd state.

### Log paths

```
~/Library/Logs/ollarma/stdout.log
~/Library/Logs/ollarma/stderr.log
```

### Full persistence operating model

The complete v4.5 operating model — install sequence, startup verification,
GPU residency policy (Phase 52), routing ladder (Phase 53), live proof
reproduction (Phase 54, deferred), and troubleshooting — is documented in:

- `docs/OLLARMA_PERSISTENCE_PLAYBOOK.md`

## Recovery (v4.3)

ollarma now ships a deterministic recovery subsystem that detects stranded
work (uncommitted sidecar worktrees, sidecar branches ahead of base, leftover
scribe artifacts) and **blocks fresh bounded execution** when the repo is not
clean. This protects against silent context loss after a token-limit crash,
agent disappearance, or interrupted sidecar session.

Full protocol reference: `docs/RECOVERY_PROTOCOL.md`.
Copy-pasteable operator walkthrough: `docs/OPERATOR_RECOVERY_PLAYBOOK.md`.
Sample packet + receipt (redacted reference copies): `docs/examples/recovery/`.
Artifact hygiene (commit vs ignore vs mirror): `docs/OLLARMA_BACKUP_PUSH_MIRROR_PROMPT.md` §Recovery Artifacts.

### Quick scan

```bash
# Summary table; exit 1 if anything non-clean.
ollarma recover scan

# Print the most recent packet without re-scanning.
ollarma recover report

# JSON for piping.
ollarma recover scan --json
```

Packets persist under `.ollarma/incidents/` with a `latest.json` pointer.

### HTTP

```bash
curl http://127.0.0.1:8484/recovery/status
curl -X POST http://127.0.0.1:8484/recovery/scan -H "Content-Type: application/json" -d '{}'
```

### MCP

Two tools registered on the ollarma MCP server:
- `scan_recovery_state(project_root, base_branch="main")` — runs scan + writes packet
- `read_recovery_report(project_root)` — returns latest packet (read-only)

### Admission control

Before `route_prompt`, `submit_workflow`, `submit_autopilot`, and `run_agent`
run, ollarma scans recovery state (30s TTL cache) and **blocks with
`RECOVERY_REQUIRED` + exact fix commands** if a non-clean state is detected.

Blocker codes: `STRANDED_WORKTREE`, `POSSIBLE_WORK_LOSS`,
`RECOVERY_SWEEP_REQUIRED`. `clean` and `RESUME_AVAILABLE` admit.

Each block appends a receipt to `.ollarma/recovery_block_receipts.jsonl` for
governance systems to observe.

### Opt-out (dry-run / tests only)

```bash
export OLLARMA_RECOVERY_ADMISSION=off
# and separately, to silence auto-scribe hooks:
export OLLARMA_SCRIBE_HOOKS=off
```

Default is fail-closed — never ship these env vars in production.

## Dashboard-First Boundary

`portfolio-dashboard` stays a separate portfolio shell. The local operator
review surface for `ollarma` now lives here first:

- `ollarma` owns the localhost dashboard, typed read models, scheduler view,
  workflow/autopilot run summaries, checkpoint refs, and receipt refs.
- `portfolio-dashboard` may link to or consume exported read surfaces later,
  but it is not required for local dashboard runtime or rendering.
- The future integration contract is links and export-safe reads only. Do not
  couple `ollarma` dashboard availability to a shared UI shell, merged repo
  ownership, or portfolio registry dependency.

If routing fails, re-check `OLLARMA_ADAPTERS_DIR` or pass `--adapters-dir` explicitly. If MCP or HTTP fail to start, confirm the `mcp` and `http` extras were installed.
