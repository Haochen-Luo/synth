import json, sys, os, collections
D = os.path.dirname(os.path.abspath(__file__)); NW = int(sys.argv[1]) if len(sys.argv) > 1 else 6
ts = json.load(open(f"{D}/benchmark_tasks_generated.json"))["tasks"]
scenes = collections.OrderedDict()
for t in ts: scenes.setdefault(t["scene_dir"], []).append(t)
shards = [[] for _ in range(NW)]
for i, (s, tl) in enumerate(scenes.items()): shards[i % NW].extend(tl)
for w in range(NW): json.dump({"tasks": shards[w]}, open(f"{D}/_val_shard_{w}.json", "w"))
print("[split] shard task counts:", [len(s) for s in shards], flush=True)
