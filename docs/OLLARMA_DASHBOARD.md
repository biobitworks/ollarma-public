# OLLARMA Dashboard

`ollarma` now exposes a localhost operator dashboard at:

- `http://127.0.0.1:8484/dashboard`

It is the first operator surface for local `ollarma` use. The dashboard is
interactive and execution-aware, but still intentionally narrow:

- bounded helper plus bounded execution only
- owned by `ollarma`
- backed by typed service/read models
- served from the existing Starlette app
- independent of `portfolio-dashboard`

## What it does

- general single-turn chat against the local model
- project-routed grounded help through the retrieval-first `/route` lane
- validated workflow discovery plus guarded `/workflow` submission
- bounded autopilot inventory or `/autopilot` execution
- deterministic operator KB cards with commands, citations, and suggested questions
- readiness guidance that tells you what is still missing before grounded help is likely to work well
- scheduler, KB, and recent run visibility in the same page

## What it shows

- scheduler queue depth and active lane
- interactive chat controls for `general_chat` and `project_route`
- explicit model picker with `Automatic (recommended)` default plus curated local model options
- curated operator KB resources with citations to local docs
- readiness findings such as no registered projects or KBs that are not ready
- per-project KB status, freshness/build state, source counts, and search DB refs
- recent retrieval-first route receipts with lane and reason metadata
- recent workflow runs discovered in registered project execution roots
- recent autopilot runs discovered in registered project execution roots
- checkpoint refs and receipt refs through typed drill-down JSON
- the current bounded execution / integration contract

## JSON surfaces

- `GET /dashboard/overview`
- `GET /dashboard/workflows/{project}`
- `GET /dashboard/runs/{run_id}`
- `POST /workflow`
- `POST /autopilot`

These JSON surfaces are the intended future export/link targets for
`portfolio-dashboard` or any other local portfolio shell. The integration
contract is intentionally narrow: links and exported reads only.

## Interaction modes

The dashboard keeps the answer mode explicit:

- `general_chat`
  Uncited, single-turn local model chat for broad questions.
- `project_route`
  Retrieval-first grounded help for a registered project. This mode returns
  lane metadata and citations/evidence refs when they exist.
- `operator_kb`
  Deterministic dashboard guidance backed by curated local docs, commands, and
  citations.
- `workflow`
  Submit one validated manifest-backed step discovered from the selected
  project's repo-local manifest roots.
- `autopilot`
  Run bounded asset discovery or a policy-driven local execution pass for the
  selected project.

Model selection is also explicit:

- `Automatic (recommended)`
  Preserves the current service behavior. General chat uses validated chat
  selection when available and otherwise uses the bounded fallback path.
  Project-routed help keeps retrieval-first routing and uses the validated
  route model only when grounded synthesis is actually needed.
- `Specific model`
  Forces the selected local Ollama tag for that request. The dashboard now
  surfaces installed models plus the curated benchmark catalog, including
  Gemma-family candidates.

The dashboard must not silently fall back from blocked or stale project routing
to generic chat. If the KB is missing, stale, or insufficiently grounded, the
operator should see that state directly.

## Runtime recovery

Use `GET /health` when the dashboard or helper surfaces feel inconsistent. It
now reports:

- strict `chat` selection readiness
- strict `route_prompt` selection readiness
- whether generic helper chat is using a fallback installed local model

If strict code-lane selection is blocked, the recovery path is:

```bash
ollarma run --suites code --trials 3
ollarma report --run-id <fresh_run_id>
ollarma verify <fresh_run_id>
```

That recovery only restores validated selection. It does not repair missing
project KB declarations in sibling adapters.

Dashboard general chat is still interactive while this is being repaired. If no
validated `code` winner and no installed fallback model are available, `/chat`
returns a structured `blocked` response with the recovery commands above instead
of a generic server error.

## KB integration notes

Sibling repos integrate by declaring adapter-local KB roots and rebuilding the
repo-local `.ollarma/kb` artifact set. The dashboard only consumes those
export-safe read artifacts:

- `manifest.json`
- `documents.jsonl`
- `chunks.jsonl`
- `tags.json`
- `search.sqlite`
- `receipts.jsonl`
- `route_receipts.jsonl`

The dashboard does not create, edit, or approve KB content. It only surfaces
the current read-model state, deterministic operator guidance, and recent
helper-routing receipts.

## What it does not do

- no writeback
- no cross-repo mutation
- no agent REPL with edit/shell tools
- no hidden fallback from blocked project routing to generic uncited chat
- no `adapters_dir` override from the dashboard UI
- no KB build controls from the dashboard UI
- no raw shell or arbitrary file execution surface
- no portfolio control-plane ownership
- no dependency on `portfolio-dashboard`

If an operator needs broad coding, git mutation, or high-confidence
interpretation, escalate to the frontier or human-review lane.
