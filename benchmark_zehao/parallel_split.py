#!/usr/bin/env python3
"""Split 333 tasks into N shards for parallel execution.
Distributes by scene (so all tasks for a scene go to the same worker)
and excludes already-completed tasks.

Usage: python3 parallel_split.py [N_WORKERS] [--skip-completed]
"""
import json, sys, os, glob, collections

D = os.path.dirname(os.path.abspath(__file__))
NW = int(sys.argv[1]) if len(sys.argv) > 1 else 5
SKIP = "--skip-completed" in sys.argv

TASKS_JSON = os.path.join(D, "benchmark_tasks_generated_validated.json")
RESULTS_DIR = os.path.join(D, "results", "eval_30B_333_v2")

ts = json.load(open(TASKS_JSON))["tasks"]

# Find completed tasks
completed = set()
if SKIP:
    for rj in glob.glob(os.path.join(RESULTS_DIR, "*", "*", "results.json")):
        try:
            r = json.load(open(rj))
            completed.add(r["metrics"]["task_id"])
        except:
            pass
    print(f"[split] Skipping {len(completed)} completed tasks")

# Filter out completed
remaining = [t for t in ts if t["id"] not in completed]
print(f"[split] {len(remaining)} tasks remaining out of {len(ts)} total")

# Group by scene, then round-robin distribute
scenes = collections.OrderedDict()
for t in remaining:
    scenes.setdefault(t["scene_dir"], []).append(t)

shards = [[] for _ in range(NW)]
for i, (s, tl) in enumerate(scenes.items()):
    shards[i % NW].extend(tl)

for w in range(NW):
    out = os.path.join(D, f"_shard_{w}.json")
    json.dump({"tasks": shards[w]}, open(out, "w"), indent=2)

counts = [len(s) for s in shards]
print(f"[split] {NW} shards: {counts} (total={sum(counts)})")
