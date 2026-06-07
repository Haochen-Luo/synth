#!/bin/bash
# Resume the 30B benchmark: run ONLY the tasks not yet completed in
# eval_30B_333_v2, with the (now fixed) bench_runner, into a SEPARATE batch
# folder so the new (post-WALKABLE-fix) results don't mix with the old ones.
#
# - Skips tasks already done in eval_30B_333_v2 (the prior run).
# - Writes results to results/$BATCH (default eval_30B_333_remaining_fixed).
# - Uses 4 workers (5x showed no 5x speedup; GPU/VLM contention).
#
# Usage: bash parallel_launch_remaining.sh [N_WORKERS=4] [BATCH_NAME]

set -e
WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

NW=${1:-4}
BATCH=${2:-eval_30B_333_remaining_fixed}
COMPLETED_FROM="eval_30B_333_v2"
TASKS_JSON="$WORKDIR/benchmark_tasks_generated_validated.json"
VLLM_URL="http://localhost:8300/v1/chat/completions"
ISAAC_IMAGE="nvcr.io/nvidia/isaac-sim:4.5.0"
NVOPTIX_HOST="/usr/share/nvidia/nvoptix.bin"

echo "[RESUME] $NW workers | batch=$BATCH | skip-from=$COMPLETED_FROM | $(date)"

# ── Step 1: Split ONLY the remaining tasks (done = eval_30B_333_v2) ──
echo "[RESUME] Splitting remaining tasks into $NW shards..."
python3 "$WORKDIR/parallel_split.py" "$NW" --skip-completed --completed-from "$COMPLETED_FROM"

# ── Step 2: (Re)create $NW containers ──
for w in $(seq 0 $((NW-1))); do
    NAME="vlm-bench-$w"
    docker rm -f "$NAME" 2>/dev/null || true
    echo "[RESUME] Creating container $NAME (GPU 0)..."
    docker run -d --name "$NAME" \
        --gpus '"device=0"' \
        --network host \
        --entrypoint bash \
        -v /home/liuqi/hc/synth:/home/liuqi/hc/synth \
        "$ISAAC_IMAGE" \
        -c "sleep infinity"
    docker cp "$NVOPTIX_HOST" "$NAME:/usr/share/nvidia/nvoptix.bin" 2>/dev/null || true
    docker exec "$NAME" bash -c "apt-get update -qq && apt-get install -y -qq ffmpeg > /dev/null 2>&1" &
done
wait
echo "[RESUME] All $NW containers ready"

# ── Step 3: Launch workers ──
for w in $(seq 0 $((NW-1))); do
    NAME="vlm-bench-$w"
    SHARD="$WORKDIR/_shard_${w}.json"
    [ ! -f "$SHARD" ] && { echo "[RESUME] Shard $w empty, skip"; continue; }
    TASK_COUNT=$(python3 -c "import json; print(len(json.load(open('$SHARD'))['tasks']))")
    [ "$TASK_COUNT" = "0" ] && { echo "[RESUME] Shard $w has 0 tasks, skip"; continue; }
    echo "[RESUME] Worker $w: $TASK_COUNT tasks via $NAME"
    TASK_IDS=$(python3 -c "import json; print(','.join(t['id'] for t in json.load(open('$SHARD'))['tasks']))")
    tmux new-session -d -s "worker_$w" bash -c "
        cd $WORKDIR
        TASKS_JSON=$SHARD python3 bench_batch.py \
            --tasks $TASK_IDS \
            --batch-name $BATCH \
            --container $NAME \
            --vllm-url $VLLM_URL \
            2>&1 | tee /home/liuqi/hc/eval_remaining_worker_${w}.log
    "
    echo "[RESUME] Worker $w launched in tmux 'worker_$w'"
done

echo ""
echo "[RESUME] Launched. Monitor: tail -f /home/liuqi/hc/eval_remaining_worker_*.log"
