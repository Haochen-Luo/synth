import json, sys, os, glob, collections
D = os.path.dirname(os.path.abspath(__file__)); NW = int(sys.argv[1]) if len(sys.argv) > 1 else 6
failed = set(open(f"{D}/_failed_scenes.txt").read().split())
baseline = json.load(open(f"{D}/_baseline_235.json"))["tasks"]
keep = [t for t in baseline if t["scene_dir"].split("/")[-1] not in failed]   # 70 good scenes
new = []
for w in range(NW):
    try: new += json.load(open(f"{D}/_val_shard_{w}_valid.json"))["tasks"]
    except Exception as e: print("[merge] miss", w, e)
final = keep + new
json.dump({"tasks": final}, open(f"{D}/benchmark_tasks_generated_validated.json", "w"), indent=2)
lv = lambda t: dict(collections.Counter(x["level"] for x in t))
pick = collections.defaultdict(int)
for t in final:
    if t["level"] in ("L3", "L4"): pick[t["scene_dir"].split("/")[-1]] += 1
allsc = [os.path.basename(os.path.dirname(f)) for f in glob.glob(f"{D}/full_scenarios_extracted/*/scene_facts.json")]
zero = [s for s in allsc if pick.get(s, 0) == 0]
recovered = sum(1 for s in failed if pick.get(s, 0) > 0)
print("[INCR-STATS] failed_reprocessed=%d recovered_with_pickup=%d still_zero=%d" %
      (len(failed), recovered, len(failed) - recovered))
print("[INCR-STATS] kept_baseline=%d new_from_failed=%d TOTAL_VALID=%d %s total_zero_pickup_scenes=%d" %
      (len(keep), len(new), len(final), lv(final), len(zero)))
