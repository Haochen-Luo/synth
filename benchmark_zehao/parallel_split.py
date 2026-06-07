#!/usr/bin/env python3
"""Split 333 tasks into N shards for parallel execution.
Distributes by scene (so all tasks for a scene go to the same worker)
and excludes already-completed tasks.

Usage: python3 parallel_split.py [N_WORKERS] [--skip-completed]
"""
import json, sys, os, glob, collections, argparse

D = os.path.dirname(os.path.abspath(__file__))

# Back-compat: first positional arg is N_WORKERS, plus --skip-completed flag.
# New: --completed-from <batch> lets us treat a PRIOR batch's results as the
# "already done" set while writing shards for a fresh batch folder.
ap = argparse.ArgumentParser()
ap.add_argument("nw", nargs="?", type=int, default=5)
ap.add_argument("--skip-completed", action="store_true")
ap.add_argument("--completed-from", default="eval_30B_333_v2",
                help="batch dir(s) under results/ whose results.json count as done "
                     "(comma-separated to union multiple)")
args = ap.parse_args()
NW = args.nw
SKIP = args.skip_completed

TASKS_JSON = os.path.join(D, "benchmark_tasks_generated_validated.json")
COMPLETED_DIRS = [os.path.join(D, "results", b.strip())
                  for b in args.completed_from.split(",") if b.strip()]

ts = json.load(open(TASKS_JSON))["tasks"]

# Find completed tasks
completed = set()
if SKIP:
    for rdir in COMPLETED_DIRS:
        for rj in glob.glob(os.path.join(rdir, "*", "*", "results.json")):
            try:
                r = json.load(open(rj))
                completed.add(r["metrics"]["task_id"])
            except:
                pass
    print(f"[split] Skipping {len(completed)} completed tasks "
          f"(from {', '.join(os.path.basename(d) for d in COMPLETED_DIRS)})")

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
