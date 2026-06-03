"""Quick live-stage check of resolve_target() on case003-L3.
Loads the compiled stage, resolves both phases, prints the resolved prim/center,
and asserts they land on ACTIVE Obj_<id> geometry (not the inactive __spawn_asset_ over).

Run in the vlm-jupyter container:
  docker exec vlm-jupyter /isaac-sim/python.sh \
    /home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/scratch_archive/test_resolve_target.py
"""
import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))  # benchmark_zehao on path

from isaacsim import SimulationApp
sim_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})
import omni.usd
from omni.isaac.core.utils.stage import open_stage, is_stage_loading

from bench_helpers import discover_scene_files, resolve_target, get_prim_world_center

SCENE = os.path.join(os.path.dirname(SCRIPT_DIR),
    "full_scenarios_extracted/native_case003_official_solo_run_full_physics_scene")

PHASES = [
    {"name": "pick_BottleFactory", "target_object": "BottleFactory",
     "target_prim": "/World/Env/BottleFactory_9203792__spawn_asset_6999656_",
     "action": "PICK_UP"},
    {"name": "go_KitchenCabinetFactory", "target_object": "KitchenCabinetFactory",
     "target_prim": "/World/Env/KitchenCabinetFactory_7432776__spawn_asset_3210872_",
     "action": "STOP"},
]

sf = discover_scene_files(SCENE)
assert sf["stage"], f"no stage in {SCENE}"
open_stage(sf["stage"])
while is_stage_loading():
    sim_app.update()
stage = omni.usd.get_context().get_stage()

print("\n==================== resolve_target() check ====================")
ok = True
collected = []
for ph in PHASES:
    over = ph["target_prim"]
    over_center = get_prim_world_center(stage, over)  # expected None (inactive over)
    res = resolve_target(stage, ph)
    collected.append({"phase": ph["name"], "object": ph["target_object"],
                      "over_path": over, "over_center": over_center, "resolved": res})
    print(f"\nphase={ph['name']} object={ph['target_object']}")
    print(f"  task over path     : {over}")
    print(f"  over center (old)  : {over_center}   <- expected None (empty bbox)")
    if res is None:
        print("  RESOLVED           : None  <<< FAIL — resolver could not find geometry")
        ok = False
        continue
    prim = stage.GetPrimAtPath(res["prim_path"])
    active = prim.IsValid() and prim.IsActive()
    is_over = "__spawn_asset_" in res["prim_path"]
    print(f"  RESOLVED prim_path : {res['prim_path']}")
    print(f"  center             : {[round(v,3) for v in res['center']]}")
    print(f"  half_extent_xy     : {round(res['half_extent_xy'],3)} m")
    print(f"  active geometry?   : {active}   over-name? {is_over}")
    if (not active) or is_over or res["half_extent_xy"] <= 0:
        ok = False
        print("  <<< FAIL — not active real geometry / zero extent")

print("\n==================== RESULT:", "PASS" if ok else "FAIL", "====================\n")
import json as _json
with open(os.path.join(SCRIPT_DIR, "_resolve_test_result.json"), "w") as _f:
    _json.dump({"ok": ok, "phases": collected}, _f, indent=2)
sim_app.close()
sys.exit(0 if ok else 1)
