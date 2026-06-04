#!/bin/bash
# 235B eval over the 333-task validated dataset.
# vLLM: Qwen3-VL-235B-A22B-Thinking-FP8 (host tmux, GPU4-7 TP4) @ :8301
# Isaac render container: vlm-isaac-g3 (physical GPU3; provisioned nvoptix+ffmpeg)
# Runs in PARALLEL with the 30B eval (which renders on GPU0) — separate GPUs,
# separate vLLM ports, so the two batches don't contend.
WORKDIR="/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao"
cd "$WORKDIR"

BATCH="eval_235B_333_v2"
TASKS_FILE="$WORKDIR/full_task_ids_v2_333.txt"
TASKS_JSON="$WORKDIR/benchmark_tasks_generated_validated.json"
CONTAINER="vlm-isaac-g3"
VLLM_URL="http://localhost:8301/v1/chat/completions"

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
