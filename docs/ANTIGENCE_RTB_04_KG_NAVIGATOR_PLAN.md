# RTB-04 KG Navigator Execution Plan

**Parent:** `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md`
**Status:** implementation plan, not shipped behavior.
**Scope:** expose read-only KG/KB navigation for Ollarma and later sibling
projects without broad artifact indexing or canonical KG write authority.

## Current Starting Point

The current machine macfind config is focused on Ollarma code/docs/test search:

```json
{
  "approved_dirs": [
    "<repo>/src",
    "<repo>/tests",
    "<repo>/docs"
  ],
  "confidence_threshold": 0.25,
  "max_results": 10
}
```

This is the correct first-pass scope for RTB planning. It avoids noisy
`.planning/`, `runs/`, cache, and generated artifact JSON while preserving
semantic search over live code, tests, and operator docs.

## Role

The navigator answers "where is the grounded local evidence?" It does not write
KG records, promote claims, or mutate any sibling repo. It composes existing
read surfaces:

- adapter-declared KB status/search,
- macfind semantic file search over approved dirs,
- optional gsigmad-provided KG pointers,
- route receipts and bridge events from RTB-01.

## Files To Add

| File | Purpose |
|---|---|
| `src/ollarma/navigator.py` | Navigator schemas, mode handling, KB/macfind/KG-pointer composition. |
| `tests/test_navigator.py` | Unit tests for modes, stale behavior, authority labels, no-write boundaries. |
| `tests/test_navigator_http.py` | HTTP endpoint tests for `/navigator/query`. |
| `docs/examples/navigator/README.md` | Operator examples and scope warnings. |

## Files To Modify

| File | Required changes |
|---|---|
| `src/ollarma/http_api.py` | Add `POST /navigator/query`. |
| `src/ollarma/cli.py` | Add `ollarma navigator query`. |
| `src/ollarma/mcp_server.py` | Add `navigator_query` MCP tool. |
| `src/ollarma/service.py` | Thin delegating helper if needed; keep navigator logic in `navigator.py`. |
| `docs/OLLARMA_SUBSTRATE_CONTRACT.md` | Document as v5.1+ additive once implemented. |
| `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md` | Flip RTB-04 from planned to implemented only after tests/proof pass. |

## Schema

```python
class NavigatorQuery(BaseModel):
    project: str
    query: str
    mode: Literal["kb_only", "kg_pointer", "auto_readonly"] = "auto_readonly"
    limit: int = 5
    kg_scope: str | None = None

class NavigatorCitation(BaseModel):
    source_type: Literal["kb", "macfind", "kg_pointer"]
    project: str
    path: str | None = None
    chunk_id: str | None = None
    authority: Literal["canonical", "reference", "local_index", "pointer"]
    digest: str | None = None
    excerpt: str | None = None

class NavigatorResult(BaseModel):
    schema_version: Literal[1] = 1
    project: str
    query: str
    mode: str
    answer_class: Literal[
        "kb_direct",
        "kg_pointer",
        "mixed_readonly",
        "orchestrator_handoff",
        "insufficient_evidence",
        "blocked"
    ]
    status: Literal["ready", "stale", "blocked"]
    reason_code: str | None = None
    citations: tuple[NavigatorCitation, ...] = ()
    next_action: str
    bridge_event_refs: tuple[str, ...] = ()
```

## Mode Semantics

### `kb_only`

- Uses adapter-declared KB artifacts only.
- Missing/stale KB follows the adapter `stale_behavior`.
- No macfind fallback if KB is blocked.

### `kg_pointer`

- Accepts only explicit gsigmad-provided KG pointer refs.
- Returns pointer metadata and citations.
- Never creates, edits, or repairs KG records.

### `auto_readonly`

- Uses KB first.
- May add macfind results from approved dirs for local operator navigation.
- May include KG pointers only when provided in the request or adapter metadata.
- If evidence is weak, returns `insufficient_evidence` or
  `orchestrator_handoff`, not a synthetic answer.

## Authority Rules

- `canonical`: adapter-declared canonical KB source.
- `reference`: adapter-declared reference KB source.
- `local_index`: macfind result from approved dirs; useful for finding code/docs
  but not KG truth.
- `pointer`: external KG/EXP/PROMPT pointer; navigable but not owned by Ollarma.

The result must expose authority level per citation. Summaries cannot blur
`local_index` and `canonical` evidence.

## Macfind Scope Rule

Initial RTB-04 must assume the focused Ollarma macfind scope:

- include: `<repo>/src`
- include: `<repo>/tests`
- include: `<repo>/docs`
- exclude: `.planning/`
- exclude: `runs/`
- exclude: caches and generated artifacts
- exclude: broad `<local-path>`

Sibling-project semantic search can be added later only through explicit
per-project approved dirs and skip rules.

## Bridge Events

If RTB-01 is implemented, navigator calls should emit:

- `navigator_query_started`
- `kb_hit`
- `navigator_query_done`
- `blocked` when KB/KG preconditions fail closed

If RTB-01 is not implemented yet, RTB-04 should not invent a parallel event
store. It should return no `bridge_event_refs`.

## Tests

Unit tests:

- `kb_only` returns KB citations on ready KB.
- `kb_only` blocks on missing KB when stale behavior is `block`.
- `auto_readonly` can include macfind citations with authority `local_index`.
- `auto_readonly` returns `insufficient_evidence` when neither KB nor macfind
  has evidence.
- `kg_pointer` returns pointer citations without writing KG files.
- citation authority is preserved per source.
- no-write guard proves navigator does not mutate project roots or KG paths.

HTTP/CLI/MCP tests:

- `POST /navigator/query` validates required fields.
- `POST /navigator/query` returns `NavigatorResult`.
- CLI command mirrors HTTP result fields.
- MCP tool returns the same schema.

Regression tests:

- Existing `/kb/search`, `/route`, and `/macfind/query` behavior remains
  unchanged.
- Missing/stale KB behavior remains fail-closed where configured.

## Acceptance Gate

RTB-04 is complete only when:

- navigator schema tests pass;
- HTTP/CLI/MCP surfaces return the same result contract;
- read-only/no-write tests pass;
- focused macfind scope is documented and used by examples;
- stale/missing KB does not silently fall back to uncited generic chat;
- KG pointers are surfaced as pointers, not rewritten as Ollarma truth.

## Deferred

- Broad all-project indexing.
- Automatic sibling-project macfind config generation.
- Canonical KG writeback.
- Claim promotion or EXP mutation.
- Long-running navigator sessions.

