#!/bin/bash
# Watcher: wait until batch-1 (remaining-fixed) finishes, then auto-launch
# batch-2 = re-run the OLD (buggy-physics) tasks with the FIXED code into a
# THIRD folder. End state: all 333 tasks have a fixed-physics result, split as:
#   eval_30B_333_remaining_fixed  (the ~109 not done by the original run)
# + eval_30B_333_rerun_fixed      (the ~224 the original run did with the bug)
#   = 333 fixed-physics results total.
# The original eval_30B_333_v2 (buggy) is kept untouched as a baseline.
#
# Run detached on HK:
#   ssh hk 'cd /home/liuqi/hc/synth/benchmark_zehao && \
#     nohup bash watch_then_rerun.sh > /home/liuqi/hc/watch_rerun.log 2>&1 &'

set -e
WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

NW=4
BATCH1="eval_30B_333_remaining_fixed"     # batch-1 (already running)
BATCH2="eval_30B_333_rerun_fixed"         # batch-2 (this script will start it)
ORIG="eval_30B_333_v2"                     # original buggy run (baseline)
TASKS_JSON="$WORKDIR/benchmark_tasks_generated_validated.json"
R="$WORKDIR/results"
POLL=120                                   # seconds between checks

TOTAL=$(python3 -c "import json;print(len(json.load(open('$TASKS_JSON'))['tasks']))")
# How many tasks batch-1 is responsible for = total - (tasks the orig run finished)
ORIG_DONE=$(ls "$R/$ORIG"/*/*/results.json 2>/dev/null | wc -l)
BATCH1_TARGET=$(( TOTAL - ORIG_DONE ))

echo "[WATCH] $(date)"
echo "[WATCH] total=$TOTAL  orig_done=$ORIG_DONE  => batch-1 target=$BATCH1_TARGET"
echo "[WATCH] waiting for $BATCH1 to reach $BATCH1_TARGET completed tasks..."

# ── Phase 1: wait for batch-1 to finish ──
STALL=0; LAST=-1
while true; do
    DONE=$(ls "$R/$BATCH1"/*/*/results.json 2>/dev/null | wc -l)
    WORKERS=$(tmux ls 2>/dev/null | grep -c '^worker_' || true)
    echo "[WATCH] $(date +%H:%M:%S) batch-1 done=$DONE/$BATCH1_TARGET workers=$WORKERS"
    if [ "$DONE" -ge "$BATCH1_TARGET" ]; then
        echo "[WATCH] batch-1 COMPLETE."
        break
    fi
    # Safety: if no workers left AND progress stalled for many polls, stop waiting
    if [ "$WORKERS" -eq 0 ]; then
        if [ "$DONE" -eq "$LAST" ]; then
            STALL=$((STALL+1))
        else
            STALL=0
        fi
        if [ "$STALL" -ge 5 ]; then
            echo "[WATCH] WARNING: no workers and no progress for $((STALL*POLL))s."
            echo "[WATCH] batch-1 at $DONE/$BATCH1_TARGET — proceeding anyway with whatever is done."
            break
        fi
    fi
    LAST=$DONE
    sleep "$POLL"
done

# ── Phase 2: launch batch-2 (re-run the orig-done tasks, fixed code, new folder) ──
# completed-from = BATCH1: tasks already done in batch-1 are skipped, so batch-2
# runs exactly the tasks NOT in batch-1 = the ones the orig buggy run did.
echo "[WATCH] launching batch-2: $BATCH2 (skip-from $BATCH1) with $NW workers"
bash "$WORKDIR/parallel_launch_remaining.sh" "$NW" "$BATCH2" "$BATCH1"
echo "[WATCH] batch-2 launched. $(date)"
echo "[WATCH] monitor: tail -f /home/liuqi/hc/eval_remaining_worker_*.log"
echo "[WATCH] when batch-2 done: $BATCH2 + $BATCH1 together = all $TOTAL tasks fixed-physics."
