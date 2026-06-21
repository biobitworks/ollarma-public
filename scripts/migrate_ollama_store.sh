#!/usr/bin/env bash
# migrate_ollama_store.sh — move the Ollama model store from the slow USB SSD
# (CELLICO_FAST) to the fast Thunderbolt/USB4 NVMe (magicBLACKbox), with guards,
# dry-run, full sha256 verification, decoupled cutover, and rollback.
#
# Phases are SEPARATE and explicit so the safe parts never trigger the risky part:
#   (default)   --dry-run   plan + rsync --dry-run + space/guard checks, NO changes
#   --apply     copy SRC -> DST (rsync), then verify EVERY blob's sha256, write sentinel
#   --switch    repoint OLLAMA_MODELS -> DST, restart ollama, re-pin floor, verify
#   --rollback  repoint OLLAMA_MODELS back to SRC, restart, re-pin (undo --switch)
#
# Guards: refuses to touch anything while the EXP-MAXMODEL sweep or an `ollama pull`
# is running. --switch refuses unless --apply completed + verification passed.
#
# Usage:
#   scripts/migrate_ollama_store.sh                 # dry-run (safe; default)
#   scripts/migrate_ollama_store.sh --apply         # copy + verify (reversible; ollama keeps running on SRC)
#   scripts/migrate_ollama_store.sh --switch --yes   # cutover (restarts ollama; brief floor drop)
#   scripts/migrate_ollama_store.sh --rollback --yes # undo cutover
set -euo pipefail

SRC="${OLLAMA_SRC:-/Volumes/CELLICO_FAST/cellico/models/ollama}"
DST="${OLLAMA_DST:-/Volumes/magicBLACKbox/ollama-models}"
PINNED=("qwen2.5:1.5b" "nomic-embed-text:latest")
SENTINEL="${DST}/.MIGRATION_VERIFIED"
RECEIPTS="/Volumes/magicBLACKbox/ollarma-experiments/maxmodel/db/migration_receipts.jsonl"
OLLAMA_HOST_URL="http://127.0.0.1:11434"
MODE="--dry-run"; ASSUME_YES=0; QUICK=0

for a in "$@"; do
  case "$a" in
    --dry-run|--apply|--switch|--rollback) MODE="$a" ;;
    --yes) ASSUME_YES=1 ;;
    --quick) QUICK=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
receipt() { mkdir -p "$(dirname "$RECEIPTS")" 2>/dev/null || true; printf '{"ts":"%s","phase":"%s","msg":%s}\n' "$(ts)" "$MODE" "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1")" >> "$RECEIPTS" 2>/dev/null || true; }
die() { log "ABORT: $*"; receipt "ABORT: $*"; exit 1; }

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  read -r -p "$1 [type 'yes' to proceed]: " r; [ "$r" = "yes" ] || die "user declined"
}

# ---- guards shared by all mutating phases ----
guard_no_sweep() {
  pgrep -f maxmodel_probe >/dev/null 2>&1 && die "EXP-MAXMODEL sweep is RUNNING — wait for it to finish (it writes to SRC)."
  pgrep -f "ollama (pull|cp|create)" >/dev/null 2>&1 && die "an 'ollama pull/cp/create' is in flight — wait for it."
}
guard_paths() {
  [ -d "$SRC/blobs" ] || die "SRC not found or not an ollama store: $SRC"
  mount | grep -q "$(dirname "$DST" | sed 's#/[^/]*$##')" || true
  [ -d "/Volumes/magicBLACKbox" ] || die "magicBLACKbox not mounted"
}

space_check() {
  local need avail
  need=$(du -sk "$SRC" | awk '{print $1}')
  avail=$(df -k /Volumes/magicBLACKbox | tail -1 | awk '{print $4}')
  log "SRC size: $((need/1024/1024)) GB · magicBLACKbox free: $((avail/1024/1024)) GB"
  [ "$avail" -gt "$need" ] || die "not enough free space on magicBLACKbox"
}

# ---- phases ----
phase_dryrun() {
  guard_paths; space_check
  log "DRY-RUN — no changes. Would rsync:"
  log "  SRC = $SRC"
  log "  DST = $DST"
  pgrep -f maxmodel_probe >/dev/null 2>&1 && log "  NOTE: sweep is RUNNING now — --apply will refuse until it finishes." || log "  sweep: not running (apply allowed)"
  rsync -a --delete --dry-run --stats "$SRC/" "$DST/" 2>/dev/null | tail -20 || true
  log "Next: scripts/migrate_ollama_store.sh --apply"
}

phase_apply() {
  guard_no_sweep; guard_paths; space_check
  log "COPY $SRC -> $DST (rsync -a --delete; ollama keeps serving from SRC, reversible)"
  mkdir -p "$DST"
  rsync -a --delete --info=progress2 "$SRC/" "$DST/"
  log "rsync done. Verifying blob integrity (sha256 == filename)..."
  rm -f "$SENTINEL"
  local bad=0 n=0
  if [ "$QUICK" = 1 ]; then
    log "(--quick) re-running rsync with --checksum as the verification pass"
    rsync -a --delete --checksum --stats "$SRC/" "$DST/" | tail -5
  else
    while IFS= read -r blob; do
      n=$((n+1))
      local want got
      want="$(basename "$blob" | sed 's/^sha256-//')"
      got="$(shasum -a 256 "$blob" | awk '{print $1}')"
      if [ "$want" != "$got" ]; then bad=$((bad+1)); log "  MISMATCH: $(basename "$blob")"; fi
      [ $((n % 50)) -eq 0 ] && log "  verified $n blobs..."
    done < <(find "$DST/blobs" -type f -name 'sha256-*')
    log "verified $n blobs, $bad mismatches"
    [ "$bad" -eq 0 ] || die "$bad blob(s) failed sha256 — DST not trustworthy, NOT switching"
  fi
  printf '{"verified_at":"%s","src":"%s","blobs":%s}\n' "$(ts)" "$SRC" "${n:-0}" > "$SENTINEL"
  receipt "apply+verify OK ($n blobs)"
  log "OK. DST verified. Next (maintenance window): scripts/migrate_ollama_store.sh --switch --yes"
}

restart_ollama() {
  # homebrew-managed `ollama serve` (pid uses /opt/homebrew/opt/ollama/bin/ollama serve)
  if command -v brew >/dev/null && brew services list 2>/dev/null | grep -q '^ollama'; then
    log "restarting via: brew services restart ollama"
    brew services restart ollama
  else
    log "brew service not found; killing + relaunching ollama serve"
    pkill -f "ollama serve" || true; sleep 2
    nohup ollama serve >/tmp/ollama_serve.log 2>&1 & disown
  fi
}

set_models_env() {
  local val="$1"
  # 1) login-session launchd env (covers GUI + brew-launched children started after)
  launchctl setenv OLLAMA_MODELS "$val" || true
  # 2) homebrew plist EnvironmentVariables (persistent across reboots), if present
  local plist
  for plist in "$HOME/Library/LaunchAgents/homebrew.mxcl.ollama.plist" \
               "/opt/homebrew/opt/ollama/homebrew.mxcl.ollama.plist"; do
    if [ -f "$plist" ]; then
      cp "$plist" "$plist.bak.$(date +%s)"
      /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OLLAMA_MODELS $val" "$plist" 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OLLAMA_MODELS string $val" "$plist" 2>/dev/null || true
      log "updated plist: $plist (backup written)"
    fi
  done
}

verify_live_store() {
  sleep 4
  local got
  got=$(curl -s -m 8 "$OLLAMA_HOST_URL/api/tags" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len(d.get("models",[])))' 2>/dev/null || echo 0)
  log "ollama now lists $got models"
  [ "${got:-0}" -gt 0 ] || die "ollama returned 0 models after restart — check OLLAMA_MODELS / mount; consider --rollback"
}

repin_floor() {
  for m in "${PINNED[@]}"; do
    curl -s -m 60 "$OLLAMA_HOST_URL/api/generate" \
      -d "{\"model\":\"$m\",\"prompt\":\"ok\",\"stream\":false,\"keep_alive\":-1,\"options\":{\"num_predict\":1}}" >/dev/null \
      && log "re-pinned $m (keep_alive=-1)" || log "WARN: could not re-pin $m"
  done
}

phase_switch() {
  guard_no_sweep; guard_paths
  [ -f "$SENTINEL" ] || die "no verified DST sentinel — run --apply first (and it must pass verification)"
  log "CUTOVER: point OLLAMA_MODELS -> $DST and restart ollama (brief floor drop)."
  confirm "Proceed with cutover? ollama will restart and the pinned floor drops for a few seconds"
  set_models_env "$DST"
  restart_ollama
  verify_live_store
  repin_floor
  receipt "switched OLLAMA_MODELS -> $DST"
  log "DONE. Live store = $DST. Verify: ollama list ; curl $OLLAMA_HOST_URL/api/ps"
  log "Old store still intact at $SRC (retire it only after a few days of confidence)."
}

phase_rollback() {
  guard_no_sweep
  log "ROLLBACK: point OLLAMA_MODELS back to $SRC and restart."
  confirm "Roll back to $SRC?"
  set_models_env "$SRC"
  restart_ollama
  verify_live_store
  repin_floor
  receipt "rolled back OLLAMA_MODELS -> $SRC"
  log "DONE. Live store = $SRC."
}

case "$MODE" in
  --dry-run) phase_dryrun ;;
  --apply)   phase_apply ;;
  --switch)  phase_switch ;;
  --rollback) phase_rollback ;;
esac
