# ollarma Demo Project

This project is the smallest self-contained live demo for `ollarma`.

## Purpose

Show one local-first, restart-safe execution path that does not depend on sibling repos:

- KB-ready project content
- one visible workflow manifest
- one real autopilot-executed script
- one deterministic handoff artifact for downstream systems

## Important Paths

- Workflow manifest: `.ollarma/manifests/seedgraph-handoff-demo.json`
- Demo script: `scripts/seedgraph_handoff_demo.py`
- Handoff artifact: `artifacts/ollarma/demo/handoff.json`
- Judge narrative: `artifacts/ollarma/demo/judge_narrative.md`
- Autopilot receipts/checkpoints: `.ollarma/autopilot/<run_id>/`
- KB artifacts after build: `.ollarma/kb/`

## Demo Narrative

The demo models the safe handoff from `ollarma` into downstream systems such as SeedGraph, Watchtower, or GettingScienceDone. `ollarma` does the bounded local work first, writes deterministic artifacts, and leaves a restart-safe trail before any broader orchestration layer takes over.

## Live Happy Path

1. Build the demo KB.
2. Confirm the workflow catalog is non-empty.
3. Run the demo script through `autopilot`.
4. Open the dashboard and show the same project state through the operator UI.

## Red-Team Notes

- Workflow submission is catalog and admission only today; it is not the live execution path.
- The real execution proof is the autopilot run plus its receipts, checkpoint, and handoff artifact.
- The demo intentionally excludes broad repo mutation, shell passthrough, and cross-repo writeback.
