#!/usr/bin/env python3
"""4DSynth-Nav batch evaluator. Orchestrates running all tasks sequentially via Docker.
Usage: python bench_batch.py [--tasks 01-L1,02-L1] [--level L1] [--dry-run]
"""
import json, os, sys, subprocess, argparse, glob, time, datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bench_helpers import aggregate_metrics, print_report

TASKS_JSON = os.environ.get("TASKS_JSON", os.path.join(SCRIPT_DIR, "benchmark_tasks.json"))
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")
DOCKER_CONTAINER = "vlm-jupyter"  #"bench-isaac"
RUNNER_PATH = "/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/bench_runner.py"
VLLM_URL = "http://localhost:8300/v1/chat/completions"
MODEL_NAME = ""

MAX_STEPS = 150         # per-episode step cap, passed to the runner
TASK_TIMEOUT = 5400     # per-task wall-clock cap (s) — full episodes + Isaac boot
NVOPTIX_HOST = "/usr/share/nvidia/nvoptix.bin"  # host copy for the OptiX denoiser

# Signatures that mean Isaac Sim crashed (vs the task merely failing).
CRASH_SIGNATURES = ("Segmentation fault", "core dumped",
                    "is not running", "OCI runtime exec failed",
                    "Fatal Python error")

def recover_container():
    """Restart the Isaac Sim container and restore nvoptix.bin. Called when a
    task fails with an Isaac-crash signature so an overnight batch survives the
    known Isaac-degradation segfault."""
    print("  [RECOVERY] restarting container + restoring nvoptix.bin ...")
    subprocess.run(f"docker restart {DOCKER_CONTAINER}", shell=True,
                   capture_output=True, text=True, timeout=180)
    time.sleep(12)
    subprocess.run(f"docker cp {NVOPTIX_HOST} "
                   f"{DOCKER_CONTAINER}:/usr/share/nvidia/nvoptix.bin",
                   shell=True, capture_output=True, text=True, timeout=120)
    print("  [RECOVERY] container back up")

def _run_once(task_id, batch_name, dry_run):
    """One docker-exec attempt. Returns (returncode, stdout, stderr, elapsed)."""
    batch_env = f"-e BATCH_NAME={batch_name} " if batch_name else ""
    tasks_json_env = f"-e TASKS_JSON={TASKS_JSON} "
    model_env = f"-e MODEL_NAME={MODEL_NAME} " if MODEL_NAME else ""
    cmd = (f'docker exec -e TASK_ID={task_id} -e VLLM_URL={VLLM_URL} '
           f'-e MAX_STEPS={MAX_STEPS} {batch_env}{tasks_json_env}{model_env}'
           f'{DOCKER_CONTAINER} /isaac-sim/python.sh {RUNNER_PATH}')
    print(f"  Command: {cmd}")
    if dry_run:
        print("  [DRY RUN] Skipped")
        return 0, "", "", 0.0
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=TASK_TIMEOUT)
        return r.returncode, r.stdout or "", r.stderr or "", time.time() - t0
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT", time.time() - t0

def run_task(task_id, batch_name="", dry_run=False):
    """Run a single task; on an Isaac-crash signature, recover the container
    and retry once. Returns the parsed results.json dict or None."""
    print(f"\n{'='*60}")
    print(f"  Running: {task_id}")
    print(f"{'='*60}")

    rc, out, err, elapsed = _run_once(task_id, batch_name, dry_run)
    if dry_run:
        return None

    crashed = rc != 0 and any(s in (out + err) for s in CRASH_SIGNATURES)
    if crashed:
        print(f"  Isaac crash detected (rc={rc}) after {elapsed:.0f}s")
        recover_container()
        print(f"  [RETRY] re-running {task_id} ...")
        rc, out, err, elapsed = _run_once(task_id, batch_name, dry_run)

    print(f"  Exit code: {rc} ({elapsed:.0f}s)")
    if rc != 0:
        print(f"  STDERR: {err[-500:]}")

    # Find the latest result for this task
    pattern = os.path.join(RESULTS_DIR, "*", "*", f"{task_id}_*", "results.json") if batch_name else os.path.join(RESULTS_DIR, "*", f"{task_id}_*", "results.json")
    results_files = sorted(glob.glob(pattern))
    if results_files:
        return json.load(open(results_files[-1]))
    return None

def collect_existing_results(batch_name=""):
    """Collect all existing results from the results directory."""
    all_results = []
    # If batch_name is provided, we only collect from that batch. Otherwise we try both old and new patterns.
    if batch_name:
        patterns = [os.path.join(RESULTS_DIR, batch_name, "*", "*", "results.json")]
    else:
        patterns = [os.path.join(RESULTS_DIR, "*", "*", "results.json"), os.path.join(RESULTS_DIR, "*", "*", "*", "results.json")]
    
    files = []
    for p in patterns: files.extend(glob.glob(p))
    for rj in sorted(list(set(files))):
        try:
            data = json.load(open(rj))
            all_results.append(data["metrics"])
        except: pass
    return all_results

def main():
    parser = argparse.ArgumentParser(description="4DSynth-Nav Batch Evaluator")
    parser.add_argument("--tasks", type=str, default="", help="Comma-separated task IDs (e.g. 01-L1,02-L1)")
    parser.add_argument("--level", type=str, default="", help="Run all tasks of a level (L1/L2/L3/L4)")
    parser.add_argument("--scene", type=str, default="", help="Run all tasks for a scene prefix (e.g. case01)")
    parser.add_argument("--all", action="store_true", help="Run all 40 tasks")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    parser.add_argument("--report-only", action="store_true", help="Only aggregate and print existing results")
    parser.add_argument("--batch-name", type=str, default="", help="Optional unified outer directory name for this batch run")
    parser.add_argument("--container", type=str, default="vlm-jupyter", help="Docker container name to use")
    parser.add_argument("--vllm-url", type=str, default="http://localhost:8300/v1/chat/completions", help="URL of the VLM API endpoint")
    parser.add_argument("--model-name", type=str, default="", help="Explicitly specify the model name (required for external APIs)")
    parser.add_argument("--max-steps", type=int, default=150, help="Per-episode step cap")
    args = parser.parse_args()

    # Update globals based on args
    global DOCKER_CONTAINER, VLLM_URL, MAX_STEPS, MODEL_NAME
    DOCKER_CONTAINER = args.container
    VLLM_URL = args.vllm_url
    MODEL_NAME = args.model_name
    MAX_STEPS = args.max_steps

    bench = json.load(open(TASKS_JSON))
    all_tasks = bench["tasks"]

    if args.report_only:
        results = collect_existing_results(args.batch_name)
        if not results:
            print("No results found."); return
        summary = aggregate_metrics(results)
        print_report(summary, results)
        # Save report
        out_dir = os.path.join(RESULTS_DIR, args.batch_name) if args.batch_name else RESULTS_DIR
        os.makedirs(out_dir, exist_ok=True)
        rpt = os.path.join(out_dir, "benchmark_report.json")
        json.dump({"summary": summary, "per_task": results}, open(rpt, "w"), indent=2)
        print(f"\nReport saved: {rpt}")
        return

    # Filter tasks
    if args.tasks:
        ids = set(args.tasks.split(","))
        tasks = [t for t in all_tasks if t["id"] in ids]
    elif args.level:
        tasks = [t for t in all_tasks if t["level"] == args.level]
    elif args.scene:
        tasks = [t for t in all_tasks if args.scene in t["scene_dir"]]
    elif args.all:
        tasks = all_tasks
    else:
        print("Specify --tasks, --level, --scene, --all, or --report-only")
        parser.print_help(); return

    print(f"\n4DSynth-Nav Benchmark: {len(tasks)} tasks to run")
    print(f"Results dir: {RESULTS_DIR}\n")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    all_metrics = []
    
    for i, t in enumerate(tasks):
        print(f"\n[{i+1}/{len(tasks)}] Task {t['id']} ({t['level']}): {t['instruction']}")
        
        result = run_task(t["id"], batch_name=args.batch_name, dry_run=args.dry_run)
        if result and "metrics" in result:
            all_metrics.append(result["metrics"])
            m = result["metrics"]
            status = "✅" if m["success"] else ("⏰" if m["timeout"] else "❌")
            print(f"  {status} SR={m['task_success_rate']} SP={m['subtask_progress']:.0%} "
                  f"GD={m['goal_distance_m']:.1f}m Steps={m['steps_used']} "
                  f"Coll={m.get('collision_count', 0)} "
                  f"Pushed={m.get('agent_pushed_events', 0)}ev/"
                  f"{m.get('agent_pushed_frames', 0)}fr")

    if all_metrics:
        summary = aggregate_metrics(all_metrics)
        print_report(summary, all_metrics)
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        bname = args.batch_name if args.batch_name else f"report_{ts}"
        out_dir = os.path.join(RESULTS_DIR, args.batch_name) if args.batch_name else RESULTS_DIR
        os.makedirs(out_dir, exist_ok=True)
        rpt = os.path.join(out_dir, f"report_{ts}.json")
        json.dump({"summary": summary, "per_task": all_metrics}, open(rpt, "w"), indent=2)
        print(f"\nReport saved: {rpt}")

if __name__ == "__main__":
    main()
