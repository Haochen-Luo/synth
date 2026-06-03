"""validate_generated_spawns.py — fill agent_start/agent_yaw for generate_tasks.py output.

Compact spawn finder (Isaac): for each generated task, samples a ring around the
phase-1 target and keeps the first spawn that is (a) inside the room bounds, (b)
collision-clear (PhysX sweep), (c) has forward clearance, and (d) has line-of-sight to
the REAL target (raycast). face tasks face the target; back tasks face 180 deg away.
Tasks with no valid spawn are dropped (an enclosed/unreachable target fails LOS here —
the LOS gate your benchmark always had, now armed against the real prim).

Reuses the same PhysX primitives as validate_all_spawns.py. Reads room bounds from
scene_facts.json (object-center extent, padded inward).

Usage (container): docker exec vlm-jupyter-180 /isaac-sim/python.sh validate_generated_spawns.py
  IN  = benchmark_tasks_generated.json
  OUT = benchmark_tasks_generated_spawned.json (+ dropped_spawns.json)
"""
import sys, os, json, math, glob
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from isaacsim import SimulationApp
sim_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})
import omni.usd, omni.timeline, omni.physx, carb
from omni.isaac.core.utils.stage import is_stage_loading
from bench_helpers import discover_scene_files

BASE = os.path.join(SCRIPT_DIR, "full_scenarios_extracted")
IN = os.path.join(SCRIPT_DIR, "benchmark_tasks_generated.json")
OUT = os.path.join(SCRIPT_DIR, "benchmark_tasks_generated_spawned.json")
DROPPED = os.path.join(SCRIPT_DIR, "dropped_spawns.json")

SWEEP_RADIUS, SWEEP_DIST = 0.40, 0.05
CAM_H = 1.58
WALKABLE = ("floor", "ground", "rug", "blanket", "towel", "mat")
_8DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (0.7, 0.7), (-0.7, 0.7), (0.7, -0.7), (-0.7, -0.7)]
RADII = [3.0, 2.5, 3.5, 2.2, 4.0]
ANGLES = list(range(0, 360, 20))


def clear(q, cx, cy):
    for sz in (0.5, 1.0):
        for dx, dy in _8DIRS:
            h = q.sweep_sphere_closest(SWEEP_RADIUS, carb.Float3(cx, cy, sz),
                                       carb.Float3(dx, dy, 0), SWEEP_DIST)
            if h["hit"]:
                wp = (h.get("rigidBody") or h.get("collider") or "").lower()
                if any(w in wp for w in WALKABLE):
                    continue
                if float(h.get("distance", 0)) < 0.01:
                    return False
    return True


def fwd_clear(q, sx, sy, yaw, min_dist=1.2):
    r = math.radians(yaw)
    h = q.raycast_closest(carb.Float3(sx, sy, 1.0), carb.Float3(math.cos(r), math.sin(r), 0), min_dist)
    if h["hit"]:
        nm = (h["rigidBody"] or h["collider"]).split("/")[-1].lower()
        return any(w in nm for w in WALKABLE)
    return True


def los(q, sx, sy, tgt, half):
    d = (tgt[0] - sx, tgt[1] - sy, tgt[2] - CAM_H)
    dist = math.sqrt(sum(c * c for c in d))
    if dist < 1e-3:
        return True
    dirv = (d[0] / dist, d[1] / dist, d[2] / dist)
    h = q.raycast_closest(carb.Float3(sx, sy, CAM_H), carb.Float3(*dirv), dist + 0.5)
    if not h["hit"]:
        return True
    return float(h.get("distance", 0)) >= dist - half - 0.25


def room_bounds(scene_dir_name):
    ff = os.path.join(BASE, scene_dir_name, "scene_facts.json")
    if not os.path.exists(ff):
        return None
    objs = json.load(open(ff))["objects"]
    xs = [o["center"][0] for o in objs]; ys = [o["center"][1] for o in objs]
    return (min(xs) + 1.0, max(xs) - 1.0, min(ys) + 1.0, max(ys) - 1.0)


def find_spawn(q, task, bounds):
    p0 = task["phases"][0]
    tx, ty, tz = p0["target_center"]
    half = p0.get("radius", 0.5)
    back = task.get("spawn_facing") == "back"
    xmn, xmx, ymn, ymx = bounds
    for r in RADII:
        for a in ANGLES:
            ra = math.radians(a)
            sx, sy = tx + r * math.cos(ra), ty + r * math.sin(ra)
            if not (xmn <= sx <= xmx and ymn <= sy <= ymx):
                continue
            if not clear(q, sx, sy):
                continue
            face_yaw = math.degrees(math.atan2(ty - sy, tx - sx))
            yaw = (face_yaw + 180) if back else face_yaw
            yaw = ((yaw + 180) % 360) - 180
            if not fwd_clear(q, sx, sy, yaw):
                continue
            # LOS to the (real) target is required regardless of facing — an
            # enclosed/occluded target fails here and the task is dropped.
            if not los(q, sx, sy, (tx, ty, tz), half):
                continue
            return round(sx, 3), round(sy, 3), round(yaw, 2)
    return None


def main():
    tasks = json.load(open(IN))["tasks"]
    by_scene = {}
    for t in tasks:
        by_scene.setdefault(t["scene_dir"], []).append(t)

    out, dropped = [], []
    for scene_dir, ts in sorted(by_scene.items()):
        scene_dir_name = scene_dir.split("/")[-1]
        sd = os.path.join(SCRIPT_DIR, scene_dir)
        sf = discover_scene_files(sd)
        if not sf["stage"]:
            for t in ts:
                dropped.append({"id": t["id"], "reason": "no_stage"})
            continue
        omni.usd.get_context().open_stage(sf["stage"])
        while is_stage_loading():
            sim_app.update()
        tl = omni.timeline.get_timeline_interface(); tl.play()
        for _ in range(20):
            sim_app.update()
        tl.pause()
        q = omni.physx.get_physx_scene_query_interface()
        bounds = room_bounds(scene_dir_name)
        if not bounds:
            for t in ts:
                dropped.append({"id": t["id"], "reason": "no_scene_facts"})
            continue
        for t in ts:
            spawn = find_spawn(q, t, bounds)
            if spawn is None:
                dropped.append({"id": t["id"], "reason": "no_valid_spawn (LOS/clearance)"})
                print(f"[SPAWN] DROP {t['id']}: no valid spawn", flush=True)
                continue
            t["agent_start"] = [spawn[0], spawn[1]]
            t["agent_yaw"] = spawn[2]
            out.append(t)
            print(f"[SPAWN] OK {t['id']}: start=({spawn[0]},{spawn[1]}) yaw={spawn[2]} facing={t.get('spawn_facing')}", flush=True)

    json.dump({"tasks": out}, open(OUT, "w"), indent=2)
    json.dump({"dropped": dropped}, open(DROPPED, "w"), indent=2)
    print(f"[SPAWN] DONE kept={len(out)} dropped={len(dropped)} -> {OUT}", flush=True)
    sim_app.close()


if __name__ == "__main__":
    main()
