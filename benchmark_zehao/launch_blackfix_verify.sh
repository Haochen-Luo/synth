#!/bin/bash
# Re-render the 10 black-frame-flagged tasks with the room-boundary fix +
# render-quality guard, into a SEPARATE batch folder so the post-fix frames can
# be compared against the old flagged runs. N_FRAMES=3 (baseline, NOT 1frame).
#
# The 10 tasks (from blackframe_audit):
#   CAMERA-class (room-boundary fix should stop the walk-into-void blackout):
#     case18_dining_push_lift-L2, case069_official_solo_run-L2/-L4,
#     case076_official_two_runners-L3, case055_official_solo_run-L3,
#     case012_official_two_runners-L2
#   RENDER-class (renderer fault; boundary won't fix, guard flags render_invalid;
#     re-render confirms determinism):
#     case06_scene_gen_v5_test_input_case5-L4, case064_official_run_jump-L4,
#     case075_official_solo_run-L4, case024_official_two_runners-L3
#
# Usage: bash launch_blackfix_verify.sh [N_WORKERS=3] [BATCH=eval_30B_blackfix_verify]
set -e
WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

NW=${1:-3}
BATCH=${2:-eval_30B_blackfix_verify}
export N_FRAMES=3            # baseline temporal context (NOT the 1frame ablation)
export ROOM_BOUNDARY=1       # the fix under test
VLLM_URL="http://localhost:8300/v1/chat/completions"
ISAAC_IMAGE="nvcr.io/nvidia/isaac-sim:4.5.0"
NVOPTIX_HOST="/usr/share/nvidia/nvoptix.bin"

TASKS=(
  case18_dining_push_lift-L2
  case069_official_solo_run-L2
  case069_official_solo_run-L4
  case076_official_two_runners-L3
  case055_official_solo_run-L3
  case012_official_two_runners-L2
  case06_scene_gen_v5_test_input_case5-L4
  case064_official_run_jump-L4
  case075_official_solo_run-L4
  case024_official_two_runners-L3
)

echo "[VERIFY] N_FRAMES=$N_FRAMES ROOM_BOUNDARY=$ROOM_BOUNDARY | $NW workers | $BATCH | ${#TASKS[@]} tasks | $(date)"

# ── Round-robin the task ids into NW shards (comma-separated lists) ──
declare -a SHARD
for i in "${!TASKS[@]}"; do
  w=$(( i % NW ))
  if [ -z "${SHARD[$w]}" ]; then SHARD[$w]="${TASKS[$i]}"; else SHARD[$w]="${SHARD[$w]},${TASKS[$i]}"; fi
done

# ── (Re)create NW Isaac containers on GPU 0 ──
for w in $(seq 0 $((NW-1))); do
  NAME="vlm-bench-$w"
  docker rm -f "$NAME" 2>/dev/null || true
  echo "[VERIFY] Creating container $NAME (GPU 0)..."
  docker run -d --name "$NAME" \
    --gpus '"device=0"' \
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
echo "[VERIFY] All $NW containers ready"

# ── Launch one worker per shard in detached tmux ──
for w in $(seq 0 $((NW-1))); do
  NAME="vlm-bench-$w"
  IDS="${SHARD[$w]}"
  if [ -z "$IDS" ]; then echo "[VERIFY] shard $w empty, skip"; continue; fi
  echo "[VERIFY] worker $w via $NAME: $IDS"
  tmux kill-session -t "verify_$w" 2>/dev/null || true
  tmux new-session -d -s "verify_$w" bash -c "
    cd $WORKDIR
    TASKS_JSON=$WORKDIR/benchmark_tasks_generated_validated.json \
    N_FRAMES=3 ROOM_BOUNDARY=1 python3 bench_batch.py \
      --tasks $IDS \
      --batch-name $BATCH \
      --container $NAME \
      --vllm-url $VLLM_URL \
      2>&1 | tee /home/liuqi/hc/verify_worker_${w}.log
  "
done

echo ""
echo "[VERIFY] launched. monitor:"
echo "  tail -f /home/liuqi/hc/verify_worker_*.log"
echo "  results -> $WORKDIR/results/$BATCH/"
