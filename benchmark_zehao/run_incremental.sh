#!/bin/bash
set -u
D="$(cd "$(dirname "$0")" && pwd)"
C0=vlm-jupyter-180; C3=vlm-isaac-g3; NW=6; PY=/isaac-sim/python.sh
LOG="$D/incremental.log"; : > "$LOG"; ts(){ date -u +%H:%M:%S; }
cont(){ [ "$1" -lt 3 ] && echo "$C0" || echo "$C3"; }
mapfile -t F < "$D/_failed_scenes.txt"; NF=${#F[@]}
SUB=$(printf "%s," "${F[@]}"); SUB="${SUB%,}"
echo "[INCR $(ts)] START re-processing $NF failed scenes" >> "$LOG"
pids=()
for w in $(seq 0 $((NW-1))); do
  sub=""; for ((i=w; i<NF; i+=NW)); do sub="$sub,${F[$i]}"; done; sub="${sub#,}"
  [ -z "$sub" ] && continue
  C=$(cont "$w")
  docker exec -e CUDA_VISIBLE_DEVICES=0 -e SCENES="$sub" "$C" $PY "$D/probe_stage.py" > "$D/par_iprobe_$w.log" 2>&1 &
  pids+=($!); sleep 2
done
for p in "${pids[@]}"; do wait "$p"; done
echo "[INCR $(ts)] probe done" >> "$LOG"
docker exec -e SCENES="$SUB" "$C0" $PY "$D/generate_tasks.py" >> "$LOG" 2>&1
echo "[INCR $(ts)] generate done" >> "$LOG"
docker exec "$C0" $PY "$D/_split_tasks.py" "$NW" >> "$LOG" 2>&1
pids=()
for w in $(seq 0 $((NW-1))); do
  C=$(cont "$w")
  docker exec -e CUDA_VISIBLE_DEVICES=0 -e VAL_TASKS_JSON="$D/_val_shard_$w.json" \
    -e VAL_VALID_OUT="$D/_val_shard_${w}_valid.json" -e VAL_REPORT_OUT="$D/_val_shard_${w}_report.json" \
    -e VAL_FIX=1 "$C" $PY "$D/validate_all_spawns.py" > "$D/par_ival_$w.log" 2>&1 &
  pids+=($!); sleep 2
done
for p in "${pids[@]}"; do wait "$p"; done
echo "[INCR $(ts)] validate done" >> "$LOG"
docker exec "$C0" $PY "$D/_merge_incremental.py" "$NW" >> "$LOG" 2>&1
echo "[INCR $(ts)] ALL DONE" >> "$LOG"
