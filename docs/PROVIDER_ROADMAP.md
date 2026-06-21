# Provider Roadmap

**Audience:** Operators and authors who want to know which provider adapters are live, which are substrate-only, and which are explicitly future work.

**Hard stance:** This document does not authorize new provider runtime code, does not list API keys, and does not enable any live provider call. It catalogs status only.

---

## Status legend

| Status | Meaning |
|---|---|
| **LIVE** | Adapter ships AND has been smoke-tested against the real provider. |
| **SUBSTRATE-LIVE** | Adapter ships in code (Pydantic schemas, helper functions, tests against mocks) but has NOT been smoke-tested against the real provider. Requires operator key + explicit run before promoting to LIVE. |
| **NOT LIVE — FUTURE** | Listed for forward planning. No adapter code in this repo. No keys. Not in any current milestone. |
| **REJECTED** | Considered and explicitly excluded — see "Out of scope" below. |

---

## Local providers (always on)

| Provider | Status | Notes |
|---|---|---|
| **Local Ollama** (MLX backend on Apple Silicon) | LIVE | Default for every `/chat`, `/route`, `/autopilot`, `/workflow` call. Selection resolved from `models.yml` + benchmark evidence; refuses with `SELECTION_STALE` when stale. |

Resident model set on the reference workstation includes `qwen2.5-coder:7b`, `qwen2.5:1.5b`, `deepseek-r1:14b`, `gemma3:4b`, `gemma3:12b`, `qwen3:1.7b`. Selection authority is the freshness of `ollarma run --suites code --trials 3` outputs (see `docs/MULTI_AGENT_BRIDGE_CONTRACT.md` §6).

---

## Frontier providers — gateway lane (v5.0)

These adapters ship under the explicit, receipt-bearing gateway. None are reachable from `/chat`, `/route`, `/autopilot`, or `/workflow`. Operators submit a populated `EscalationReceipt` to `/gateway/submit` and receive a `FrontierReceipt` in return. The gateway is **disabled by default** and requires `gateway.enabled=true` AND a per-project allowlist entry before any provider call is reachable.

| Provider | Status | Module | Phase | Live smoke deferred reason |
|---|---|---|---|---|
| **Anthropic** (Messages API) | SUBSTRATE-LIVE | `src/ollarma/providers/anthropic_adapter.py` | v5.0 Phase 59 (`2fdfa6f`) | `ANTHROPIC_API_KEY` absent on reference host; operator CLI `ollarma gateway smoke-anthropic` ships for when key is present |
| **OpenAI** (Chat Completions) | SUBSTRATE-LIVE | `src/ollarma/providers/openai_adapter.py` | v5.0 Phase 60 (`7748cbe`) | `OPENAI_API_KEY` absent on reference host; same opt-in pattern |

**Gateway hard invariants** (from `.planning/REQUIREMENTS.md`):
- I-01 Local-first stays the default. Gateway is opt-in.
- I-02 No silent fallback. Every frontier call needs an `EscalationReceipt` in → `FrontierReceipt` out.
- I-06 API keys stay out of git forever. Keychain or env only.

---

## Future provider adapters (NOT live)

These are catalogued for forward planning. **No adapter code lives in this repo for them today.** Adding any of them is its own scoped milestone with its own REQUIREMENTS.md.

| Provider | Status | Why future, not now |
|---|---|---|
| **Google Gemini** | NOT LIVE — FUTURE | Gateway pattern is proven via Anthropic + OpenAI. Adding a third provider requires (a) operator key in keychain, (b) per-project allowlist update, (c) cost-accounting alignment with `FrontierReceipt.cost_usd`, (d) live smoke test under HG-3-equivalent gate before promotion to SUBSTRATE-LIVE → LIVE. None of these are done. |
| **xAI Grok** | NOT LIVE — FUTURE | Same as above. The gateway abstraction is provider-agnostic by design (Phase 60 forced this), so the adapter shape is known; only the operator+key+smoke pipeline is missing. |
| **DeepSeek V4 Flash** (`deepseek-v4-flash:cloud`) | NOT LIVE — FUTURE — gateway lane only | First DeepSeek target. Reachable via Ollama's `:cloud` namespace (Ollama-hosted relay → DeepSeek). Despite being addressable through the local Ollama client, it IS a cloud call, so it MUST go through the gateway lane (`/gateway/submit` with explicit `EscalationReceipt`). **Forbidden in `/chat`, `/route`, `/autopilot`, `/workflow`, and Vitaology per-atom loops.** Sources: [ollama.com/library/deepseek-v4-flash](https://ollama.com/library/deepseek-v4-flash). Future scoped milestone: see `.planning/seeds/SEED-deepseek-v4-gateway-integration.md`. |
| **DeepSeek V4 Pro** (`deepseek-v4-pro:cloud`) | NOT LIVE — FUTURE — gateway lane only — second-stage target | Second DeepSeek target, scoped after Flash ships and proves the integration pattern. Same gateway-only constraint as Flash. Sources: [ollama.com/library/deepseek-v4-pro](https://ollama.com/library/deepseek-v4-pro). Same milestone seed; staged after Flash. |

A future scoped milestone would mirror the v5.0 Phase 59→60 sequence: stand up adapter code under `src/ollarma/providers/<provider>_adapter.py`; round-trip a `FrontierReceipt` end-to-end with all populated fields; verify keychain hygiene with grep + log-scrub tests; then live smoke with operator approval.

### DeepSeek V4 — `:cloud` namespace nuance

Unlike Anthropic / OpenAI which require dedicated provider SDKs, DeepSeek V4 models are addressed via Ollama's hosted-relay namespace (`<model>:cloud`). The **Python call shape is identical to a local Ollama call** — `ollama.Client().generate(model='deepseek-v4-flash:cloud', ...)` — but the network destination is Ollama's relay, which proxies to DeepSeek. **This makes the boundary easy to violate by accident**: existing local-routing code paths could call a `:cloud` model with no other code change. The DeepSeek adapter milestone (seed) MUST add (a) explicit allowlisting of the `:cloud` namespace at the gateway-only lane and (b) a refusal hook in `service._route_local_first` that rejects any model name ending in `:cloud` outside the gateway path. This is a NET-NEW constraint not present for Anthropic/OpenAI (whose SDK imports localized the boundary).

---

## Out of scope (REJECTED)

Reasons follow each entry. Adding any of these would require an explicit scope reversal, not a provider-roadmap update.

| Provider | Why rejected |
|---|---|
| **NVIDIA / CUDA-bound providers** | Hardware contract is M1 Metal/MLX only. |
| **Open-WebUI relay or third-party multi-provider proxies** | Adds a layer Ollarma already provides (the gateway). Defeats receipt-chain auditability. |
| **Self-hosted remote-Ollama relays** | Single-host assumption is load-bearing for all v4.5+ work. Multi-host is a separate seed (`SEED-ollarma-swarm-broker-role.md`), not a provider question. |
| **Direct OpenRouter / Together / Replicate via the routing path** | Same reason as Open-WebUI. The gateway exists so every provider goes through one audited surface; bypassing it via `/route` would violate I-02. |

---

## Promotion flow (template for any future provider)

When a future provider IS scoped:

1. **Scope** — operator opens a milestone via `/gsd-new-milestone`. REQUIREMENTS.md inherits gateway invariants (I-01..I-06).
2. **Substrate** — adapter lands under `src/ollarma/providers/<provider>_adapter.py`. Tests round-trip `FrontierReceipt` against a mocked provider response. Status becomes SUBSTRATE-LIVE.
3. **Live smoke** — operator stores key in keychain (NEVER in git). `ollarma gateway smoke-<provider>` runs against the real API once. Receipt + scribe entry land. Status becomes LIVE.
4. **Documented** — this file updated. README links updated.

Until step 4, operators must treat the provider as NOT LIVE in the bridge.

---

## Cross-references

- `docs/MULTI_AGENT_BRIDGE_CONTRACT.md` §1 — bridge diagram showing future-provider slot
- `docs/OLLARMA_SUBSTRATE_CONTRACT.md` §1 — gateway endpoint stability pledge
- `.planning/REQUIREMENTS.md` — v5.0 hard invariants (I-01..I-06)
- `.planning/milestones/v5.0-ROADMAP.md` — Anthropic + OpenAI substrate ship history
- `.planning/seeds/SEED-deepseek-v4-gateway-integration.md` — forward-looking scope for the DeepSeek V4 Flash + Pro provider milestone

*Last updated: 2026-05-07.*
