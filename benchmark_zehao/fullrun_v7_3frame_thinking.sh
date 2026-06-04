#!/bin/bash
# Full benchmark run: 3-frame VLM context + Qwen3-Thinking model
# All L1-L4 tasks, overnight execution
set -e

BATCH="fullrun_v7_3frame_thinking"
WORKDIR="$(cd "$(dirname "$0")" && pwd)"

TASKS=(
  case01-L1 case01-L2 case01-L3 case01-L4
  case02-L1 case02-L2 case02-L3 case02-L4
  case03-L1 case03-L2 case03-L3 case03-L4
  case04-L1 case04-L2 case04-L3 case04-L4
  case05-L1 case05-L2 case05-L3 case05-L4
  case06-L1 case06-L2 case06-L3 case06-L4
  case07-L1 case07-L2 case07-L3 case07-L4
  case09-L1 case09-L2 case09-L3 case09-L4
  case10-L1 case10-L2 case10-L3 case10-L4
  case12-L1 case12-L2
)

TOTAL=${#TASKS[@]}
echo "========================================================"
echo "[RUN] Full benchmark: $BATCH"
echo "[RUN] Tasks: $TOTAL | Model: Qwen3-VL-30B-A3B-Thinking"
echo "[RUN] Features: 3-frame context, think-strip, VFOV-fixed spawns"
echo "[RUN] Started: $(date)"
echo "========================================================"

FAIL_COUNT=0
PASS_COUNT=0
for i in "${!TASKS[@]}"; do
  TID="${TASKS[$i]}"
  N=$((i+1))
  echo ""
  echo "========================================"
  echo "[RUN] ($N/$TOTAL) $TID — $(date +%H:%M:%S)"
  echo "========================================"

  if docker exec \
    -e TASK_ID="$TID" \
    -e BATCH_NAME="$BATCH" \
    -w "$WORKDIR" \
    vlm-jupyter /isaac-sim/python.sh bench_runner.py; then
    echo "[RUN] ✓ $TID completed"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "[RUN] ✗ $TID FAILED (exit code $?)"
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
done

echo ""
echo "========================================================"
echo "[RUN] DONE — $(date)"
echo "[RUN] Passed: $PASS_COUNT/$TOTAL  Failed: $FAIL_COUNT/$TOTAL"
echo "========================================================"
