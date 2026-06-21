# Hackathon Backup Demo

This is the fastest credible live demo path from the current `ollarma` checkout.

## Demo Surface

Use the repo-local `ollarma-demo` project to show:

1. deterministic KB build and search readiness
2. non-empty workflow catalog discovery
3. one real `autopilot` execution that writes a downstream handoff artifact
4. the same state in the localhost dashboard

This avoids the current upstream manifest/catalog debt in sibling repos.

## Exact Commands

From `<repo>`:

```bash
export OLLARMA_DEMO_ADAPTERS=<repo>/demo/hackathon/adapters
PYTHONPATH=src python -m ollarma.cli kb-build --project ollarma-demo --adapters-dir "$OLLARMA_DEMO_ADAPTERS"
OLLARMA_ADAPTERS_DIR="$OLLARMA_DEMO_ADAPTERS" PYTHONPATH=src python -m ollarma.cli serve --host 127.0.0.1 --port 8485
```

After the server is up:

```bash
curl -s http://127.0.0.1:8485/health
curl -s http://127.0.0.1:8485/dashboard/workflows/ollarma-demo
curl -s -X POST http://127.0.0.1:8485/route \
  -H 'content-type: application/json' \
  -d '{"project":"ollarma-demo","prompt":"where is handoff.json"}'
curl -s -X POST http://127.0.0.1:8485/autopilot \
  -H 'content-type: application/json' \
  -d '{"project":"ollarma-demo","run_assets":false}'
open http://127.0.0.1:8485/dashboard
```

Primary execution path when host swap is zero:

```bash
PYTHONPATH=src python -m ollarma.cli autopilot ollarma-demo --adapters-dir "$OLLARMA_DEMO_ADAPTERS" --run --include scripts/seedgraph_handoff_demo.py
```

If `autopilot` returns `RESOURCE_BUDGET_EXCEEDED`, the host is in degraded mode and the stable fallback is:

```bash
sysctl vm.swapusage
python demo/hackathon/demo_project/scripts/seedgraph_handoff_demo.py
```

## Verification

Run these after the autopilot execution:

```bash
test -f demo/hackathon/demo_project/artifacts/ollarma/demo/handoff.json
test -f demo/hackathon/demo_project/artifacts/ollarma/demo/judge_narrative.md
find demo/hackathon/demo_project/.ollarma/autopilot -name receipts.json -o -name checkpoint.json
python -m pytest -q tests/test_hackathon_demo.py -x
```

Current observed host-state blocker during live verification:

- `autopilot ollarma-demo --run` rejected execution with `RESOURCE_BUDGET_EXCEEDED: swap in use (1486.9 MB)`
- KB build, workflow catalog discovery, exact-answer routing, dashboard load, direct script fallback, and focused autopilot tests all succeeded

## Judge-Facing Narrative

- `ollarma` is the bounded local substrate.
- It builds repo-local knowledge, exposes portable workflow contracts, and executes one local task safely.
- It leaves deterministic artifacts and restart-safe receipts before any broader system takes over.

## Red-Team Pass

- Do not claim manifest-backed workflow execution. The workflow surface is admission-only today.
- Do not use repo-root autopilot discovery for the live demo. It discovers too many runnable assets.
- Do not depend on sibling repo adapters or manifests for the event demo path.

## Remediation Path

If the dashboard server is stale or pointing at the wrong adapters:

```bash
pkill -f "python -m ollarma.cli serve" || true
export OLLARMA_DEMO_ADAPTERS=<repo>/demo/hackathon/adapters
OLLARMA_ADAPTERS_DIR="$OLLARMA_DEMO_ADAPTERS" PYTHONPATH=src python -m ollarma.cli serve --host 127.0.0.1 --port 8485
```

If KB artifacts are missing:

```bash
PYTHONPATH=src python -m ollarma.cli kb-build --project ollarma-demo --adapters-dir "$OLLARMA_DEMO_ADAPTERS"
```

If the execution artifact is missing:

```bash
PYTHONPATH=src python -m ollarma.cli autopilot ollarma-demo --adapters-dir "$OLLARMA_DEMO_ADAPTERS" --run --include scripts/seedgraph_handoff_demo.py
```

If the scheduler blocks execution because swap is non-zero:

```bash
sysctl vm.swapusage
```

Then reduce host memory pressure and rerun the autopilot command. If the event machine cannot be stabilized in time, use the direct-script fallback above and show the already-bounded dashboard, KB, and workflow surfaces.

## Deferred Until After Demo

- upstream Watchtower and Overwatch adapter remediation
- broad multi-project orchestration
- public packaging and distribution
