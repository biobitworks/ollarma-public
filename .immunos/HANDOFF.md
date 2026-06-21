# Token-Loss Handoff — Ollarma Phase 39

**Written:** 2026-04-16T08:06Z
**By:** Claude Opus 4.6 (session ending or token-limited)
**For:** Any local model (qwen3:1.7b pinned, deepseek-r1:8b for reasoning)

## Current State

**Phase 39: D6 Safety Wiring** — gap closure, wiring 4 orphaned safety modules into production.

### Done
- `39-CONTEXT.md` written with all 5 decisions locked
- `39-DISCUSSION-LOG.md` written
- Phase 37 code review: 5/5 findings fixed and committed
- qwen3:1.7b pinned in Ollama pipeline

### Not Done
- `39-RESEARCH.md` — needs to be written (researcher was mid-flight when tokens ran out last time)
- `39-PLAN.md` — not started
- Actual code changes — not started

## What Phase 39 Builds (5 wiring targets)

1. **`service.find_on_mac()`** → add `gate=GuardrailGate(AdapterConfig())` to `MacFindSearcher.search()` call
2. **`service.read_sibling_file()`** → add `gate=GuardrailGate(AdapterConfig())` to `read_sibling_path()` call
3. **`service.run_agent()`** → call `OverwatchAdapter().attach()`, put result in `AgentReceipt.overwatch_state`
4. **`service.run_agent()`** → call `GovernanceStore().store()`, append `RunReceipt` with `governance_refs=(digest,)`
5. **`tests/conftest.py`** → session-scope fixture asserting no sibling mutation via git porcelain

## Key Files
- `src/ollarma/service.py` — lines ~2234 (read_sibling_file), ~2902 (find_on_mac), ~2974 (run_agent)
- `src/ollarma/agents.py` line 51 — AgentReceipt (add `overwatch_state: str = "NOT_CONFIGURED"`)
- `src/ollarma/overwatch_adapter.py` — OverwatchAdapter.attach()
- `src/ollarma/governance_store.py` — GovernanceStore.store()
- `src/ollarma/run_ledger.py` — RunReceipt, append_run_receipt()
- `src/ollarma/macfind_searcher.py` — search(gate=)
- `src/ollarma/sibling_reader.py` — read_sibling_path(gate=)

## Pattern
All imports MUST be lazy (inside the function body) to avoid circular imports. Same pattern as Phase 35.

## Services Running
- Ollama: port 11434 (launchd auto-restart)
- Watchtower: port 8000 (launchd auto-restart)
- Ollarma MCP: PID-based only (no launchd — restart with `ollarma mcp`)
- qwen3:1.7b: pinned in pipeline (1.4GB)

## Recovery Command
```bash
# If Claude tokens exhausted, use local model:
ollama run qwen3:8b  # or deepseek-r1:8b for reasoning
# Read this file, then read 39-CONTEXT.md for full decisions
```
