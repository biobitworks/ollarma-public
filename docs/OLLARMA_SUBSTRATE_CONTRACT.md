# Ollarma Substrate Contract

**Audience:** Sibling projects (Overwatch, Antigence, gettingsciencedone, and
any other repo in the Biobitworks tree) that consume ollarma programmatically.

**Scope:** The three durable surfaces ollarma publishes — HTTP endpoints, CLI
verbs, and Python importable modules — plus the receipt schemas they emit.

**Stability pledge (v5.0):** Every receipt class referenced below commits to
`schema_version=1` for the duration of the v5.0 milestone. Consumers SHOULD
read `schema_version` at call time and degrade gracefully if they see an
unexpected value. v5.1+ may bump `schema_version`; this document makes NO
promise about post-v5.0 stability.

**Executable source of truth:** The worked example in §4 is copied verbatim
into `tests/integration/test_substrate_consumer_contract.py`. If the doc
drifts from the code, the integration test fails.

---

## §1 — HTTP surface contract

The service runs on `127.0.0.1:8484` via `ollarma serve`. Every response that
persists evidence (admissions, frontier calls, recovery packets) carries a
Pydantic-validated receipt; the receipt class column points at the stable
import path.

| Endpoint                 | Method | Purpose                                                    | Receipt / Schema class                           | schema_version | Stability (v5.0) |
|--------------------------|--------|------------------------------------------------------------|--------------------------------------------------|----------------|-------------------|
| `/health`                | GET    | Liveness + folded readiness status                         | `ollarma.service.StartupReadinessPayload`        | 1              | stable            |
| `/startup/readiness`     | GET    | Readiness posture: queue, pipelines, gateway block         | `ollarma.service.StartupReadinessPayload`        | 1              | stable (additive) |
| `/chat`                  | POST   | Single-turn model chat                                     | JSON; no persisted receipt                       | n/a            | stable            |
| `/route`                 | POST   | Project-scoped routing through fleet adapters              | JSON; receipt in `.ollarma/session-log.jsonl`    | 1              | stable            |
| `/workflow`              | POST   | Execute a manifest-driven workflow step                    | `ollarma.scribe.ScribeEntry`                     | 1              | stable            |
| `/autopilot`             | POST   | Asset discovery + tiered execution (per project / fleet)   | `ollarma.autopilot.AutopilotReport`              | 1              | stable            |
| `/recovery/status`       | GET    | Read latest recovery packet (no scan)                      | Recovery packet dict (see `RECOVERY_PROTOCOL.md`)| 1              | stable            |
| `/recovery/scan`         | POST   | Run recovery scan + write packet                           | Recovery packet dict                             | 1              | stable            |
| `/gateway/submit`        | POST   | Submit `EscalationReceipt`; get `FrontierReceipt`          | `ollarma.gateway.FrontierReceipt`                | 1              | stable (v5.0)     |
| `/kb/status/{project}`   | GET    | KB freshness, chunk count, last-build timestamp            | JSON (see `OLLARMA_KB_CONTRACT.md`)              | 1              | stable            |
| `/kb/search`             | POST   | KB lookup for a project                                    | JSON (see `OLLARMA_KB_CONTRACT.md`)              | 1              | stable            |

**Receipt class stability:**

- `FrontierReceipt` (`ollarma.gateway`) — one per gateway call in every posture
  (`succeeded`, `failed`, `dry_run`, `disabled`). Hash-chained into
  `.ollarma/gateway/receipts.jsonl`.
- `GatewayAdmissionEntry` (`ollarma.gateway`) — one per admission decision
  (accept / reject / disabled / dry_run). Hash-chained into
  `.ollarma/gateway/admissions.jsonl`.
- `EscalationReceipt` (`ollarma.escalation`) — emitted locally when
  deterministic execution cannot proceed; consumers submit it to
  `/gateway/submit`.

All three classes pin `schema_version=1` for the v5.0 milestone.

Additional endpoints exist (`/v1/sibling/read`, `/models/status`,
`/pipelines/*`, `/macfind/*`, `/agents/{name}/run`, `/metrics`,
`/dashboard/*`, `/openapi.json`, `/docs`). Those are either internal, subject
to refinement in later phases, or documented in their own specs; they are NOT
part of the substrate contract.

---

## §2 — CLI surface contract

Invoke via the `ollarma` console script (installed by `pip install -e .`).
Every verb below is stable for v5.0; flag additions are additive.

| Verb                | Purpose                                                     | Example                                                                     | Exit codes                                   |
|---------------------|-------------------------------------------------------------|-----------------------------------------------------------------------------|----------------------------------------------|
| `projects`          | List registered fleet adapters                              | `ollarma projects`                                                          | 0 ok, 1 adapter-load error                   |
| `kb-build`          | Build or refresh a project's KB                             | `ollarma kb-build --project overwatch`                                      | 0 ok, 1 project unknown / build error        |
| `kb-status`         | Show KB freshness for a project                             | `ollarma kb-status --project overwatch`                                     | 0 ok, 1 project unknown                      |
| `kb-search`         | Query a project's KB                                        | `ollarma kb-search --project overwatch --query "prion clock" --limit 5`     | 0 ok, 1 no hits / project unknown            |
| `chat`              | Interactive REPL with optional project scope                | `ollarma chat --project overwatch`                                          | 0 ok, 1 project unknown                      |
| `autopilot`         | Discovery (default) or execution (`--run`) for a project    | `ollarma autopilot overwatch --run --threshold 0.9`                         | 0 ok, 1 project unknown / fleet empty        |
| `workflow`          | Execute one manifest step                                   | `ollarma workflow --project overwatch --manifest-ref ... --step-id ...`     | 0 ok, 1 on step failure                      |
| `recover scan`      | Scan repo for stranded work; write packet                   | `ollarma recover scan --project-root . --base main`                         | 0 clean, 1 non-clean, 2 scan error           |
| `recover report`    | Print latest packet without re-scanning                     | `ollarma recover report --project-root .`                                   | 0 clean, 1 non-clean, 2 no packet            |
| `receipts trace`    | Walk escalation → admission → frontier for one id           | `ollarma receipts trace er-abc123... --repo-root .`                         | 0 chain intact, 1 mismatch / missing         |
| `startup-smoke`     | Probe launchd + HTTP readiness (inspection only)            | `ollarma startup-smoke --base-url http://127.0.0.1:8484`                    | 0 ready/degraded, 1 blocked / unreachable    |
| `serve`             | Start the HTTP surface on `127.0.0.1:8484`                  | `ollarma serve`                                                             | 0 clean shutdown, non-zero on bind failure   |
| `dashboard`         | Launch the local dashboard                                  | `ollarma dashboard`                                                         | 0 clean shutdown                             |
| `swe-bench fetch`   | Download + cache SWE-bench Lite with pinned SHA             | `ollarma swe-bench fetch`                                                   | 0 ok, 2 on error                             |
| `swe-bench run`     | Run subset through `local` or `frontier` lane               | `ollarma swe-bench run --lane frontier --subset first-2 --virtual-key-id vk_demo --no-live` | 0 ok, 2 on bad args / --no-live gated exit  |
| `swe-bench status`  | Show dataset cache state + latest run                       | `ollarma swe-bench status`                                                  | 0 ok                                         |
| `swe-bench leaderboard` | Print leaderboard + verify chain                        | `ollarma swe-bench leaderboard <run_id>`                                    | 0 clean, 1 chain broken, 2 missing artifact  |

---

## §3 — Python module surface contract

These are the importable symbols sibling projects may rely on for the v5.0
milestone. Anything prefixed `_` is private and may change without notice.

### `ollarma.escalation`

| Symbol                     | Kind   | Notes                                                       |
|----------------------------|--------|-------------------------------------------------------------|
| `EscalationReceipt`        | class  | Frozen Pydantic model. Implicit `schema_version=1` by contract (field set frozen across v5.0). |
| `ReasonCode`               | enum   | `str`-valued escalation reason codes (see class docstring). |
| `build_escalation_receipt` | func   | Normalized builder; returns `EscalationReceipt`.            |

### `ollarma.gateway`

| Symbol                   | Kind   | schema_version | Notes                                               |
|--------------------------|--------|----------------|-----------------------------------------------------|
| `FrontierReceipt`        | class  | `1`            | Frozen; emitted per gateway call in every posture.  |
| `GatewayAdmissionEntry`  | class  | `1`            | Frozen; emitted per admission decision.             |
| `GatewayReasonCode`      | enum   | n/a            | Extends `ReasonCode` with gateway-specific codes.   |
| `GatewayReceiptStore`    | class  | n/a            | Hash-chained JSONL store under `.ollarma/gateway/`. |
| `GatewayStoreError`      | exc    | n/a            | Raised on chain corruption / schema violation.      |

### `ollarma.gateway_client`

| Symbol                  | Kind  | Notes                                                          |
|-------------------------|-------|----------------------------------------------------------------|
| `GatewayClient`         | class | Ingress. `submit(...)` returns `FrontierReceipt`.              |
| `GatewayError`          | exc   | Base class; never raised directly.                             |
| `GatewayInputError`     | exc   | Raised on non-`EscalationReceipt` input (HTTP 400).            |
| `GatewayDisabledError`  | exc   | Taxonomy completeness (reserved for disabled paths).           |

### `ollarma.gateway_admission`

| Symbol                   | Kind   | Notes                                                     |
|--------------------------|--------|-----------------------------------------------------------|
| `AdmissionPolicy`        | class  | Allowlist + virtual-key registry + Keychain precheck.     |
| `AdmissionDecision`      | class  | Frozen dataclass summarizing one precheck outcome.        |
| `resolve_virtual_key`    | func   | Bytes-in / bytes-out Keychain resolver (never `str`).     |
| `_KEYCHAIN_LOOKUP`       | seam   | **Test-only** monkeypatch point. Not part of the public contract beyond its role as a sanctioned deterministic test seam. |

### `ollarma.providers` (Phase 59 + 60)

**Public:**
- `AnthropicProvider` class (from `ollarma.providers.anthropic`)
- `OpenAIProvider` class (from `ollarma.providers.openai`)
- `ProviderResponse` dataclass (from `ollarma.providers.base`)
- `PROVIDER_REGISTRY` dict + `get_provider(name)` helper (from `ollarma.providers`)

| Symbol               | Kind   | Notes                                                            |
|----------------------|--------|------------------------------------------------------------------|
| `AnthropicProvider`  | class  | Direct-httpx Anthropic Messages adapter (Phase 59, FRONT-01..06).|
| `OpenAIProvider`     | class  | Direct-httpx OpenAI Chat Completions adapter (Phase 60, FRONT-05).|
| `ProviderResponse`   | class  | Frozen dataclass; intermediate (not persisted to disk).          |
| `PROVIDER_REGISTRY`  | dict   | `str -> type[BaseProvider]`. Includes `"anthropic"` + `"openai"` as of Phase 60. |
| `get_provider`       | func   | Registry factory; raises `KeyError` on unknown name.             |

**Stability:** `schema_version=1` on `FrontierReceipt` (unchanged across Phase 59 + 60); `ProviderResponse` is an **internal intermediate** — sibling projects that consume frontier-provider output must rely on `FrontierReceipt`, not `ProviderResponse`.

**Live smoke (operator commands):**
- `ollarma gateway smoke-anthropic` — requires `ANTHROPIC_API_KEY` in the macOS Keychain under service `ollarma-anthropic` (install: `security add-generic-password -s ollarma-anthropic -a $USER -w`). Env override available via vk registry entries that set `keychain_service: "env:ANTHROPIC_API_KEY"`.
- `ollarma gateway smoke-openai` — requires `OPENAI_API_KEY` in the macOS Keychain under service `ollarma-openai` (install: `security add-generic-password -s ollarma-openai -a $USER -w`). Env override available via vk registry entries that set `keychain_service: "env:OPENAI_API_KEY"`.

### `ollarma.receipts_trace`

| Symbol           | Kind   | Notes                                                            |
|------------------|--------|------------------------------------------------------------------|
| `trace`          | func   | `(escalation_receipt_id, repo_root) -> TraceResult`. Reads only. |
| `TraceResult`    | class  | Frozen dataclass: rows + exit_code + chain_intact.               |
| `TraceRow`       | class  | Frozen dataclass: one row per stage.                             |
| `render_table`   | func   | Rich for TTY, plain ASCII otherwise.                             |

### `ollarma.swe_bench` (Phase 61 + 62)

| Symbol                     | Kind  | Notes                                                            |
|----------------------------|-------|------------------------------------------------------------------|
| `SWEBenchProblem`          | class | Frozen Pydantic; one row of a SWE-bench Lite dataset.            |
| `SWEBenchRun`              | class | Frozen Pydantic; per-problem run record (local OR frontier).     |
| `LocalLaneRunner`          | class | Runs problems through local autopilot (SWAP_DEGRADED aware).     |
| `FrontierLaneRunner`       | class | Runs problems through the gateway frontier lane (SWE-03).        |
| `LeaderboardArtifact`      | class | Frozen Pydantic; dual-lane pass@1 + cost aggregate (SWE-04).     |
| `build_leaderboard`        | func  | Aggregates local + frontier runs into a `LeaderboardArtifact`.   |
| `verify_leaderboard_chain` | func  | Walks both lanes' receipt streams + gateway; returns VerifyResult (SWE-05). |

**Stability:** `schema_version=1` on `LeaderboardArtifact` (frozen for v5.0);
all Runners accept an optional `run_id` for deterministic artifact layout.

### Private / not part of the contract

- Any symbol whose name starts with `_`.
- `ollarma.gateway_admission._KEYCHAIN_LOOKUP` is callable from tests as a
  sanctioned seam — it is explicitly NOT a stable Python API.
- Modules not listed above (e.g., `ollarma.http_api`, `ollarma.service`,
  `ollarma.evidence`, `ollarma.scribe*`, `ollarma.executor`) ship internal
  implementation and may change between milestones.

---

## §4 — Worked example

This example is the single source of truth for the consumer contract. It is
copied **verbatim** into
`tests/integration/test_substrate_consumer_contract.py::test_worked_example_runs`;
the test runs it every time the suite runs. If the example drifts from the
code, the test fails.

```python
import pathlib
import tempfile

from ollarma import gateway_admission
from ollarma.escalation import ReasonCode, build_escalation_receipt
from ollarma.gateway import FrontierReceipt
from ollarma.gateway_admission import AdmissionPolicy
from ollarma.gateway_client import GatewayClient


def run_worked_example(repo_root: pathlib.Path) -> FrontierReceipt:
    # 1. Build an EscalationReceipt locally (sibling-repo side).
    escalation = build_escalation_receipt(
        project="demo",
        lane="local-exec",
        task_class="generic",
        reason_code=ReasonCode.SWAP_DEGRADED,
        reason_detail="worked example",
        next_action="frontier_or_human",
    )

    # 2. Build an in-memory AdmissionPolicy (no config file on disk).
    policy = AdmissionPolicy(
        {
            "enabled": True,
            "allowlist": ["demo"],
            "virtual_keys": [
                {
                    "id": "vk_demo",
                    "keychain_service": "ollarma-demo",
                    "provider": "anthropic",
                },
            ],
        }
    )

    # 3. Stub the Keychain lookup via the sanctioned test seam.
    #    Raw bytes never cross the admission boundary; this is the only
    #    place in the example where we touch a private symbol.
    gateway_admission._KEYCHAIN_LOOKUP = lambda _service: b"fake-key-bytes"

    # 4. Submit in dry_run mode. No provider call, zero cost/latency.
    client = GatewayClient(repo_root)
    receipt = client.submit(
        escalation,
        dry_run=True,
        admission_policy=policy,
        virtual_key_id="vk_demo",
    )

    # 5. Contract assertions.
    assert isinstance(receipt, FrontierReceipt)
    assert receipt.status == "dry_run"
    assert receipt.reason_code == "DRY_RUN"
    assert receipt.schema_version == 1
    return receipt


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        run_worked_example(pathlib.Path(tmp))
```

**Why this example is small on purpose:** the contract surface is the set of
stable import paths + receipt fields, not a tutorial. Sibling projects that
need escalation + admission policy composition copy this function and swap in
their real project name, allowlist, and vk id.

---

## Related documents

- `docs/DETERMINISTIC_EXECUTION_BOUNDARY.md` — helper-vs-execution bound this
  contract formalizes.
- `docs/LOCAL_ADOPTION.md` — install modes + adapter discovery.
- `docs/RECOVERY_PROTOCOL.md` — recovery packet schema.
- `docs/OLLARMA_KB_CONTRACT.md` — `/kb/*` endpoint details.
- `.planning/v5.0-SUBSTRATE-AUDIT.md` §F-07 — audit finding this plan closes.

---

*Phase 57.1-04 — substrata consumer contract (F-07, v5.0 Hard Gate 7
down-payment). Full sibling-project integration story lands in Phase 65.*
