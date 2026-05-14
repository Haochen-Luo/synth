#!/usr/bin/env python3
"""4DSynth-Nav batch evaluator. Orchestrates running all tasks sequentially via Docker.
Usage: python bench_batch.py [--tasks 01-L1,02-L1] [--level L1] [--dry-run]
"""
import json, os, sys, subprocess, argparse, glob, time, datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bench_helpers import aggregate_metrics, print_report

TASKS_JSON = os.path.join(SCRIPT_DIR, "benchmark_tasks.json")
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")
DOCKER_CONTAINER = "vlm-jupyter"
RUNNER_PATH = "/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/bench_runner.py"

def run_task(task_id, dry_run=False):
    """Run a single task inside the Isaac Sim Docker container."""
    cmd = (f'docker exec -e TASK_ID={task_id} {DOCKER_CONTAINER} '
           f'/isaac-sim/python.sh {RUNNER_PATH}')
    print(f"\n{'='*60}")
    print(f"  Running: {task_id}")
    print(f"  Command: {cmd}")
    print(f"{'='*60}")
    
    if dry_run:
        print("  [DRY RUN] Skipped")
        return None
    
    t0 = time.time()
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1800)
    elapsed = time.time() - t0
    
    print(f"  Exit code: {result.returncode} ({elapsed:.0f}s)")
    if result.returncode != 0:
        print(f"  STDERR: {(result.stderr or '')[-500:]}")
    
    # Find the latest result for this task
    pattern = os.path.join(RESULTS_DIR, f"{task_id}_*", "results.json")
    results_files = sorted(glob.glob(pattern))
    if results_files:
        return json.load(open(results_files[-1]))
    return None

def collect_existing_results():
    """Collect all existing results from the results directory."""
    all_results = []
    for rj in sorted(glob.glob(os.path.join(RESULTS_DIR, "*/results.json"))):
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
    parser.add_argument("--ssh", type=str, default="", help="SSH host to run on (e.g. GPU-843)")
    args = parser.parse_args()

    bench = json.load(open(TASKS_JSON))
    all_tasks = bench["tasks"]

    if args.report_only:
        results = collect_existing_results()
        if not results:
            print("No results found."); return
        summary = aggregate_metrics(results)
        print_report(summary, results)
        # Save report
        rpt = os.path.join(RESULTS_DIR, "benchmark_report.json")
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
        
        result = run_task(t["id"], dry_run=args.dry_run)
        if result and "metrics" in result:
            all_metrics.append(result["metrics"])
            m = result["metrics"]
            status = "✅" if m["success"] else ("⏰" if m["timeout"] else "❌")
            print(f"  {status} SR={m['task_success_rate']} SP={m['subtask_progress']:.0%} "
                  f"GD={m['goal_distance_m']:.1f}m Steps={m['steps_used']}")

    if all_metrics:
        summary = aggregate_metrics(all_metrics)
        print_report(summary, all_metrics)
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        rpt = os.path.join(RESULTS_DIR, f"report_{ts}.json")
        json.dump({"summary": summary, "per_task": all_metrics}, open(rpt, "w"), indent=2)
        print(f"\nReport saved: {rpt}")

if __name__ == "__main__":
    main()
