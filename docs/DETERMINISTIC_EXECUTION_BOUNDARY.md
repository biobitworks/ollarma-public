# Deterministic Execution Boundary

This document answers a narrow operator question:

- can `ollarma` help find a file or determine what script/notebook to run?
- can `ollarma` actually run a Python script, notebook, test suite, or
  pipeline step under strong deterministic guardrails?

The answer is `yes`, but only through the correct surface.

## What `ollarma` can do

### 1. Find files, explain scripts, and suggest the next local step

Use the bounded helper surfaces:

- `ollarma projects --adapters-dir <path>` to discover registered sibling repos
- `POST /chat` for generic single-turn local model help
- `POST /route` or MCP `route_prompt` for bounded project-aware help
- `ollarma kb-status --project <name>` and `ollarma kb-search --project <name>`
  for deterministic read-only retrieval
- `ollarma chat --project <name>` for an interactive local REPL when you want
  repo-aware inspection with tool use
- `GET /dashboard` for an interactive helper and bounded execution dashboard
  over scheduler, KB, manifest modules, and recent run metadata

Good uses:

- locate where a script or notebook lives in a registered repo
- explain what a script does
- inspect local project structure
- recommend which validated workflow, test, or script should run next

Constraint:

- service-mode routing is read-only by default and should be treated as an
  assistant for discovery and triage, not as an execution lane
- service-mode routing is now retrieval-first: it returns either a direct KB
  answer, a bounded grounded synthesis answer, an explicit orchestrator handoff,
  or an explicit escalation receipt
- dashboard interaction must keep the answer mode explicit: generic chat stays
  uncited, project routing stays grounded and single-turn, and deterministic
  operator KB guidance stays separate from model output
- generic HTTP/dashboard chat may use an installed local fallback model when
  validated `chat` selection is missing or stale; strict route, workflow, and
  tool-using execution lanes do not inherit that fallback
- dashboard interaction must not silently fall back from blocked routed help to
  generic chat
- project KB state is now contract-bound and fail-closed: undeclared, missing,
  or unbuilt KB surfaces should return explicit blocked reason codes instead of
  silently widening authority

### 2. Run deterministic local work with guardrails

Use the bounded execution surfaces:

- `ollarma workflow --project <name> --manifest-ref <ref> --step-id <id>`
- `ollarma autopilot <project> --run`
- `POST /workflow`
- `POST /autopilot`
- `ollarma kb-build --project <name>`

Use the operator review surfaces:

- `GET /dashboard` for HTML review
- `GET /dashboard/overview` for typed JSON overview data
- `GET /dashboard/workflows/{project}` for project-scoped manifest discovery
- `GET /dashboard/runs/{run_id}` for typed run drill-downs

These are the correct lanes for deterministic local execution of:

- Python scripts
- Jupyter notebooks
- pytest suites
- Snakemake dry-run pipeline steps

## Strong guardrails already in the code

### Workflow lane

The manifest contract only admits these task classes:

- `validated-script`
- `validated-notebook`
- `validated-pipeline-step`
- `validated-pytest-suite`

It also enforces:

- repo-relative locators only in public payloads
- bounded materialization roots
- protected path rejection
- secret-bearing file rejection
- symlink escape rejection
- checkpoint and escalation policies

### Autopilot lane

The autopilot execution path provides:

- explicit asset discovery
- per-asset routing by type
- subprocess or papermill execution with timeouts
- restricted working directory to the asset parent or project root
- dry-run capable inventory mode
- escalation when dependencies are missing or timeouts occur

Default asset execution classes:

- `script`
- `notebook`
- `pytest_suite`
- `snakefile`

## What `ollarma` should not be trusted to do by default

- broad repo mutation through service-mode routing
- commit / push / backup / mirror workflows
- high-stakes project characterization without verification
- open-ended coding or multi-file refactors
- governance-significant decisions without fallback

Those cases should stay on the frontier lane or on the explicitly governed
interactive workflow.

## Dashboard boundary

The `ollarma` dashboard is an interactive localhost helper and bounded
execution surface. It is meant to make scheduler state, workflow receipts,
checkpoint refs, autopilot run metadata, operator KB guidance, and guarded
execution entrypoints reviewable without widening mutation authority.

Keep these constraints:

- `portfolio-dashboard` is not required for `ollarma` dashboard runtime
- future portfolio integration is links/export surfaces only
- the dashboard must not become a general remote execution or writeback surface
- the dashboard may call bounded helper lanes such as `/chat` and `/route`, and
  it may expose bounded `/workflow` and `/autopilot` controls
- the dashboard must not expose KB build, edit, shell mutation, or arbitrary
  file-execution controls
- the dashboard must not expose `adapters_dir` or absolute project-root details
  in its normal UI
- review happens when the local `ollarma` dashboard is available, not when
  cross-dashboard federation is solved

## KB boundary

The deterministic KB contract is repo-local and read-only:

- adapters declare KB roots and freshness policy
- `.ollarma/kb` is the default repo-local artifact root
- `search.sqlite` is the read-only local search model materialized beside the
  JSON artifacts
- `canonical` and `reference` sources are distinct
- blocked KB contract states currently include:
  - `KB_SOURCES_UNDECLARED`
  - `KB_SOURCE_MISSING`
  - `KB_NOT_BUILT`
- stale KB state adds:
  - `KB_STALE`

Read-only retrieval surfaces are now:

- CLI `kb-status` and `kb-search`
- HTTP `GET /kb/status/{project}` and `POST /kb/search`
- MCP `kb_status` and `kb_search`

If `stale_behavior=block`, search must fail closed with `KB_STALE`. If
`stale_behavior=escalate`, search may return hits but must preserve `status:
stale` for the caller.

## Retrieval-first helper routing

`POST /route` and MCP `route_prompt` now follow this order:

1. classify the query
2. inspect KB status and deterministic retrieval evidence
3. choose one lane:
   - `kb_direct`
   - `grounded_local_synthesis`
   - `orchestrator_handoff`
   - `frontier_or_human`
4. return a machine-readable route receipt with lane, reason code, KB status,
   evidence count, and next action

Direct KB answers are only allowed when the query class is exact-answer
eligible and the KB is fresh enough. Stale, missing, or ungrounded queries do
not silently fall back to free-form helper answers.

## Practical routing rule

Use this decision table:

- `Need to find a file or understand a repo?`
  Use `POST /route`, MCP `route_prompt`, or `ollarma chat --project`.
- `Need to run a script/notebook/test/pipeline with deterministic bounds?`
  Use `ollarma workflow`, `ollarma autopilot --run`, or the dashboard
  execution panel.
- `Need to materialize or rebuild deterministic KB artifacts?`
  Use `ollarma kb-build --project`.
- `Need bounded retrieval over the materialized KB?`
  Use `kb-status` or `kb-search` over CLI, HTTP, or MCP.
- `Need a routed helper answer?`
  Use the dashboard project-routed mode, `POST /route`, or MCP `route_prompt`
  and inspect the returned
  `lane`, `reason_code`, and `route_receipt`.
- `Need to inspect queue state, receipts, checkpoints, or recent runs?`
  Use `GET /dashboard`, `/dashboard/overview`,
  `/dashboard/workflows/{project}`, or `/dashboard/runs/{run_id}`.
- `Need generic single-turn help with no grounding guarantee?`
  Use the dashboard general-chat mode or `POST /chat`.
- `Need to recover from missing or stale code-suite selection artifacts?`
  Run `ollarma run --suites code --trials 3`, then
  `ollarma report --run-id <fresh_run_id>`, then
  `ollarma verify <fresh_run_id>`.
- `Need broad coding, git mutation, or high-confidence interpretation?`
  Escalate to Codex or Claude.

## Recovery admission gate (v4.3)

The bounded execution lanes now admit-check recovery state **before** doing
any work. Entrypoints covered:

- `POST /route` and MCP `route_prompt` (helper lane)
- `POST /workflow` and CLI `ollarma workflow` (validated workflow lane)
- `POST /autopilot` and CLI `ollarma autopilot --run` (autopilot lane)
- `POST /agents/{name}/run` and MCP `run_agent` (typed-agent lane)

When the repo is stranded (sidecar worktree with uncommitted changes, sidecar
branch ahead of base, or broad sweep required), the gate **fails closed**
and the entrypoint returns a structured `RECOVERY_REQUIRED` error containing
a `blocker_code` and `next_fix_commands[]`. No inference, no tool use, no
workflow dispatch — the operator must resolve the flagged state first.

Admission is cached 30 seconds per repo root. The ollarma server does not
hold a lock; concurrent requests during a block window will all observe
the same cached state.

Full contract: `docs/RECOVERY_PROTOCOL.md`. Scan on demand with
`ollarma recover scan`, inspect with `ollarma recover report`.

For programmatic consumption (Python imports, receipt schema pledges, HTTP/CLI surface stability), see [docs/OLLARMA_SUBSTRATE_CONTRACT.md](./OLLARMA_SUBSTRATE_CONTRACT.md).

## Live stress-test conclusion

On 2026-04-10, a bounded `/route` trial against the `Overwatch` adapter
completed within the timeout gate but returned a generic and incorrect summary
for the real local `portfolio-dashboard` repo.

That means:

- the helper lane is operational
- timeout-gated local attempts are reasonable
- semantic correctness must still be checked
- fallback to Codex or Claude is required when the response is generic,
  incorrect, blocked, or too high-stakes to trust
