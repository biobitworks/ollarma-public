#!/usr/bin/env bash
# promote_v5.sh — operator-facing helper to promote v5.0-rc1 → v5.0
#
# Precondition: you have physically installed provider keys in macOS Keychain
# and are ready to authorize cost on Anthropic. This script WILL spend money
# (~$3-5 of Anthropic API usage for 300 claude-haiku-4-5 runs) and WILL
# require sudo for swap clearance.
#
# What this script does (each step logs to stdout + stderr):
#   1. Precondition checks — fail loudly with a single remediation line each
#   2. Fetch SWE-bench Lite if not cached
#   3. Dual-lane run (local + frontier) against the configured subset
#   4. Build leaderboard + run verify-chain
#   5. Print the exact EXP-1.2 SIG entry seed for operator to append to notebook
#
# This script does NOT:
#   - Install Keychain entries for you (you do `security add-generic-password`)
#   - Clear swap for you (you do `sudo purge` or reboot)
#   - Sign the EXP-1.2 lab-notebook entry (you append + sign after reviewing)
#   - Tag v5.0 (you run `git tag -a v5.0 -m "..."` after signing)
#
# Honest posture: every action is printed BEFORE it runs. No silent work.

set -euo pipefail

SUBSET="${SUBSET:-first-300}"
PROJECT="${PROJECT:-demo}"
VK="${VK:-vk_swebench}"
MODEL_FRONTIER="${MODEL_FRONTIER:-claude-haiku-4-5-latest}"
OLLARMA="${OLLARMA:-.venv/bin/python3.13 -m ollarma.cli}"

log() { printf "[promote_v5] %s\n" "$*"; }
fail() { printf "[promote_v5] FAIL: %s\n" "$*" >&2; exit 1; }

# --- precondition checks ---
log "step 1/5: precondition checks"

if ! security find-generic-password -s ollarma-anthropic >/dev/null 2>&1; then
  fail "ANTHROPIC Keychain missing. Install: security add-generic-password -s ollarma-anthropic -a \$USER -w"
fi
log "  anthropic keychain: present"

SWAP_MB=$(sysctl -n vm.swapusage | awk '{print $7}' | tr -d 'M')
SWAP_INT=${SWAP_MB%.*}
if [ "${SWAP_INT:-9999}" -gt 512 ]; then
  fail "swap ${SWAP_MB}MB exceeds 512MB threshold (SWAP_DEGRADED). Clear via: sudo purge  OR reboot"
fi
log "  swap: ${SWAP_MB}MB (under 512MB threshold)"

if ! curl -sS --max-time 3 127.0.0.1:8484/health >/dev/null 2>&1; then
  fail "ollarma service unreachable on 127.0.0.1:8484. Start via: ollarma start --install  OR launchctl kickstart gui/\$(id -u)/com.byron.ollarma"
fi
log "  service: reachable"

if ! curl -sS --max-time 3 https://api.anthropic.com/v1/messages >/dev/null 2>&1; then
  fail "api.anthropic.com unreachable. Check network before burning local time + cost."
fi
log "  network (anthropic): reachable"

# --- verify gateway config has allowlist entry + vk ---
log ""
log "step 2/5: gateway config sanity"
log "  ensure .planning/config.json has:"
log "    features.gateway.enabled = true"
log "    features.gateway.allowlist includes '${PROJECT}'"
log "    features.gateway.virtual_keys includes {id:'${VK}', keychain_service:'ollarma-anthropic', provider:'anthropic'}"
log "  (not auto-edited — operator review required)"
printf "  press ENTER to continue, Ctrl-C to abort: "
read -r _ACK

# --- fetch dataset ---
log ""
log "step 3/5: fetch SWE-bench Lite (idempotent if cached)"
$OLLARMA swe-bench fetch

# --- dual-lane run ---
log ""
log "step 4/5: dual-lane run subset=${SUBSET}"
log "  command (local):    $OLLARMA swe-bench run --lane local    --subset ${SUBSET} --project ${PROJECT}"
log "  command (frontier): $OLLARMA swe-bench run --lane frontier --subset ${SUBSET} --provider anthropic --model ${MODEL_FRONTIER} --virtual-key-id ${VK} --live"
log "  about to run both lanes. expected duration: 30-90 min. expected frontier cost: ~\$3-5."
printf "  press ENTER to begin, Ctrl-C to abort: "
read -r _ACK

LOCAL_LOG="results/promote-v5-local-$(date -u +%Y%m%dT%H%M%SZ).log"
FRONTIER_LOG="results/promote-v5-frontier-$(date -u +%Y%m%dT%H%M%SZ).log"
mkdir -p results

log "  running local lane (logs: ${LOCAL_LOG})"
$OLLARMA swe-bench run --lane local --subset "${SUBSET}" --project "${PROJECT}" 2>&1 | tee "${LOCAL_LOG}"

log "  running frontier lane (logs: ${FRONTIER_LOG})"
$OLLARMA swe-bench run --lane frontier --subset "${SUBSET}" --provider anthropic --model "${MODEL_FRONTIER}" --virtual-key-id "${VK}" --live 2>&1 | tee "${FRONTIER_LOG}"

# --- leaderboard + verify ---
log ""
log "step 5/5: build leaderboard + verify chain"
RUN_ID=$(ls -t .ollarma/benchmarks/swe-bench-lite/runs/ | head -n 1)
[ -n "${RUN_ID}" ] || fail "no run_id found under .ollarma/benchmarks/swe-bench-lite/runs/"
log "  run_id: ${RUN_ID}"

$OLLARMA swe-bench leaderboard "${RUN_ID}"

# --- SIG entry seed for EXP-1.2 measured-claim signature ---
log ""
log "===================================================================="
log "EXP-1.2 measured-claim SIG entry seed"
log "===================================================================="
HANDOFF_SHA=$(shasum -a 256 "results/swe-bench-lite-${RUN_ID}.json" | awk '{print $1}' || echo "MISSING")
COMMIT=$(git rev-parse --short HEAD)
TS=$(date -u +%Y%m%dT%H%M%SZ)
SEED="${TS}|EXP-1.2-measured|${RUN_ID}|${COMMIT}|${HANDOFF_SHA}"
HASH4=$(echo -n "${SEED,,}" | shasum -a 256 | cut -c1-4 2>/dev/null || echo -n "$(echo "$SEED" | tr '[:upper:]' '[:lower:]')" | shasum -a 256 | cut -c1-4)
log ""
log "  append the following entry to .gsigmad/LAB_NOTEBOOK.md:"
log ""
cat <<EOF

## SIG-${TS}-claude-${HASH4}

**Claim type:** measured (CONFIRMATORY against EXP-1.2)
**Milestone:** v5.0 Gateway / Frontier / SWE-bench — promotion v5.0-rc1 → v5.0
**Run ID:** ${RUN_ID}
**Leaderboard SHA256:** ${HANDOFF_SHA}
**Signed at:** ${TS}

(populate measured pass@1 for local + frontier + total cost_usd from leaderboard)

### Signature

Entry seed (lowercased): ${SEED,,}
Compute sha256 → first 4 hex chars → ${HASH4}.

EOF

log ""
log "after appending + committing the signed entry:"
log "  git tag -a v5.0 -m 'v5.0 promoted from rc1; HG-3 closed by operator live run ${RUN_ID}'"
log "  git push --tags"
log ""
log "promote_v5.sh complete. substrate is your witness; operator signs the claim."
