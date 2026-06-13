#!/bin/bash
# Overnight orchestrator (HK): batch1 (10 blackfix-verify, 3 workers, N_FRAMES=3,
# ROOM_BOUNDARY=1) -> wait -> batch2 (1frame missing ~46, 3 workers, N_FRAMES=1)
# -> wait -> summary. Self-contained, nohup-safe. Run from benchmark_zehao/.
cd /home/liuqi/hc/synth/benchmark_zehao
LOG=/home/liuqi/overnight.log
exec >>"$LOG" 2>&1
echo "=================== ORCHESTRATOR START $(date) ==================="

wait_tmux() {  # wait for sessions named $1* to APPEAR then DISAPPEAR
  local pfx="$1"
  # 1) wait up to 5 min for at least one matching session to come up
  local tries=0
  while ! tmux ls 2>/dev/null | grep -q "^${pfx}"; do
    sleep 10; tries=$((tries+1))
    if [ $tries -ge 30 ]; then
      echo "  WARN: no '${pfx}' session appeared after 5min — aborting wait"; return 1
    fi
  done
  echo "  '${pfx}' sessions are up; waiting for them to finish..."
  # 2) now wait until they all exit
  while tmux ls 2>/dev/null | grep -q "^${pfx}"; do sleep 30; done
}

echo "--- BATCH 1: blackfix-verify (10 tasks, N_FRAMES=3, ROOM_BOUNDARY=1) ---"
bash launch_blackfix_verify.sh 3 eval_30B_blackfix_verify
wait_tmux "verify_" || { echo "BATCH 1 launch failed, STOP"; exit 1; }
echo "--- BATCH 1 done $(date) ---"

echo "--- BATCH 2: 1frame backfill (~46 missing, N_FRAMES=1) -> eval_30B_1frame_backfill ---"
bash launch_1frame_backfill.sh 3
wait_tmux "worker_" || { echo "BATCH 2 launch failed, STOP"; exit 1; }
echo "--- BATCH 2 done $(date) ---"

echo "=================== ORCHESTRATOR DONE $(date) ==================="
python3 - <<'PYEOF'
import glob, os, re
def uniq(fold):
    s = set()
    for d in glob.glob(f"results/{fold}/*/*"):
        if os.path.isdir(d) and os.path.exists(d + "/results.json"):
            s.add(re.sub(r"_\d{8}_\d{6}$", "", os.path.basename(d)))
    return len(s)
print("blackfix_verify unique done:", uniq("eval_30B_blackfix_verify"), "/ 10")
print("1frame_backfill unique done:", uniq("eval_30B_1frame_backfill"), "/ ~46")
PYEOF
