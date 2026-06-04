#!/bin/bash
# Corrected full build: room-grounded probe + matched reach thresholds + fallback.
# 6-way parallel (GPU0 vlm-jupyter-180 + GPU3 vlm-isaac-g3). File-based split/merge (no stdin bug).
set -u
D=/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao
SF="$D/full_scenarios_extracted"
C0=vlm-jupyter-180; C3=vlm-isaac-g3; NW=6; PY=/isaac-sim/python.sh
LOG="$D/build_v2.log"; : > "$LOG"
ts(){ date -u +%H:%M:%S; }
cont(){ [ "$1" -lt 3 ] && echo "$C0" || echo "$C3"; }
echo "[V2 $(ts)] START NW=$NW" >> "$LOG"
mapfile -t SCENES < <(ls -d "$SF"/*_full_physics_scene | xargs -n1 basename | sort)
N=${#SCENES[@]}; echo "[V2 $(ts)] $N scenes" >> "$LOG"

# Phase 1: parallel probe (room-grounded seed)
pids=()
for w in $(seq 0 $((NW-1))); do
  sub=""; for ((i=w; i<N; i+=NW)); do sub="$sub,${SCENES[$i]}"; done; sub="${sub#,}"
  C=$(cont "$w")
  docker exec -e CUDA_VISIBLE_DEVICES=0 -e SCENES="$sub" "$C" $PY "$D/probe_stage.py" \
    > "$D/par_probe_$w.log" 2>&1 &
  pids+=($!); sleep 2
done
for p in "${pids[@]}"; do wait "$p"; done
echo "[V2 $(ts)] probe DONE ($(ls "$SF"/*/scene_facts.json 2>/dev/null | wc -l) facts)" >> "$LOG"

# Phase 2: generate (reach_ok 1.0/1.5 thresholds + fallback)
docker exec "$C0" $PY "$D/generate_tasks.py" >> "$LOG" 2>&1
echo "[V2 $(ts)] generate DONE" >> "$LOG"

# Phase 3: split (file) -> parallel validate -> merge+stats (file)
docker exec "$C0" $PY "$D/_split_tasks.py" "$NW" >> "$LOG" 2>&1
pids=()
for w in $(seq 0 $((NW-1))); do
  C=$(cont "$w")
  docker exec -e CUDA_VISIBLE_DEVICES=0 -e VAL_TASKS_JSON="$D/_val_shard_$w.json" \
    -e VAL_VALID_OUT="$D/_val_shard_${w}_valid.json" -e VAL_REPORT_OUT="$D/_val_shard_${w}_report.json" \
    -e VAL_FIX=1 "$C" $PY "$D/validate_all_spawns.py" > "$D/par_val_$w.log" 2>&1 &
  pids+=($!); sleep 2
done
for p in "${pids[@]}"; do wait "$p"; done
echo "[V2 $(ts)] validate DONE" >> "$LOG"
docker exec "$C0" $PY "$D/_merge_stats.py" "$NW" >> "$LOG" 2>&1
echo "[V2 $(ts)] ALL DONE" >> "$LOG"
