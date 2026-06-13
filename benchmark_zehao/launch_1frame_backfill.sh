#!/bin/bash
# Backfill the 46 MISSING 1frame tasks (the ones lost when the root disk filled
# during the original 1frame run) with the CURRENT code (room-boundary gate +
# render-quality guard), N_FRAMES=1. Outputs to a SEPARATE folder so the new-code
# results never mix with the old 287 (old code, no room-boundary). "Done" is read
# from BOTH the old 1frame folder and this new folder, so re-runs are idempotent.
#
# Usage: bash launch_1frame_backfill.sh [N_WORKERS=3]
set -e
WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

NW=${1:-3}
OUT_BATCH="eval_30B_1frame_backfill"          # NEW folder for the 46
SKIP_FROM="eval_30B_333_1frame"               # old 287 count as done
export N_FRAMES=1
export ROOM_BOUNDARY=1
TASKS_JSON="$WORKDIR/benchmark_tasks_generated_validated.json"
VLLM_URL="http://localhost:8300/v1/chat/completions"
ISAAC_IMAGE="nvcr.io/nvidia/isaac-sim:4.5.0"
NVOPTIX_HOST="/usr/share/nvidia/nvoptix.bin"

echo "[BACKFILL] N_FRAMES=$N_FRAMES ROOM_BOUNDARY=$ROOM_BOUNDARY | $NW workers | out=$OUT_BATCH | skip-from=$SKIP_FROM,$OUT_BATCH | $(date)"

# ── Compute the remaining tasks (master - done), done = old folder + new folder ──
python3 - <<PYEOF
import json, glob, os
WORKDIR="$WORKDIR"
master = json.load(open("$TASKS_JSON"))["tasks"]
done = set()
for batch in ["$SKIP_FROM", "$OUT_BATCH"]:
    for rj in glob.glob(f"{WORKDIR}/results/{batch}/*/*/results.json"):
        d = os.path.basename(os.path.dirname(rj))
        done.add("_".join(d.split("_")[:-2]))
remaining = [t for t in master if t["id"] not in done]
print(f"[BACKFILL] master={len(master)} done={len(done)} remaining={len(remaining)}")
json.dump({"tasks": remaining}, open("/tmp/backfill_1frame.json", "w"))
PYEOF

NREM=$(python3 -c "import json;print(len(json.load(open('/tmp/backfill_1frame.json'))['tasks']))")
if [ "$NREM" -eq 0 ]; then echo "[BACKFILL] nothing to do"; exit 0; fi
if [ "$NREM" -gt 60 ]; then
  echo "[BACKFILL] ABORT: remaining=$NREM > 60 — expected ~46. Refusing to run a full sweep."; exit 1
fi

TASKS_JSON="/tmp/backfill_1frame.json" python3 "$WORKDIR/parallel_split.py" "$NW"

for w in $(seq 0 $((NW-1))); do
  NAME="vlm-bench-$w"
  docker rm -f "$NAME" 2>/dev/null || true
  echo "[BACKFILL] Creating container $NAME (GPU 0)..."
  docker run -d --name "$NAME" --gpus '"device=0"' --network host --entrypoint bash \
    -v /home/liuqi/hc/synth:/home/liuqi/hc/synth \
    "$ISAAC_IMAGE" -c "sleep infinity"
  docker exec "$NAME" mkdir -p /usr/share/nvidia 2>/dev/null || true
  docker cp "$NVOPTIX_HOST" "$NAME:/usr/share/nvidia/nvoptix.bin" 2>/dev/null || true
  docker exec "$NAME" bash -c "apt-get update -qq && apt-get install -y -qq ffmpeg > /dev/null 2>&1" &
done
wait
echo "[BACKFILL] containers ready"

for w in $(seq 0 $((NW-1))); do
  NAME="vlm-bench-$w"
  SHARD="$WORKDIR/_shard_${w}.json"
  [ -f "$SHARD" ] || { echo "[BACKFILL] shard $w empty, skip"; continue; }
  IDS=$(python3 -c "import json;print(','.join(t['id'] for t in json.load(open('$SHARD'))['tasks']))")
  echo "[BACKFILL] worker $w via $NAME: $(echo $IDS | tr ',' '\n' | wc -l) tasks"
  tmux kill-session -t "worker_$w" 2>/dev/null || true
  tmux new-session -d -s "worker_$w" bash -c "
    cd $WORKDIR
    N_FRAMES=1 ROOM_BOUNDARY=1 python3 bench_batch.py \
      --tasks $IDS --batch-name $OUT_BATCH --container $NAME --vllm-url $VLLM_URL \
      2>&1 | tee /home/liuqi/hc/backfill_worker_${w}.log
  "
done
echo "[BACKFILL] launched -> results/$OUT_BATCH/"
