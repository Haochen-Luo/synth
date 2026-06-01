#!/bin/bash
# ══════════════════════════════════════════════════════════════
# ONE-SHOT OVERNIGHT PIPELINE: validate spawns → generate tasks → run batch
# ══════════════════════════════════════════════════════════════
#
# Usage: ssh GPU-843 'nohup bash /path/to/run_full_benchmark_overnight.sh \
#            > /path/to/overnight_pipeline.log 2>&1 &'
#
# NOTE: no set -e — Phase 1 may exit non-zero on OOM but cached scenes
# are still valid. Pipeline continues with whatever was cached.
WORKDIR="/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao"
cd "$WORKDIR"

echo "========================================"
echo "[PIPELINE] Full Benchmark Overnight Run"
echo "[PIPELINE] Started: $(date)"
echo "========================================"

# ── Phase 1: Validate spawns (Isaac Sim + PhysX floodfill) ──
echo ""
echo "[PIPELINE] Phase 1: Validating spawns with floodfill..."
echo "[PIPELINE] This loads each of 122 scenes and runs BFS at 0.25m resolution."
echo "[PIPELINE] Cached results will be reused if they exist."
echo ""

docker exec -w "$WORKDIR" vlm-jupyter \
    /isaac-sim/python.sh validate_full_spawns.py

echo ""
echo "[PIPELINE] Phase 1 complete: $(date)"

# ── Phase 2: Generate validated tasks ──
echo ""
echo "[PIPELINE] Phase 2: Generating validated benchmark tasks..."

# This is plain Python, can run on host
python3 "$WORKDIR/generate_validated_benchmark.py"

echo ""
echo "[PIPELINE] Phase 2 complete: $(date)"

# ── Phase 3: Generate task ID list ──
echo ""
echo "[PIPELINE] Phase 3: Extracting task IDs..."

python3 -c "
import json
tasks = json.load(open('$WORKDIR/benchmark_tasks_full_runner.json'))['tasks']
# Scene-grouped order
from collections import OrderedDict
scenes = OrderedDict()
for t in tasks:
    scenes.setdefault(t['scene_dir'], []).append(t['id'])
all_ids = []
for s, tids in scenes.items():
    all_ids.extend(tids)
with open('$WORKDIR/full_task_ids.txt', 'w') as f:
    for tid in all_ids:
        f.write(tid + '\n')
print(f'Task list: {len(all_ids)} tasks across {len(scenes)} scenes')
"

echo "[PIPELINE] Phase 3 complete: $(date)"

# ── Phase 4: Run benchmark batch ──
echo ""
echo "[PIPELINE] Phase 4: Running 30B benchmark batch..."
BATCH="fullrun_30B_validated_L2L4"
TASKS_FILE="$WORKDIR/full_task_ids.txt"

mapfile -t TASKS < "$TASKS_FILE"
TOTAL=${#TASKS[@]}
echo "========================================"
echo "[RUN] Batch: $BATCH ($TOTAL tasks, scene-grouped)"
echo "[RUN] Model: Qwen3-VL-30B-A3B @ localhost:8000 (30B vLLM)"
echo "[RUN] Tasks JSON: benchmark_tasks_full_runner.json (floodfill-validated)"
echo "[RUN] MAX_STEPS=150, MAX_VLM_CALLS=50"
echo "[RUN] Started: $(date)"
echo "========================================"

FAIL=0; PASS=0
for i in "${!TASKS[@]}"; do
  TID="${TASKS[$i]}"
  N=$((i+1))
  echo ""
  echo "======== ($N/$TOTAL) $TID — $(date +%H:%M:%S) ========"
  if docker exec \
    -e TASK_ID="$TID" \
    -e BATCH_NAME="$BATCH" \
    -e TASKS_JSON="$WORKDIR/benchmark_tasks_full_runner.json" \
    -w "$WORKDIR" \
    vlm-jupyter /isaac-sim/python.sh bench_runner.py; then
    echo "[RUN] ✓ $TID completed"
    PASS=$((PASS+1))
  else
    echo "[RUN] ✗ $TID FAILED (exit $?)"
    FAIL=$((FAIL+1))
  fi
done

echo ""
echo "========================================"
echo "[PIPELINE] ALL DONE — $(date)"
echo "[PIPELINE] Passed: $PASS/$TOTAL  Failed: $FAIL/$TOTAL"
echo "========================================"
