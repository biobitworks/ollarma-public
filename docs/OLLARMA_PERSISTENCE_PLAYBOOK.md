# Ollarma Persistence Playbook

Operating guide for the v4.5 persistent-service model on this machine.
Covers the full stack: launchd install, startup verification, GPU residency
policy, routing ladder, live proof reproduction, and troubleshooting.

Cross-references:
- `docs/LOCAL_ADOPTION.md` — install modes, adapter discovery, Phase 51 baseline
- `docs/RECOVERY_PROTOCOL.md` — v4.3 recovery contract
- `docs/OPERATOR_RECOVERY_PLAYBOOK.md` — v4.3/v4.4 5-step recovery walkthrough

---

## 1. Install

The hardened 8-step launchd installer (Phase 51, hardened in Phase 55):

```bash
ollarma start --install
```

What it does:

| Step | Action | Fatal on failure? |
|------|--------|------------------|
| 1 | Copy `com.byron.ollarma.plist` → `~/Library/LaunchAgents/` | Yes |
| 2 | `plutil -lint` the installed plist | Yes — refuses to load a broken plist |
| 3 | Create `~/Library/Logs/ollarma/` | Yes |
| 4 | `launchctl bootout gui/<uid>/com.byron.ollarma` (best-effort; tolerates 113/3) | No |
| 5 | `launchctl bootstrap gui/<uid> <plist>` | Yes — loud failure with EIO retry |
| 6 | `launchctl enable gui/<uid>/com.byron.ollarma` | Yes |
| 7 | `launchctl kickstart -kp gui/<uid>/com.byron.ollarma` | **No** (Phase 55 fix: TimeoutExpired is non-fatal — bootstrap already succeeded) |
| 8 | `launchctl list` + `launchctl print` for operator confirmation | No (check=False) |

**Phase 55 kickstart note:** If Step 7 times out, the installer prints a yellow
warning and continues to the verification step. The service will start under launchd
KeepAlive control regardless. Run `ollarma startup-smoke` to confirm.

**EIO retry (Step 5):** If bootstrap returns exit code 5 (EIO), the installer runs
a second `_bootout_and_wait()` + retry before hard-failing. This handles the race
where a KeepAlive child has not fully exited after bootout.

All subprocess calls use `shell=False` and a 10-second timeout. The plist includes
`ThrottleInterval=10` and `ProcessType=Interactive`.

Log paths:

```
~/Library/Logs/ollarma/stdout.log
~/Library/Logs/ollarma/stderr.log
```

---

## 2. Verify — startup-smoke

After login or restart, validate the full persistent-service chain:

```bash
ollarma startup-smoke
```

Exit codes:

| Code | Meaning |
|------|---------|
| 0 | Service is `ready` or `degraded` — healthy for most use |
| 1 | Service is `blocked`, HTTP unreachable, or launchd label missing |

What the smoke command probes:

1. `launchctl list com.byron.ollarma` — confirms launchd knows the label
2. `GET /health` — top-level `status` field (`ready` / `degraded` / `blocked`)
3. `GET /startup/readiness` — full `StartupReadinessPayload` (schema_version=1)
4. `GET /models/status` — helper model availability
5. `GET /metrics` — Prometheus endpoint reachable

Curl equivalents for manual inspection:

```bash
# Folded status + readiness
curl -sS http://127.0.0.1:8484/health | jq '.status, .startup_readiness.status'

# Full readiness payload
curl -sS http://127.0.0.1:8484/startup/readiness | jq

# Models
curl -sS http://127.0.0.1:8484/models/status | jq
```

`/health` status is **never** `"ok"` in v4.5 — the legacy `"ok"` value was removed so
degraded startup cannot hide behind a passing status string.

Persisted readiness snapshot: `.ollarma/startup/readiness.json` (gitignored;
refreshed atomically on each service start).

---

## 3. Residency Policy (Phase 52)

GPU residency policy controls which models are kept warm in unified memory.

### Policy constants

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Rescue model | `qwen3:1.7b` | ~1.1 GB Q4 — always available; fits in any residual budget |
| Opportunistic model | `phi4-mini` | ~2.3 GB Q4 — warmed when swap < 512 MB |
| Swap eviction threshold | 512 MB | SWAP_DEGRADED threshold; matches scheduler guard |
| Benchmark freeze flag | `.ollarma/benchmark_active.flag` | Policy mutations blocked during benchmark runs |

### Residency decision rules

1. **Rescue pin**: `qwen3:1.7b` is always pinned. It is never evicted by policy.
2. **Opportunistic warm**: `phi4-mini` is warmed when `swap_used_mb < 512 MB` and
   `BENCHMARK_ACTIVE` flag is absent.
3. **Swap eviction**: When `swap_used_mb ≥ 512 MB`, opportunistic models are evicted.
   The rescue model is unaffected.
4. **Benchmark freeze**: When `.ollarma/benchmark_active.flag` exists, policy applies
   no new mutations — existing residency is preserved for measurement stability.

### Inspect current residency

```bash
curl -sS http://127.0.0.1:8484/health | jq '.startup_readiness.pipeline'
```

The `pipeline` field in the readiness payload lists loaded model names and the
telemetry source. `StartupResidencyPosture` (embedded in the readiness payload
since Phase 52) shows whether the rescue model is resident.

---

## 4. Routing Ladder (Phase 53)

The routing ladder selects the local model for each `route_prompt` request by
workload class, degrades under swap pressure, and issues an explicit escalation
receipt when no local path is viable.

### Ladder rungs and ordering

1. Explicit caller `model=` override → `LADDER_USER_OVERRIDE` (ladder still
   computed for observability; chosen_model = override).
2. Benchmark winner for the workload class (`resolve_ranked_selection` rank-1).
3. Pareto frontier alternates from the benchmark artifact (ranked by score).
4. Resident models get a tiebreak advantage within the same rank tier.
5. `qwen3:1.7b` (rescue) appended as last rung (deduplicated if already present).
6. If swap > 512 MB: only resident models and the rescue model pass the filter.
7. Empty ladder after filtering → `blocked_escalate` + explicit escalation receipt.

### Swap-aware degradation

| Swap state | Ladder behavior | Status |
|-----------|-----------------|--------|
| `swap_used_mb < 512` | Full ladder — all viable local models | `chose_preferred` or `chose_degraded` |
| `swap_used_mb ≥ 512` | Resident + rescue only | `chose_degraded` or `chose_rescue` |
| No local path survives | Escalation receipt issued | `blocked_escalate` |

### Observability

Current ladder state is available on `/health` without adding new endpoints:

```bash
curl -sS http://127.0.0.1:8484/health | jq '.last_routing_ladder'
```

Fields: `status`, `chosen_model`, `reason_code`, `detail`, `escalation_hint`.

`RouteResult.ladder` (in the `/route` response) contains the full `LadderDecision`
for that request, including `rungs_considered` and the workload class used.

### Reason codes

| Code | Meaning |
|------|---------|
| `LADDER_PREFERRED` | Benchmark winner chosen; no pressure |
| `LADDER_DEGRADED_SWAP` | Swap pressure reduced ladder to resident/rescue |
| `LADDER_DEGRADED_RESIDENCY` | Residency filter reduced ladder |
| `LADDER_RESCUE_ONLY` | Only rescue model available |
| `LADDER_BLOCKED_NO_LOCAL` | No local path — escalation receipt issued |
| `LADDER_USER_OVERRIDE` | Caller specified explicit model |

---

## 5. Proof Reproduction (Phase 54 — deferred)

**Phase 54 is deferred.** The host currently has elevated swap usage (SWAP_DEGRADED
state with ~7836 MB swap). The PIPE-10 guard blocks live autopilot and
`ollarma run --suites code --trials 3` until swap pressure is resolved.

### Operator action required to unblock Phase 54

1. Quit OrbStack (primary swap consumer on this machine), OR
2. Reboot to reset unified memory state.
3. Verify: `ollarma startup-smoke` exits 0 with status `ready` (not `degraded`).
4. Then run:

```bash
# Live autopilot proof (Phase 54 scope)
ollarma run --suites code --trials 3

# Confirm route receipts stayed local
cat results/latest.json | jq '.results[].route_receipt.lane'
# Expected: all "local" — no "frontier_or_human"

# Confirm routing ladder chose a local model
cat results/latest.json | jq '.results[].route_receipt.ladder.chosen_model'
```

5. Inspect `.ollarma/startup/readiness.json` for the live readiness snapshot
   captured at the time of the run.

Once Phase 54 is executed, update this section with the actual proof run results
and commit refs.

---

## 6. Troubleshooting

### SELECTION_STALE

The benchmark artifact (`results/latest.json`) is missing or stale.
Routing ladder falls back to rescue model (`qwen3:1.7b`).

```bash
ollarma run --dry-run   # refreshes the selection artifact with one inference
```

### SWAP_DEGRADED

Swap usage exceeds the 512 MB threshold. The scheduler rejects new work on
inference endpoints. Response behavior differs by route (tracked as DEBT-07
for parity work in v5.0):

- **`/route`, `/chat`** — HTTP 429 + `Retry-After: 30`
- **`/workflow`** — HTTP 409 with `reason_code: "SWAP_DEGRADED"` in the
  response body (no `Retry-After` header; `submit_workflow` catches the
  admission error at the service layer and returns a structured rejection
  instead of re-raising)
- **Other inference routes** (`/autopilot`, `/pipelines/*`,
  `/agents/{name}/run`, `/v1/sibling/read`, `/macfind/query`) — currently
  HTTP 500 on `SchedulerAdmissionError`; tracked as DEBT-08 for middleware
  normalization in v5.0

```bash
# Check current swap
vm_stat | grep "Pages swapped"

# Primary resolution: quit OrbStack
# Secondary: reboot

# Verify recovery
ollarma startup-smoke
```

### Bootstrap EIO (exit code 5)

`launchctl bootstrap` returned Input/Output error. The installer retries once
after a second `_bootout_and_wait()`. If it fails twice:

```bash
# Manual recovery
launchctl bootout gui/$(id -u)/com.byron.ollarma
sleep 2
ollarma start --install
```

### Kickstart timeout (Phase 55 fix)

If `ollarma start --install` prints:

```
Warning: launchctl kickstart timed out — bootstrap already succeeded; the service
will start under launchd control. Run `ollarma startup-smoke` in a few seconds to verify.
```

This is non-fatal. Wait 5–10 seconds, then:

```bash
ollarma startup-smoke
```

If the smoke check exits 0, the service is running normally. The kickstart timeout
is cosmetic — launchd KeepAlive ensures the service starts regardless.

### OrbStack eating memory

OrbStack is the primary source of swap pressure on this machine. Quitting it
frees 4–6 GB of unified memory, typically resolving SWAP_DEGRADED within 30
seconds. Use `ollarma startup-smoke` to confirm recovery.

### launchd service missing from list

```bash
# Verify the plist is installed
ls -la ~/Library/LaunchAgents/com.byron.ollarma.plist

# Re-install if missing
ollarma start --install

# Confirm
launchctl list com.byron.ollarma
```

### HTTP service unreachable (startup-smoke exit 1)

```bash
# Check if process is running
ps aux | grep ollarma

# Check logs
tail -50 ~/Library/Logs/ollarma/stderr.log

# Manual start (bypasses launchd — for debugging only)
ollarma serve --host 127.0.0.1 --port 8484
```
