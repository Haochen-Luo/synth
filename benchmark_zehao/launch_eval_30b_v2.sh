#!/bin/bash
# 30B eval over the 333-task validated dataset.
# vLLM: Qwen3-VL-30B-A3B-Thinking-FP8 (host tmux, GPU0) @ :8300
# Isaac render container: vlm-jupyter-180 (physical GPU0, has nvoptix+ffmpeg)
# Per-task docker-exec via bench_batch.py -> segfault-isolated + auto-recover.
WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

BATCH="eval_30B_333_v2"
TASKS_FILE="$WORKDIR/full_task_ids_v2_333.txt"
TASKS_JSON="$WORKDIR/benchmark_tasks_generated_validated.json"
CONTAINER="vlm-jupyter"
VLLM_URL="http://localhost:8300/v1/chat/completions"

mapfile -t TASKS < "$TASKS_FILE"
TOTAL=${#TASKS[@]}
echo "[RUN] $BATCH ($TOTAL tasks) | $CONTAINER | $VLLM_URL | $(date)"

FAIL=0; PASS=0
for i in "${!TASKS[@]}"; do
  TID="${TASKS[$i]}"; N=$((i+1))
  echo ""; echo "======== ($N/$TOTAL) $TID — $(date +%H:%M:%S) ========"
  if TASKS_JSON="$TASKS_JSON" python3 bench_batch.py \
      --tasks "$TID" --batch-name "$BATCH" \
      --container "$CONTAINER" --vllm-url "$VLLM_URL"; then
    PASS=$((PASS+1)); echo "[RUN] ✓ $TID"
  else
    FAIL=$((FAIL+1)); echo "[RUN] ✗ $TID"
  fi
done
echo ""; echo "[PIPELINE] $BATCH DONE — Passed $PASS/$TOTAL Failed $FAIL/$TOTAL — $(date)"
