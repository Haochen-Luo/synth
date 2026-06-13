#!/bin/bash
# 1-FRAME experiment launcher for HK node (30B).
# Identical to parallel_launch.sh but:
#   - BATCH = eval_30B_333_1frame (fresh folder)
#   - exports N_FRAMES=1 into every worker  -> VLM sees ONLY the current
#     decision frame (no step-2/step-1 temporal history). PLAN_LEN unchanged (5).
#   - full 333-task set (benchmark_tasks_generated_validated.json)
#
# Prereqs: vLLM on :8300 (GPU 1), isaac-sim:4.5.0 image, /home/liuqi/hc/synth mounted.
# Usage: bash parallel_launch_1frame.sh [N_WORKERS=4]

set -e
WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

export N_FRAMES=1                       # <<< the experiment switch

NW=${1:-4}
BATCH="eval_30B_333_1frame"
TASKS_JSON="$WORKDIR/benchmark_tasks_generated_validated.json"
VLLM_URL="http://localhost:8300/v1/chat/completions"
ISAAC_IMAGE="nvcr.io/nvidia/isaac-sim:4.5.0"
NVOPTIX_HOST="/usr/share/nvidia/nvoptix.bin"

echo "[1FRAME] N_FRAMES=$N_FRAMES | $NW workers | $BATCH | $(date)"

# ── Step 1: Filter out already-completed tasks, then split into NW shards ──
echo "[1FRAME] Filtering completed tasks and splitting into $NW shards..."
python3 -c "
import json, glob, os
master = json.load(open('$TASKS_JSON'))['tasks']
# find completed task IDs from results dirs
done_ids = set()
for rj in glob.glob('$WORKDIR/results/$BATCH/*/*/results.json'):
    d = os.path.basename(os.path.dirname(rj))
    # dir name = taskid_YYYYMMDD_HHMMSS -> strip last 2 parts
    tid = '_'.join(d.split('_')[:-2])
    done_ids.add(tid)
remaining = [t for t in master if t['id'] not in done_ids]
print(f'[split] {len(remaining)} tasks remaining out of {len(master)} total ({len(done_ids)} done, skipped)')
# write filtered tasks json
with open('/tmp/remaining_1frame.json', 'w') as f:
    json.dump({'tasks': remaining}, f)
"
TASKS_JSON="/tmp/remaining_1frame.json" python3 "$WORKDIR/parallel_split.py" "$NW"

# ── Step 2: (Re)create containers ──
for w in $(seq 0 $((NW-1))); do
    NAME="vlm-bench-$w"
    docker rm -f "$NAME" 2>/dev/null || true
    # All Isaac containers on GPU0; GPU1 dedicated to vLLM for best throughput.
    GPU=0
    echo "[1FRAME] Creating container $NAME (GPU $GPU)..."
    docker run -d --name "$NAME" \
        --gpus "\"device=$GPU\"" \
        --network host \
        --entrypoint bash \
        -v /home/liuqi/hc/synth:/home/liuqi/hc/synth \
        "$ISAAC_IMAGE" \
        -c "sleep infinity"
    docker exec "$NAME" mkdir -p /usr/share/nvidia 2>/dev/null || true
    docker cp "$NVOPTIX_HOST" "$NAME:/usr/share/nvidia/nvoptix.bin" 2>/dev/null || true
    docker exec "$NAME" bash -c "apt-get update -qq && apt-get install -y -qq ffmpeg > /dev/null 2>&1" &
done
wait
echo "[1FRAME] All $NW containers ready"

# ── Step 3: Launch workers (N_FRAMES exported into each tmux) ──
for w in $(seq 0 $((NW-1))); do
    NAME="vlm-bench-$w"
    SHARD="$WORKDIR/_shard_${w}.json"
    if [ ! -f "$SHARD" ]; then
        echo "[1FRAME] Shard $w empty, skipping"; continue
    fi
    TASK_COUNT=$(python3 -c "import json; print(len(json.load(open('$SHARD'))['tasks']))")
    echo "[1FRAME] Worker $w: $TASK_COUNT tasks via $NAME"
    TASK_IDS=$(python3 -c "import json; print(','.join(t['id'] for t in json.load(open('$SHARD'))['tasks']))")

    tmux new-session -d -s "worker_$w" bash -c "
        cd $WORKDIR
        N_FRAMES=1 TASKS_JSON=$SHARD python3 bench_batch.py \
            --tasks $TASK_IDS \
            --batch-name $BATCH \
            --container $NAME \
            --vllm-url $VLLM_URL \
            2>&1 | tee /home/liuqi/hc/eval_1frame_worker_${w}.log
    "
    echo "[1FRAME] Worker $w launched in tmux 'worker_$w'"
done

echo ""
echo "[1FRAME] All workers launched. Monitor:"
echo "  tail -f /home/liuqi/hc/eval_1frame_worker_*.log"
echo "  results -> $WORKDIR/results/$BATCH/"
