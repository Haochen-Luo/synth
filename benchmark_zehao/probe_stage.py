"""probe_stage.py — Real-geometry probe of compiled stages -> scene_facts.json.

THE single Isaac-dependent step of the corrected generation pipeline (see README
"Session 2026-06-02"). For each scene it loads the *compiled stage* (the ground truth
the runner actually renders & measures), traverses ACTIVE geometry under the object
containers, and records, per object: the real Obj_<id> prim path, world bbox/center,
z_bottom, on_floor flag, factory + semantic class. It also derives support relations
(which object rests on which) by bbox-stacking, since the upstream
physics_support_relations sidecar is missing from these packages.

Everything downstream (generate_tasks.py) is then pure Python over scene_facts.json,
so the manifest-style `__spawn_asset_` paths (which are inactive overs here) never
enter task specs again.

Usage (inside the GPU-180 container):
  # all scenes:
  docker exec vlm-jupyter-180 /isaac-sim/python.sh \
    /home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/probe_stage.py
  # subset (comma-separated scene dir names):
  docker exec -e SCENES=native_case003_official_solo_run_full_physics_scene \
    vlm-jupyter-180 /isaac-sim/python.sh .../probe_stage.py

Output: <scene_dir>/scene_facts.json
"""
import sys, os, json, glob, re, traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bench_helpers import discover_scene_files, _world_bbox
from semantic_classes import semantic_class_of

BASE = os.path.join(SCRIPT_DIR, "full_scenarios_extracted")
OBJECT_CONTAINERS = ("/World/Env", "/World/InteractiveProps")
ON_FLOOR_THRESH = 0.20      # object bottom within this of floor surface -> on_floor
SUPPORT_Z_TOL   = 0.12      # object bottom within this of a support's top -> resting on it

SCHEMA = "v2_probe_stage_real_geometry_reachability"

# ── Floodfill reachability (two-height sweep, aligned with validate_and_fix_spawns) ──
GRID_RES = 0.25
MAX_CELLS = 6000
AGENT_RADIUS = 0.40
_4DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1)]
WALKABLE = ("floor", "ground", "rug", "blanket", "towel", "mat")
REACH_TOL_M = 1.5   # object is "reachable" if a walkable cell lies within this of its center


def _flood_fill(query_if, seedx, seedy):
    import carb
    start = (round(seedx / GRID_RES), round(seedy / GRID_RES))
    visited = {start}; queue = [start]; idx = 0
    while idx < len(queue) and len(visited) < MAX_CELLS:
        gx, gy = queue[idx]; idx += 1
        wx, wy = gx * GRID_RES, gy * GRID_RES
        for ddx, ddy in _4DIRS:
            ngx, ngy = gx + ddx, gy + ddy
            if (ngx, ngy) in visited:
                continue
            blocked = False
            for sz in (0.5, 1.0):  # body + head height
                h = query_if.sweep_sphere_closest(AGENT_RADIUS, carb.Float3(wx, wy, sz),
                                                  carb.Float3(ddx, ddy, 0), GRID_RES)
                if h["hit"]:
                    wp = (h.get("rigidBody") or h.get("collider") or "").lower()
                    if any(w in wp for w in WALKABLE):
                        continue
                    blocked = True
                    break
            if not blocked:
                visited.add((ngx, ngy)); queue.append((ngx, ngy))
    return visited


def _nearest_reach_dist(reachable, x, y):
    if not reachable:
        return float("inf")
    gx, gy = round(x / GRID_RES), round(y / GRID_RES)
    rmax = int(REACH_TOL_M / GRID_RES) + 1
    best = float("inf")
    for dx in range(-rmax, rmax + 1):
        for dy in range(-rmax, rmax + 1):
            if (gx + dx, gy + dy) in reachable:
                d = ((dx * GRID_RES) ** 2 + (dy * GRID_RES) ** 2) ** 0.5
                if d < best:
                    best = d
    return best


def _obj_id(name):
    """Stable instance id for dedup. Obj_<id>_<Factory> -> <id>; else first digit run."""
    m = re.match(r'Obj_(\d+)_', name)
    if m:
        return m.group(1)
    ids = re.findall(r'\d+', name)
    return ids[0] if ids else name


def _factory(name):
    """Factory class token from a prim name, or '' if none."""
    m = re.search(r'([A-Za-z]+Factory)', name)
    return m.group(1) if m else ""


def _xy_inside(cx, cy, bmin, bmax):
    return bmin[0] <= cx <= bmax[0] and bmin[1] <= cy <= bmax[1]


def probe_scene(stage, query_if=None):
    # ── Floor surface z (top of the Infinigen room floor meshes) ──
    # Match names ending in "_floor"/"floor" (e.g. living_room_0_0_floor); EXCLUDE
    # "FloorLamp" etc. (which also contain "floor" and would poison the height).
    # Use the lowest floor top (ground) to be robust to mezzanines/raised platforms.
    floor_tops = []
    primary_floor_xy = None; _best_area = -1.0
    for prim in stage.Traverse():
        nm = prim.GetName().lower()
        if nm.endswith("floor") and "lamp" not in nm:
            bb = _world_bbox(stage, prim)
            if bb:
                center, bmin, bmax = bb
                floor_tops.append(bmax[2])  # bbox_max z = floor surface
                # Largest floor mesh = the PRIMARY room; its center seeds the floodfill
                # (room-grounded, matching validate_all_spawns — avoids the probe<->validate
                # reachability disagreement where an object-centroid seed floods another region).
                area = (bmax[0] - bmin[0]) * (bmax[1] - bmin[1])
                if area > _best_area:
                    _best_area = area; primary_floor_xy = (center[0], center[1])
    floor_z = min(floor_tops) if floor_tops else None

    # ── Enumerate object wrappers (direct children of the containers) ──
    # Dedup by instance id, preferring /World/InteractiveProps (what the runner's
    # resolver + dedup operate on).
    by_id = {}
    for cpath in OBJECT_CONTAINERS:
        container = stage.GetPrimAtPath(cpath)
        if not container or not container.IsValid():
            continue
        for child in container.GetChildren():
            name = child.GetName()
            fac = _factory(name)
            if not fac:
                continue
            bb = _world_bbox(stage, child)
            if bb is None:
                continue  # no geometry — skip (an empty/over-only wrapper)
            center, bmin, bmax = bb
            oid = _obj_id(name)
            prefer = cpath == "/World/InteractiveProps"
            if oid in by_id and not prefer:
                continue  # keep existing (InteractiveProps) entry
            bw, bd = bmax[0] - bmin[0], bmax[1] - bmin[1]
            half_ext = max(bw, bd) / 2.0 if 0 < bw < 100 else 0.0
            z_bottom = bmin[2]
            on_floor = (floor_z is not None) and (abs(z_bottom - floor_z) <= ON_FLOOR_THRESH)
            by_id[oid] = {
                "prim_path": child.GetPath().pathString,
                "obj_id": oid,
                "factory": fac,
                "semantic": semantic_class_of(name),
                "center": [round(v, 4) for v in center],
                "bbox_min": [round(v, 4) for v in bmin],
                "bbox_max": [round(v, 4) for v in bmax],
                "z_bottom": round(z_bottom, 4),
                "half_extent_xy": round(half_ext, 4),
                "on_floor": bool(on_floor),
            }

    objects = list(by_id.values())

    # ── Support relations (bbox-stacking): for each object, the highest object
    # whose top surface it rests on; else None (floor if on_floor). ──
    for o in objects:
        if o["on_floor"]:
            o["support"] = None
            continue
        cx, cy = o["center"][0], o["center"][1]
        zb = o["z_bottom"]
        best = None
        best_top = -1e9
        for s in objects:
            if s is o:
                continue
            sb_min, sb_max = s["bbox_min"], s["bbox_max"]
            s_top = sb_max[2]
            if not _xy_inside(cx, cy, sb_min, sb_max):
                continue
            if abs(zb - s_top) <= SUPPORT_Z_TOL and s_top > best_top:
                best, best_top = s, s_top
        o["support"] = best["prim_path"] if best else None
        # half-extent of the supporting furniture — used downstream as the pickup's reach
        # geometry (you reach an object by walking to the furniture it sits on, not its center)
        o["support_he"] = round(best["half_extent_xy"], 4) if best else 0.0

    # ── Reachability (floodfill ONCE per scene; bake per-object so generation can
    # ensure reachability by construction instead of dropping at validation). ──
    reach_meta = {"reachable_cells": None, "reachable_objects": None}
    if query_if is not None:
        # ROOM-GROUNDED seed: floodfill from the PRIMARY (largest) floor mesh center,
        # matching validate_all_spawns (which seeds inside the room floor polygon). The
        # old object-centroid seed could flood a non-primary region → over-report
        # reachable → tasks that validate then dropped (probe<->validate disagreement).
        seeds = []
        if primary_floor_xy:
            cx, cy = primary_floor_xy
            seeds = [(cx, cy), (cx + 1.0, cy), (cx - 1.0, cy), (cx, cy + 1.0), (cx, cy - 1.0)]
        seeds += [(o["center"][0], o["center"][1]) for o in objects if o["on_floor"]][:6]
        reachable = set()
        for sx, sy in seeds:
            r = _flood_fill(query_if, sx, sy)
            if len(r) > len(reachable):
                reachable = r
            if len(reachable) >= 300:   # primary-floor seed succeeded → stop (room-grounded)
                break
        for o in objects:
            d = _nearest_reach_dist(reachable, o["center"][0], o["center"][1])
            o["reach_dist"] = round(d, 3) if d != float("inf") else None
            o["reachable"] = bool(d <= REACH_TOL_M)
        reach_meta = {"reachable_cells": len(reachable),
                      "reachable_objects": sum(1 for o in objects if o["reachable"])}

    # ── Semantic-class counts (for unique-in-room target selection) ──
    counts = {}
    for o in objects:
        counts[o["semantic"]] = counts.get(o["semantic"], 0) + 1

    return {
        "schema_version": SCHEMA,
        "floor_z": round(floor_z, 4) if floor_z is not None else None,
        "object_count": len(objects),
        "reachability": reach_meta,
        "semantic_class_counts": counts,
        "objects": objects,
    }


def main():
    from isaacsim import SimulationApp
    sim_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})
    import omni.usd, omni.timeline, omni.physx, carb
    from omni.isaac.core.utils.stage import is_stage_loading

    sel = os.environ.get("SCENES", "").strip()
    if sel:
        scene_dirs = [os.path.join(BASE, s) for s in sel.split(",")]
    else:
        scene_dirs = sorted(glob.glob(os.path.join(BASE, "*_full_physics_scene")))

    ok, fail = 0, 0
    for sd in scene_dirs:
        scene = os.path.basename(sd).replace("_full_physics_scene", "")
        try:
            sf = discover_scene_files(sd)
            if not sf["stage"]:
                print(f"[PROBE] SKIP {scene}: no stage", flush=True)
                fail += 1
                continue
            omni.usd.get_context().open_stage(sf["stage"])
            while is_stage_loading():
                sim_app.update()
            stage = omni.usd.get_context().get_stage()
            # PhysX warmup so collision meshes are cooked for the floodfill
            tl = omni.timeline.get_timeline_interface(); tl.play()
            for _ in range(20):
                sim_app.update()
            tl.pause()
            query_if = omni.physx.get_physx_scene_query_interface()
            facts = probe_scene(stage, query_if)
            facts["scene_name"] = scene
            out = os.path.join(sd, "scene_facts.json")
            with open(out, "w") as f:
                json.dump(facts, f, indent=2)
            nfloor = sum(1 for o in facts["objects"] if o["on_floor"])
            print(f"[PROBE] OK {scene}: {facts['object_count']} objs "
                  f"({nfloor} on_floor) floor_z={facts['floor_z']} -> {out}", flush=True)
            ok += 1
        except Exception as e:
            print(f"[PROBE] FAIL {scene}: {e}\n{traceback.format_exc()}", flush=True)
            fail += 1

    print(f"[PROBE] DONE ok={ok} fail={fail}", flush=True)
    sim_app.close()


if __name__ == "__main__":
    main()
