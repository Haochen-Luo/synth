#!/bin/bash
# Parallel 30B benchmark launcher for HK node.
# Starts N Isaac Sim containers on GPU 0, splits tasks, runs in parallel.
#
# Prerequisites:
#   - vLLM server running on GPU 1 (:8300)
#   - isaac-sim:4.5.0 image available
#   - /home/liuqi/hc/synth mounted
#
# Usage: bash parallel_launch.sh [N_WORKERS=5]

set -e
WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

NW=${1:-5}
BATCH="eval_30B_333_v2"
TASKS_JSON="$WORKDIR/benchmark_tasks_generated_validated.json"
VLLM_URL="http://localhost:8300/v1/chat/completions"
ISAAC_IMAGE="nvcr.io/nvidia/isaac-sim:4.5.0"
NVOPTIX_HOST="/usr/share/nvidia/nvoptix.bin"

echo "[PARALLEL] $NW workers | $BATCH | $(date)"

# ── Step 1: Split tasks (skip already-completed) ──
echo "[PARALLEL] Splitting tasks into $NW shards..."
python3 "$WORKDIR/parallel_split.py" "$NW" --skip-completed

# ── Step 2: Create containers ──
for w in $(seq 0 $((NW-1))); do
    NAME="vlm-bench-$w"
    # Remove old container if exists
    docker rm -f "$NAME" 2>/dev/null || true
    echo "[PARALLEL] Creating container $NAME (GPU 0)..."
    docker run -d --name "$NAME" \
        --gpus '"device=0"' \
        --network host \
        --entrypoint bash \
        -v /home/liuqi/hc/synth:/home/liuqi/hc/synth \
        "$ISAAC_IMAGE" \
        -c "sleep infinity"
    # Install nvoptix + ffmpeg
    docker cp "$NVOPTIX_HOST" "$NAME:/usr/share/nvidia/nvoptix.bin" 2>/dev/null || true
    docker exec "$NAME" bash -c "apt-get update -qq && apt-get install -y -qq ffmpeg > /dev/null 2>&1" &
done
wait  # wait for all ffmpeg installs
echo "[PARALLEL] All $NW containers ready"

# ── Step 3: Launch workers ──
for w in $(seq 0 $((NW-1))); do
    NAME="vlm-bench-$w"
    SHARD="$WORKDIR/_shard_${w}.json"
    RUNNER="$WORKDIR/bench_runner.py"
    
    if [ ! -f "$SHARD" ]; then
        echo "[PARALLEL] Shard $w is empty, skipping"
        continue
    fi
    
    TASK_COUNT=$(python3 -c "import json; print(len(json.load(open('$SHARD'))['tasks']))")
    echo "[PARALLEL] Worker $w: $TASK_COUNT tasks via $NAME"
    
    # Read task IDs from shard
    TASK_IDS=$(python3 -c "
import json
tasks = json.load(open('$SHARD'))['tasks']
print(','.join(t['id'] for t in tasks))
")
    
    # Launch in background tmux
    tmux new-session -d -s "worker_$w" bash -c "
        cd $WORKDIR
        TASKS_JSON=$SHARD python3 bench_batch.py \
            --tasks $TASK_IDS \
            --batch-name $BATCH \
            --container $NAME \
            --vllm-url $VLLM_URL \
            2>&1 | tee /home/liuqi/hc/eval_worker_${w}.log
    "
    echo "[PARALLEL] Worker $w launched in tmux 'worker_$w'"
done

echo ""
echo "[PARALLEL] All workers launched! Monitor with:"
echo "  tail -f /home/liuqi/hc/eval_worker_*.log"
echo "  nvidia-smi  (check GPU memory)"
echo ""
echo "[PARALLEL] Expected completion: ~$((333 / NW * 7 / 60)) hours"
