import json, sys, os, glob, collections
D = os.path.dirname(os.path.abspath(__file__)); NW = int(sys.argv[1]) if len(sys.argv) > 1 else 6
allt = []
for w in range(NW):
    try: allt += json.load(open(f"{D}/_val_shard_{w}_valid.json"))["tasks"]
    except Exception as e: print("[merge] miss", w, e)
json.dump({"tasks": allt}, open(f"{D}/benchmark_tasks_generated_validated.json", "w"), indent=2)
facts = glob.glob(f"{D}/full_scenarios_extracted/*/scene_facts.json")
gen = json.load(open(f"{D}/benchmark_tasks_generated.json"))["tasks"]
dr = json.load(open(f"{D}/dropped_tasks.json"))["dropped"]
lv = lambda t: dict(collections.Counter(x["level"] for x in t))
vids = {t["id"] for t in allt}
sc = collections.defaultdict(lambda: {"v": 0, "pv": 0})
for t in gen:
    s = t["scene_dir"].split("/")[-1]
    if t["id"] in vids:
        sc[s]["v"] += 1
        if t["level"] in ("L3", "L4"): sc[s]["pv"] += 1
alls = {t["scene_dir"].split("/")[-1] for t in gen}
zero = [s for s in alls if sc[s]["pv"] == 0]
print("[STATS] scenes_probed=%d tasks_gen=%d %s tasks_VALID=%d %s gen_dropped=%d" %
      (len(facts), len(gen), lv(gen), len(allt), lv(allt), len(dr)), flush=True)
print("[STATS] scenes_with_valid_task=%d/%d  scenes_ZERO_valid_pickup=%d" %
      (sum(1 for v in sc.values() if v["v"] > 0), len(alls), len(zero)), flush=True)
print("[STATS] zero-pickup examples:", zero[:15], flush=True)
