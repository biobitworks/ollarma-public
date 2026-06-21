#!/usr/bin/env bash
# modelbench_launcher.command — double-click control panel for the magicSTUDIObox
# local-model benchmark (EXP-MAXMODEL). Double-click in Finder (opens in Terminal),
# or run from Watchtower. Safe: read-only options never touch the running sweep;
# mutating options (migration/cutover) self-guard and confirm.
REPO="<repo>"
EXP="$REPO/experiments/maxmodel"
DB="/Volumes/magicBLACKbox/ollarma-experiments/maxmodel/db"
PY="$REPO/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$REPO" || { echo "repo not found: $REPO"; read -r; exit 1; }

pause(){ echo; read -r -p "press return to continue..."; }
sweep_alive(){ pgrep -f maxmodel_probe >/dev/null 2>&1; }

status(){
  echo "===== magicSTUDIObox model-bench status ====="
  if sweep_alive; then echo "SWEEP: RUNNING"; else echo "SWEEP: stopped"; fi
  echo "--- progress ---"; cat "$DB/progress.json" 2>/dev/null
  echo; echo "--- local-model watchdog (latest) ---"; cat "$DB/watchdog_status.txt" 2>/dev/null || echo "(watchdog not running)"
  echo; echo "--- models benchmarked ---"
  "$PY" - <<'P' 2>/dev/null
import json
d=json.load(open("/Volumes/magicBLACKbox/ollarma-experiments/maxmodel/db/models_db.json"))
ok=[x for x in d if x.get("status")=="ok"]
print(f"  {len(ok)} ok / {len(d)} total")
P
}

while true; do
  clear 2>/dev/null
  echo "############################################################"
  echo "#  magicSTUDIObox  —  Ollarma local-model benchmark        #"
  echo "############################################################"
  if sweep_alive; then echo "  [sweep RUNNING — mutating actions are blocked]"; else echo "  [sweep idle — all actions available]"; fi
  echo
  echo "  1) Live status (sweep + local-model watchdog)"
  echo "  2) View REPORT.md   3) View MATRIX.md   4) Re-synthesize matrix"
  echo "  5) Drive A/B load benchmark (internal vs magicBLACKbox)"
  echo "  6) Compare drive benchmark results"
  echo "  7) Migration: DRY-RUN (safe plan)"
  echo "  8) Migration: APPLY  (copy+verify, reversible)"
  echo "  9) Migration: SWITCH (cutover — restarts ollama)"
  echo " 10) Tail watchdog log     11) Open JSON DB folder"
  echo " 12) STRESS TEST (large model saturates resources)"
  echo " 13) STRESS TEST --escalate (stack 2 large models -> swap line)"
  echo " 14) Publish matrix -> Watchtower summary JSON"
  echo " --- GPU stop/start boundaries ---"
  echo " 15) GPU status (FREE/BUSY/STRESS + budget)"
  echo " 16) GPU START a worker model      17) GPU STOP (release GPU to floor)"
  echo " 18) STOP stress job               19) Burst budget (Kaggle 30h/wk)"
  echo "  0) Quit"
  echo
  read -r -p "choose: " c
  case "$c" in
    1) status; pause ;;
    2) ${PAGER:-less} "$EXP/REPORT.md" ;;
    3) ${PAGER:-less} "$EXP/MATRIX.md" ;;
    4) "$PY" "$EXP/synthesize.py"; pause ;;
    5) if sweep_alive; then echo "blocked: sweep running (contaminates load timing)"; else "$PY" "$EXP/drive_loadbench.py"; fi; pause ;;
    6) "$PY" "$EXP/drive_loadbench.py" --compare; pause ;;
    7) bash "$REPO/scripts/migrate_ollama_store.sh"; pause ;;
    8) bash "$REPO/scripts/migrate_ollama_store.sh" --apply; pause ;;
    9) echo "CUTOVER restarts ollama (brief floor drop)."; read -r -p "type 'switch' to proceed: " s
       [ "$s" = "switch" ] && bash "$REPO/scripts/migrate_ollama_store.sh" --switch --yes || echo "cancelled"; pause ;;
    10) echo "(ctrl-c to stop)"; tail -f "$EXP/watchdog.log" ;;
    11) open "$DB/.." 2>/dev/null; pause ;;
    12) if sweep_alive; then echo "blocked: sweep running"; else "$PY" "$EXP/stress_test.py" --duration 60; fi; pause ;;
    13) if sweep_alive; then echo "blocked: sweep running"; else "$PY" "$EXP/stress_test.py" --escalate --duration 60; fi; pause ;;
    14) "$PY" "$EXP/publish_matrix.py"; pause ;;
    15) "$PY" "$EXP/gpu_control.py" status; pause ;;
    16) read -r -p "model to load (e.g. qwen3-coder:30b): " mm; [ -n "$mm" ] && "$PY" "$EXP/gpu_control.py" start --model "$mm"; pause ;;
    17) "$PY" "$EXP/gpu_control.py" stop --all; pause ;;
    18) "$PY" "$EXP/gpu_control.py" stop-stress; pause ;;
    19) "$PY" "$EXP/gpu_control.py" budget; pause ;;
    0|q|Q) echo "bye"; exit 0 ;;
    *) ;;
  esac
done
