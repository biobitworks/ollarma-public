# Ollarma KB Contract

`ollarma` owns a deterministic, repo-local knowledge-base contract for sibling
project help and routing.

## What It Owns

- adapter-declared KB metadata
- repo-local KB artifact root under the consumer repo, default
  `.ollarma/kb`
- read-only contract and export surfaces
- fail-closed status and reason codes when KB state is undeclared, missing, or
  not yet built

## What It Does Not Own

- portfolio governance or project status authority
- direct writeback into sibling repos beyond the owning repo's local KB area
- live mutable control-plane databases
- broad fallback authority to answer from stale or undeclared KB state

## Contract Shape

- `project_root` stays absolute and owned by the adapter
- `knowledge_base.artifact_root` stays repo-relative
- KB source declarations may be repo-relative or absolute, but they must resolve
  under `project_root`
- exported references should remain repo-relative or stable-id based

## Authority Model

- `canonical` sources are owned primary repo content
- `reference` sources are readable but not authoritative for mutation or policy
- stale or unbuilt KB state must fail closed with an explicit reason code

Initial reason codes:

- `KB_SOURCES_UNDECLARED`
- `KB_SOURCE_MISSING`
- `KB_NOT_BUILT`
- `KB_STALE`

## Read Surfaces

The contract is consumed through read-only surfaces only:

- CLI: `ollarma kb-status`, `ollarma kb-search`
- HTTP: `GET /kb/status/{project}`, `POST /kb/search`
- MCP: `kb_status`, `kb_search`

The build lane stays explicit and separate:

- CLI: `ollarma kb-build`

The local artifact set now includes both JSON exports and a bounded SQLite read
model at `.ollarma/kb/search.sqlite`.

## Future Phases

- Phase 22 materializes deterministic KB artifacts
- Phase 23 exposes read-only search/read models
- Phase 24 grounds helper routing in retrieval evidence
- Phase 25 surfaces KB status and exports in the dashboard and operator docs
