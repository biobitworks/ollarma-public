# Ollarma — Dashboard Surfaces

Ollarma already exposes an operator dashboard. This file is the receipt that points at
the canonical document and the live URL; it does not duplicate the content.

> **Canonical operator-dashboard doc:** [`OLLARMA_DASHBOARD.md`](OLLARMA_DASHBOARD.md).
> Edit the canonical doc, not this receipt.

## Live runtime

When ollarma is running locally (default port 8484):

| Surface | URL |
|--------|-----|
| Operator dashboard | `http://127.0.0.1:8484/dashboard` |
| Health | `http://127.0.0.1:8484/health` |
| Receipts trace CLI | `ollarma receipts trace <escalation_receipt_id>` |

The dashboard is served from the existing Starlette app in this repo. There is no
separate `dashboard/` directory or SPA build.

## What the dashboard does (per `OLLARMA_DASHBOARD.md`)

- Single-turn chat against the local model.
- Project-routed grounded help via the retrieval-first `/route` lane.
- Validated workflow discovery + guarded `/workflow` submission.
- Bounded autopilot inventory + `/autopilot` execution.
- Deterministic operator KB cards (commands, citations, suggested questions).
- Readiness guidance (what is still missing before grounded help works well).
- Scheduler / KB / recent-run visibility on one page.

## Boundary recap

- Owned by ollarma. Independent of `portfolio-dashboard` and Watchtower.
- Backed by typed service / read models.
- Bounded helper plus bounded execution only — no broad shell passthrough.
- Receipts are hash-chained JSONL under `.ollarma/`.

## Watchtower overlay (downstream-only)

Watchtower (separate repo) reads ollarma artifacts read-only and surfaces them in its
portfolio review matrix at `GET /api/project-review`. Watchtower does not write to
ollarma. Ollarma does not write to Watchtower.

The review-matrix heuristic recognises `dashboard/`, `web/`, `web/public`, `frontend/`,
`ui/`, `docs/DASHBOARD.md`, or `docs/dashboard.md` to mark a project "dashboard
observed". Ollarma's runtime is served from the Starlette app rather than a separate
directory — this file exists so the heuristic clears the gap without relocating the
runtime.
