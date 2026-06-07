#!/usr/bin/env bash
# Repro a single bad-case task with SWEEP_DEBUG, isolated into its own batch dir
# so it never touches the main eval stats. Runs the REAL bench_runner (full Isaac
# init, identical to production eval) inside the vlm-jupyter container.
#
# Usage:  bash repro_badcase.sh [TASK_ID]
# Default TASK_ID = case12_text_bedroom_04_minimal_study-L4
#
# Output: results/_repro_sweep_debug/<LEVEL>/<TASK>_<ts>/  (run.log has [SWEEP] lines)
set -euo pipefail

TASK_ID="${1:-case12_text_bedroom_04_minimal_study-L4}"
BATCH="_repro_sweep_debug"
REPO=/home/liuqi/hc/synth/benchmark_zehao
CONTAINER=vlm-jupyter            # NOT the vlm-bench-* eval workers
VLLM=http://localhost:8300/v1/chat/completions
TASKS_JSON=$REPO/benchmark_tasks_generated_validated.json

echo "[repro] task=$TASK_ID batch=$BATCH container=$CONTAINER"
docker exec \
  -e TASK_ID="$TASK_ID" \
  -e BATCH_NAME="$BATCH" \
  -e TASKS_JSON="$TASKS_JSON" \
  -e VLLM_URL="$VLLM" \
  -e SWEEP_DEBUG=1 \
  "$CONTAINER" /isaac-sim/python.sh "$REPO/bench_runner.py"

echo "[repro] DONE. Run dir:"
ls -dt "$REPO/results/$BATCH/"*/*/ 2>/dev/null | head -1
