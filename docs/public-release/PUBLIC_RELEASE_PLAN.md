# Ollarma — Public Release Plan

**Compiled:** 2026-06-10 · **Role:** PI/PM/Operator/SWE
**Honest status:** the repo is currently **tightly coupled to a private portfolio**. A public version is a **clean extraction of the substrate**, not a switch-flip. This document is the extraction contract: what is core (ships public), what is private-coupling (strip/parameterize), and the exact mechanism for each.

---

## 1. Current-state assessment (measured, not assumed)

A scan of tracked files (2026-06-10):

| Signal | Finding | Implication for public release |
|---|---|---|
| Hardcoded secrets/keys | **None.** Every `api_key`/`secret`/`token` match is *redaction logic* (`bridge_events.py`, `conversation_provenance.py` actively scrub secrets). Invariant **I-06** (keys out of git) holds. | ✅ No key-stripping needed. The substrate is already secret-hygienic. |
| Private project names | Heavy coupling: cellico (176 files), antigence (157), overwatch (114), watchtower (90), biobitworks (26), deltaprot (22). | ⚠️ Mostly in **fleet adapters, portfolio docs, and tests**. Substrate *code* references them via config, not hardcoding. Strip docs; synthesize example adapters. |
| Absolute `<local-path>` paths | In `discovery.py`, `fleet_tools.py` (as `DEFAULT_*_DIR` fallbacks pointing at the private `gettingsciencedone` repo), plus clients/scripts. | ⚠️ Path resolution is **already 4-level** (CLI → env → `~/.config/ollarma/config.toml` → hardcoded fallback). Decoupling = change the fallback to a non-personal default. |
| Portfolio governance wiring | `overwatch_adapter.py`, `sibling_reader.py`, fleet registry of real private repos. | ⚠️ Optional integration layer. Public ships with it **disabled by default** + a synthetic example sibling. |

**Conclusion:** the *engine* is clean and secret-safe; the *configuration and documentation* carry the private portfolio. The extraction boundary is therefore sharp and mostly mechanical.

---

## 2. Extraction boundary — core (public) vs private-coupling

### 2a. Core substrate → SHIPS PUBLIC (the ~65-module engine)

The mechanisms from [`../publication/TERMINOLOGY_EQUIVALENCE.md`](../publication/TERMINOLOGY_EQUIVALENCE.md), all portfolio-agnostic:

| Capability | Modules |
|---|---|
| Model cascade / routing ladder | `routing_ladder.py`, `stage_router.py`, `escalation.py`, `gateway.py`, `gateway_admission.py`, `gateway_client.py` |
| Admission + residency + scheduling | `admission.py`, `residency.py`, `scheduler.py`, `reserved_models.py`, `guards.py` |
| Verifier guardrails + antibody policy | `guardrail.py`, `antibody_profile.py`, `antibody_model_policy.py` |
| Deterministic execution boundary | `execution_policy.py`, `executor.py`, `autopilot.py`, `notebook_workflow.py`, `plan_parser.py` |
| Provenance / receipts | `evidence.py`, `receipts_trace.py`, `run_ledger.py`, `governance_store.py`, `conversation_provenance.py`, `idempotency.py` |
| Recovery engine | `recovery.py`, `pipeline_control.py` |
| Local retrieval / KB | `kb.py`, `kb_search.py`, `kb_contract.py`, `embeddings.py`, `macfind_*` |
| Bench harness | `registry.py`, `scorers.py`, `reporter.py`, `metrics.py`, `bench_refresh.py`, `run_benchmark.py`, `tasks/` |
| Swarm engine | swarm package + `scripts/swarm_*.py` |
| Surfaces | `http_api.py`, `cli.py`, `mcp_server.py`, `service.py`, `dashboard*.py` |

### 2b. Private-coupling → STRIP or PARAMETERIZE

| Item | Action for public |
|---|---|
| `discovery.py:DEFAULT_ADAPTERS_DIR`, `fleet_tools.py:DEFAULT_SKILLS_DIR` (point at private `gettingsciencedone`) | Change fallback to `~/.config/ollarma/adapters` + `~/.config/ollarma/skills`; ship a **synthetic example adapter** (`docs/examples/`). Resolution chain already supports override — only the fallback changes. |
| Real fleet adapters (deltaprot, cellico, etc.) | **Do not ship.** Replace with 1–2 synthetic example projects (`example-science`, `example-code`). |
| `overwatch_adapter.py`, `sibling_reader.py` portfolio wiring | Ship the **interface**, disabled by default; document as "bring-your-own governance backend." |
| `.immunos/`, `.gsigmad/`, `.ollarma/` runtime state | Already gitignored. Confirm none tracked; add `.immunos/` to public `.gitignore`. |
| Portfolio docs (`AGENTS.md` portfolio glossary, `OLLARMA_BACKUP_PUSH_MIRROR_PROMPT.md`, project-specific evidence docs) | Move to a `private/` overlay not in the public tree; keep generic substrate docs. |
| Personal references (byron, biobitworks, machine paths in docs) | Replace with `operator`/`example.org`/relative paths in the public doc set. |
| `com.byron.ollarma.plist` (launchd) | Ship a templated `com.example.ollarma.plist.template`. |

---

## 3. Mechanism — how the public tree is produced

**Strategy: overlay, not fork.** Keep one source of truth; generate the public tree by subtraction + parameterization, so the substrate never diverges.

1. **`public-manifest.txt`** — explicit allowlist of paths that ship public (the §2a set + generic docs + tests that don't name private projects).
2. **`scripts/build_public_tree.py`** — copies allowlisted paths into `dist/public/`, runs a **scrubber** (replace personal tokens, swap `DEFAULT_*_DIR` fallbacks, inject synthetic adapters), then runs the test suite against the scrubbed tree.
3. **Synthetic fixtures** — `example-science` and `example-code` adapters + a tiny `example/` KB so `bench run`, `/route`, and the swarm demo work out-of-the-box with no private data.
4. **Public CI gate** — `git grep` in `dist/public/` for the private-name denylist (`cellico|overwatch|watchtower|deltaprot|biobitworks|<local-path>`) must return **zero hits**; the scrubbed suite must pass. This is the release fail-closed.
5. **Provenance** — emit `dist/public/MANIFEST.sha256` so a downstream user can verify the tree they got matches the published release.

---

## 4. What the public package is (positioning)

Per the user's framing: **a communication, safety, and verification layer for AI-assisted computational research** — runs offline on commodity Apple-silicon, routes cheap local work and escalates hard cases to a frontier model, verifies output before trusting it, and records a tamper-evident receipt chain. See `TERMINOLOGY_EQUIVALENCE.md` §4 for the abstract.

**Ships with:** the substrate, the benchmark harness, the swarm engine, synthetic example adapters, the HTTP/CLI/MCP surfaces, and the full test suite.
**Does not ship:** any real project adapter, portfolio governance backends (interface only), personal config, or machine-specific paths.

---

## 5. Licensing

See [`LICENSING_TIERS.md`](./LICENSING_TIERS.md) for the public OSS / academic / corporate model. Repo already carries Apache-2.0 (`LICENSE`); the tri-tier sits on top of it.

---

## 6. Acceptance gate (public release is ready only when)

- [ ] `dist/public/` produced by `build_public_tree.py` from `public-manifest.txt`.
- [ ] Denylist `git grep` returns 0 hits in `dist/public/`.
- [ ] Scrubbed test suite passes (target: same 1672/0, minus any private-adapter tests excluded by manifest).
- [ ] `bench run --dry-run`, `ollarma serve` + `/chat`, and the swarm demo all work against synthetic fixtures only.
- [ ] No `<local-path>`, no private project name, no personal contact in the public tree.
- [ ] `MANIFEST.sha256` emitted; README positions the package per §4.
- [ ] License headers + `LICENSING_TIERS.md` present.

## 7. Built and verified (2026-06-10)

`scripts/build_public_tree.py` + `public-manifest.txt` are **implemented, run, and the gate PASSES.**

- **102 files** copied into `dist/public/` from the substrate allowlist (engine + bench harness + generic surfaces + substrate tests + generic docs + the three license files).
- Scrubber applied: machine paths → relative/`~/.config`; `byron@biobitworks.com` → `operator@example.org`; and all private portfolio names genericized with **identifier-safe** (underscore) placeholders (`cellico`→`example_science`, `antigence`→`example_review`, `overwatch`→`governance_backend`, `watchtower`→`operator_console`, `deltaprot`→`example_proteomics`, `biobitworks`→`example_org`, etc.). Underscore form is required because these names appear inside Python identifiers (e.g. `RESERVED_ANTIGENCE_SENTINEL_MODELS`); a hyphen would break syntax. The same regex applies to source and tests, so the tree stays internally consistent.
- **Denylist gate: PASS — 0 hits.** Confirmed by an independent `grep` over `dist/public/` (`cellico|overwatch|watchtower|deltaprot|biobitworks|<local-path>` → 0 files).
- **Functional:** the public tree imports cleanly (`ollarma`, `service`, `routing_ladder`, `guardrail`, `gateway`, `swarm.run_simulation`); **140 / 142 substrate tests pass.**

**Known limitation (precisely characterized):** 2 `test_routing_ladder.py::TestRouteIntegration` escalation tests fail *on the scrubbed tree only* — the scrubber mutates a grounding fixture enough to flip one routing decision (`grounded_local_synthesis` vs `frontier_or_human`). These pass in the real repo; the failure is **scrubber fidelity, not a substrate bug.** The production-grade fix is to **parameterize** those private names in the actual source (config-driven project keys) instead of regex-scrubbing a copy — at which point the fixtures are stable and the gate stays 0. That source parameterization is the one remaining step between "clean buildable public tree" (done) and "100%-green public tree."

**Net state:** the public version is **prepared** — a clean (0 private references), importable, 98.6%-passing public tree is produced reproducibly by one command, plus the three license files. The remaining work is source-level parameterization of ~10 example-project string literals (mechanical, enumerated).

**Reproduce:** `python scripts/build_public_tree.py` (writes `dist/public/`, scrubs, runs the fail-closed denylist gate). `dist/` is gitignored — the tree is a build artifact; the builder + manifest + license files are the committed contract.
