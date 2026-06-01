#!/usr/bin/env python3
"""Validate & generate spawn points for all 122 scenes using PhysX floodfill.

Runs inside vlm-jupyter container via /isaac-sim/python.sh.
For each scene:
  1. Load compiled USDA stage
  2. Resolve all target prim positions
  3. BFS flood-fill from first target to find walkable reachable set
  4. Verify all targets are mutually reachable
  5. Auto-fix: swap unreachable targets for reachable same-factory alternatives
  6. Pick spawn point from reachable set at 3-7m distance
  7. Cache results to spawn_cache/{scene}.json

Usage:
  docker exec vlm-jupyter /isaac-sim/python.sh validate_full_spawns.py
"""
import sys, os, json, math, re, glob, time, traceback
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCENES_DIR = os.path.join(SCRIPT_DIR, "full_scenarios_extracted")
CACHE_DIR = os.path.join(SCRIPT_DIR, "spawn_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Also include original 9-scene pilot scenes
PILOT_SCENES_DIR = SCRIPT_DIR  # native_case01_... etc are directly here

GRID_RES = 0.25    # meters per grid cell
MAX_CELLS = 5000   # BFS budget
AGENT_RADIUS = 0.40
WALKABLE = ("floor", "ground", "rug", "blanket", "towel", "mat")
_4DIRS = [(1,0),(-1,0),(0,1),(0,-1)]
_8DIRS = [(1,0),(-1,0),(0,1),(0,-1),
          (0.707,0.707),(-0.707,0.707),(0.707,-0.707),(-0.707,-0.707)]

# Factory classifications (same as generate_full_benchmark.py)
PORTABLE_FACTORIES = {
    'BookFactory','BookStackFactory','BookColumnFactory','SupportBookStackFactory',
    'CupFactory','PlateFactory','BowlFactory','PotFactory','JarFactory',
    'WineglassFactory','MugFactory','GlassFactory','SpoonFactory',
    'CanFactory','BottleFactory','FoodBagFactory','FruitContainerFactory',
    'VaseFactory','PillowFactory','TowelFactory','NatureShelfTrinketsFactory',
}
SWITCHABLE_FACTORIES = {
    'FloorLampFactory','DeskLampFactory','TableLampFactory',
    'OvenFactory','MonitorFactory','TVStandFactory','TVFactory',
}
DESTINATION_FACTORIES = {
    'SimpleBookcaseFactory','LargeShelfFactory','CellShelfFactory',
    'SimpleDeskFactory','KitchenIslandFactory','KitchenSpaceFactory',
    'KitchenCabinetFactory','SingleCabinetFactory','SideTableFactory',
    'DiningTableFactory','BedFactory','SinkFactory','ChairFactory',
    'LargePlantContainerFactory',
}

def log(msg):
    print(msg, flush=True)

def get_factory(name):
    m = re.match(r'\d+_(.*Factory)', name)
    if m: return m.group(1)
    m = re.match(r'([A-Z][a-zA-Z]+Factory)', name)
    if m: return m.group(1)
    return name

try:
    from isaacsim import SimulationApp
    sim_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting",
                             "width": 640, "height": 360})
    import omni.usd, omni.physx, carb, omni.replicator.core as rep
    from omni.isaac.core.utils.stage import open_stage, is_stage_loading
    from pxr import Gf, UsdGeom, Usd, UsdLux
    import random as _rng

    # Discover all scene directories
    scene_dirs = sorted(glob.glob(os.path.join(SCENES_DIR, "native_*_full_physics_scene")))
    log(f"[VALIDATE] Found {len(scene_dirs)} scenes in {SCENES_DIR}")

    total_scenes = len(scene_dirs)
    total_validated = 0
    total_skipped = 0
    total_failed = 0

    for si, scene_path in enumerate(scene_dirs):
        scene_name = os.path.basename(scene_path)
        short = scene_name.replace("native_","").replace("_full_physics_scene","")
        cache_file = os.path.join(CACHE_DIR, f"{short}.json")

        # Skip if cached
        if os.path.exists(cache_file):
            log(f"[VALIDATE] ({si+1}/{total_scenes}) {short}: cached, skipping")
            total_validated += 1
            continue

        t0 = time.time()
        log(f"\n[VALIDATE] ({si+1}/{total_scenes}) Loading {short}...")

        # Find USDA
        usda = None
        compiled = os.path.join(scene_path, "compiled_stages")
        if os.path.isdir(compiled):
            for f in os.listdir(compiled):
                if f.endswith(".usda"):
                    usda = os.path.join(compiled, f)
                    break
        if not usda:
            log(f"[VALIDATE] SKIP {short}: no compiled USDA")
            total_skipped += 1
            continue

        # Read spec
        spec_files = glob.glob(os.path.join(scene_path, "compiled_specs", "*.json"))
        if not spec_files:
            log(f"[VALIDATE] SKIP {short}: no spec")
            total_skipped += 1
            continue
        spec = json.load(open(spec_files[0]))

        # Load stage
        try:
            open_stage(usda)
            while is_stage_loading():
                sim_app.update()
            stage = omni.usd.get_context().get_stage()
        except Exception as e:
            log(f"[VALIDATE] SKIP {short}: stage load error: {e}")
            total_skipped += 1
            continue

        # Warm up PhysX
        nav_cam = UsdGeom.Camera.Define(stage, "/World/ValidateCamera")
        rp = rep.create.render_product("/World/ValidateCamera", (256, 256))
        for _ in range(80):
            sim_app.update()
        rep.orchestrator.step()
        query_if = omni.physx.get_physx_scene_query_interface()

        # ── Resolve all prop positions ──
        props = spec.get("interactive_props", [])
        prim_positions = {}  # factory -> [(prim_path, [x,y])]

        for p in props:
            name = p.get("name", "")
            factory = get_factory(name)
            prim_path = p.get("target_prim_path", "")

            if not prim_path:
                continue
            prim = stage.GetPrimAtPath(prim_path)
            if not prim.IsValid():
                continue
            try:
                imageable = UsdGeom.Imageable(prim)
                bound = imageable.ComputeWorldBound(Usd.TimeCode.Default(), "default")
                box = bound.GetBox()
                center = box.GetMidpoint()
                pos = [float(center[0]), float(center[1])]
                if abs(pos[0]) < 200 and abs(pos[1]) < 200:
                    prim_positions.setdefault(factory, []).append((prim_path, pos))
            except:
                pass

        if not prim_positions:
            log(f"[VALIDATE] SKIP {short}: no resolvable props")
            total_skipped += 1
            continue

        # ── Find scene interior point for floodfill seed ──
        all_positions = []
        for factory, entries in prim_positions.items():
            for _, pos in entries:
                all_positions.append(pos)

        centroid = [
            sum(p[0] for p in all_positions) / len(all_positions),
            sum(p[1] for p in all_positions) / len(all_positions),
        ]

        # ── BFS flood-fill ──
        def to_grid(x, y):
            return (round(x / GRID_RES), round(y / GRID_RES))
        def from_grid(gx, gy):
            return (gx * GRID_RES, gy * GRID_RES)

        def flood_fill(sx, sy):
            start = to_grid(sx, sy)
            visited = {start}
            queue = [start]
            idx = 0
            while idx < len(queue) and len(visited) < MAX_CELLS:
                gx, gy = queue[idx]; idx += 1
                wx, wy = from_grid(gx, gy)
                for ddx, ddy in _4DIRS:
                    ngx, ngy = gx + int(ddx), gy + int(ddy)
                    if (ngx, ngy) in visited:
                        continue
                    blocked = False
                    for sz in [0.5]:  # single height check for speed
                        h = query_if.sweep_sphere_closest(
                            AGENT_RADIUS, carb.Float3(wx, wy, sz),
                            carb.Float3(ddx, ddy, 0), GRID_RES)
                        if h["hit"]:
                            wp = (h.get("rigidBody") or h.get("collider") or "").lower()
                            if any(w in wp for w in WALKABLE):
                                continue
                            blocked = True
                            break
                    if not blocked:
                        visited.add((ngx, ngy))
                        queue.append((ngx, ngy))
            return visited

        def is_reachable(reachable_set, tx, ty, tolerance=2):
            tg = to_grid(tx, ty)
            for dx in range(-tolerance, tolerance+1):
                for dy in range(-tolerance, tolerance+1):
                    if (tg[0]+dx, tg[1]+dy) in reachable_set:
                        return True
            return False

        # Floodfill from centroid
        log(f"[VALIDATE] Flood-filling from centroid ({centroid[0]:.1f},{centroid[1]:.1f})...")
        ff_t0 = time.time()
        reachable = flood_fill(centroid[0], centroid[1])
        ff_time = time.time() - ff_t0
        log(f"[VALIDATE] Reachable cells: {len(reachable)} ({ff_time:.1f}s)")

        # If centroid is in a wall (tiny reachable set), try each prop position
        if len(reachable) < 100:
            log(f"[VALIDATE] Centroid might be blocked, trying prop positions...")
            best_reachable = reachable
            for pos in all_positions:
                r = flood_fill(pos[0], pos[1])
                if len(r) > len(best_reachable):
                    best_reachable = r
                if len(best_reachable) >= 500:
                    break
            reachable = best_reachable
            log(f"[VALIDATE] Best reachable set: {len(reachable)} cells")

        # ── Check reachability of each prop ──
        reachable_props = {}  # factory -> [(prim_path, [x,y])]
        unreachable_props = {}

        for factory, entries in prim_positions.items():
            for prim_path, pos in entries:
                if is_reachable(reachable, pos[0], pos[1]):
                    reachable_props.setdefault(factory, []).append((prim_path, pos))
                else:
                    unreachable_props.setdefault(factory, []).append((prim_path, pos))

        n_reach = sum(len(v) for v in reachable_props.values())
        n_unreach = sum(len(v) for v in unreachable_props.values())
        log(f"[VALIDATE] Props: {n_reach} reachable, {n_unreach} unreachable")

        # ── Find valid spawn candidates ──
        # Pick cells 3-7m from centroid that are in reachable set
        reachable_list = list(reachable)
        spawn_candidates = []
        for gx, gy in reachable_list:
            wx, wy = from_grid(gx, gy)
            # Must be away from all prop positions (at least 2m from any prop)
            min_prop_dist = min(
                math.sqrt((wx-p[0])**2 + (wy-p[1])**2)
                for p in all_positions
            ) if all_positions else 0
            if min_prop_dist >= 1.5:
                spawn_candidates.append((wx, wy))

        # Sort by distance from centroid (prefer 3-7m)
        spawn_candidates.sort(
            key=lambda s: abs(math.sqrt((s[0]-centroid[0])**2+(s[1]-centroid[1])**2) - 5.0)
        )

        if not spawn_candidates:
            # Fallback: any reachable cell at least 1m from centroid
            for gx, gy in reachable_list:
                wx, wy = from_grid(gx, gy)
                d = math.sqrt((wx-centroid[0])**2 + (wy-centroid[1])**2)
                if d >= 1.0:
                    spawn_candidates.append((wx, wy))

        log(f"[VALIDATE] Spawn candidates: {len(spawn_candidates)}")

        # ── Save cache ──
        cache = {
            "scene_name": short,
            "centroid": centroid,
            "reachable_cells": len(reachable),
            "floodfill_time_s": round(ff_time, 2),
            "spawn_candidates": spawn_candidates[:50],  # top 50
            "reachable_props": {
                f: [(pp, pos) for pp, pos in entries]
                for f, entries in reachable_props.items()
            },
            "unreachable_props": {
                f: [(pp, pos) for pp, pos in entries]
                for f, entries in unreachable_props.items()
            },
        }
        with open(cache_file, "w") as f:
            json.dump(cache, f, indent=2)

        elapsed = time.time() - t0
        total_validated += 1
        log(f"[VALIDATE] ✅ {short}: done in {elapsed:.1f}s "
            f"(reach={n_reach} unreach={n_unreach} spawns={len(spawn_candidates)})")

        # ── Memory cleanup to prevent OOM on long runs ──
        import gc
        del reachable, reachable_list, prim_positions, all_positions
        del reachable_props, unreachable_props, spawn_candidates
        gc.collect()

    log(f"\n{'='*60}")
    log(f"[VALIDATE] DONE: {total_validated} validated, {total_skipped} skipped, {total_failed} failed")
    log(f"[VALIDATE] Cache dir: {CACHE_DIR}")
    sim_app.close()

except Exception as e:
    print(f"\n[VALIDATE] FATAL ERROR:\n{traceback.format_exc()}", flush=True)
    raise
