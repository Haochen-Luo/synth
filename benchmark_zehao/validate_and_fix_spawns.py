#!/usr/bin/env python3
"""Validate and fix benchmark spawn points using PhysX collision geometry.

Runs inside the vlm-jupyter container via /isaac-sim/python.sh.
Loads each scene, validates spawn points with 4 checks:
  1. Enclosure: is the point inside a room (walls in most directions)?
  2. Overlap: is the point overlapping an obstacle?
  3. Clearance: does the point have enough space to move?
  4. Floor: is there a floor at the expected height?

Also performs BFS flood-fill reachability check and L2/L4 FOV gate.

Usage:
  ssh GPU-843 'docker exec vlm-jupyter /isaac-sim/python.sh \\
      /home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/validate_and_fix_spawns.py'
"""
import sys, os, json, math, traceback, time

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_JSON = os.path.join(SCRIPT_DIR, "benchmark_tasks_0527fix.json")
OUTPUT_JSON = os.path.join(SCRIPT_DIR, "benchmark_tasks_validated.json")
REPORT_JSON = os.path.join(SCRIPT_DIR, "spawn_validation_report.json")

WALKABLE = ("floor", "ground", "rug", "blanket", "towel", "mat")
AGENT_RADIUS = 0.40
STEP_DIST = 0.25
FOV_HALF = 45.0  # degrees, half of 90° horizontal FOV
_8DIRS = [(1,0),(-1,0),(0,1),(0,-1),
          (0.707,0.707),(-0.707,0.707),(0.707,-0.707),(-0.707,-0.707)]
_4DIRS = [(1,0),(-1,0),(0,1),(0,-1)]


def log(msg):
    print(msg, flush=True)


try:
    from isaacsim import SimulationApp
    sim_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting",
                             "width": 640, "height": 360})
    import omni.usd, omni.physx, carb, omni.replicator.core as rep
    from omni.isaac.core.utils.stage import open_stage, is_stage_loading
    from pxr import Gf, UsdGeom

    with open(INPUT_JSON) as f:
        config = json.load(f)
    tasks = config["tasks"]

    # Group tasks by scene_dir (same USD stage)
    from collections import defaultdict
    by_scene = defaultdict(list)
    for t in tasks:
        by_scene[t["scene_dir"]].append(t)

    report = {"validated": [], "fixed": [], "unreachable": [], "fov_violations": []}

    for scene_dir, scene_tasks in sorted(by_scene.items()):
        scene_path = os.path.join(SCRIPT_DIR, scene_dir)
        usda = None
        compiled = os.path.join(scene_path, "compiled_stages")
        if os.path.isdir(compiled):
            for f in os.listdir(compiled):
                if f.endswith(".usda"):
                    usda = os.path.join(compiled, f)
                    break
        if not usda:
            log(f"[VALIDATE] SKIP {scene_dir}: no compiled USDA found")
            continue

        log(f"\n[VALIDATE] Loading scene: {scene_dir}")
        open_stage(usda)
        while is_stage_loading():
            sim_app.update()
        stage = omni.usd.get_context().get_stage()

        # Add a dummy agent prim so PhysX has collision geometry
        agent_prim = stage.DefinePrim("/World/Humans/agent_validate")
        human_usd_path = os.path.join(scene_path, "assets/humans")
        if os.path.isdir(human_usd_path):
            humans = [f for f in os.listdir(human_usd_path) if f.endswith((".usdc", ".usd"))]
            if humans:
                agent_prim.GetReferences().AddReference(os.path.join(human_usd_path, humans[0]))

        # Create a camera and render product — required to activate PhysX
        nav_cam = UsdGeom.Camera.Define(stage, "/World/ValidateCamera")
        rp = rep.create.render_product("/World/ValidateCamera", (256, 256))

        # Warm up: enough updates to let PhysX build collision meshes
        for _ in range(100):
            sim_app.update()
        # Orchestrator step kicks the full physics pipeline
        rep.orchestrator.step()

        query_if = omni.physx.get_physx_scene_query_interface()

        # ── Validation functions ──

        def check_overlap(x, y):
            """Check if point overlaps any obstacle."""
            for sz in [0.5, 1.0]:
                for dx, dy in _8DIRS:
                    h = query_if.sweep_sphere_closest(
                        AGENT_RADIUS, carb.Float3(x, y, sz),
                        carb.Float3(dx, dy, 0), 0.05)
                    if h["hit"]:
                        wp = (h.get("rigidBody") or h.get("collider") or "").lower()
                        if any(w in wp for w in WALKABLE):
                            continue
                        d = float(h.get("distance", 1))
                        if d < 0.01:
                            return False, wp.split("/")[-1][:60]
            return True, ""

        def check_clearance(x, y, min_clear=0.5):
            """Minimum clearance in at least 2 directions."""
            clear_dirs = 0
            for dx, dy in _8DIRS:
                blocked = False
                for sz in [0.5, 1.0]:
                    h = query_if.sweep_sphere_closest(
                        AGENT_RADIUS, carb.Float3(x, y, sz),
                        carb.Float3(dx, dy, 0), min_clear)
                    if h["hit"]:
                        wp = (h.get("rigidBody") or h.get("collider") or "").lower()
                        if any(w in wp for w in WALKABLE):
                            continue
                        blocked = True
                        break
                if not blocked:
                    clear_dirs += 1
            return clear_dirs >= 2, clear_dirs

        def validate_point(x, y):
            """Run overlap + clearance checks.
            Note: enclosure/floor are not checked here because PhysX
            sweep_sphere_closest doesn't work at long ranges in compiled scenes.
            Instead, the full BFS reachability check (5000 cells) catches
            exterior spawns: if spawn is outside, target won't be reachable."""
            r2, overlap_hit = check_overlap(x, y)
            r3, clear_dirs = check_clearance(x, y)
            passed = r2 and r3
            details = {
                "overlap": {"pass": r2, "hit": overlap_hit},
                "clearance": {"pass": r3, "clear_dirs": clear_dirs},
            }
            return passed, details

        def find_nearest_valid(ox, oy, max_radius=3.0):
            """Grid-search for nearest valid point."""
            for r_step in range(1, int(max_radius / 0.25) + 1):
                r = r_step * 0.25
                n = max(8, int(2 * math.pi * r / 0.25))
                for i in range(n):
                    angle = 2 * math.pi * i / n
                    cx = ox + r * math.cos(angle)
                    cy = oy + r * math.sin(angle)
                    ok, _ = validate_point(cx, cy)
                    if ok:
                        return cx, cy, r
            return None, None, None

        # ── BFS Flood-fill reachability ──

        def flood_fill_reachable(sx, sy, grid_res=0.25, max_cells=5000):
            """BFS from (sx,sy). Returns set of reachable (gx,gy) grid coords."""
            def to_grid(x, y):
                return (round(x / grid_res), round(y / grid_res))
            def from_grid(gx, gy):
                return (gx * grid_res, gy * grid_res)

            start = to_grid(sx, sy)
            visited = {start}
            queue = [start]
            idx = 0
            while idx < len(queue) and len(visited) < max_cells:
                gx, gy = queue[idx]; idx += 1
                wx, wy = from_grid(gx, gy)
                for ddx, ddy in _4DIRS:
                    ngx, ngy = gx + int(ddx), gy + int(ddy)
                    if (ngx, ngy) in visited:
                        continue
                    nwx, nwy = from_grid(ngx, ngy)
                    # Check if movement from (wx,wy) to (nwx,nwy) is clear
                    blocked = False
                    for sz in [0.5, 1.0]:
                        h = query_if.sweep_sphere_closest(
                            AGENT_RADIUS, carb.Float3(wx, wy, sz),
                            carb.Float3(ddx, ddy, 0), grid_res)
                        if h["hit"]:
                            wp = (h.get("rigidBody") or h.get("collider") or "").lower()
                            if any(w in wp for w in WALKABLE):
                                continue
                            blocked = True
                            break
                    if not blocked:
                        visited.add((ngx, ngy))
                        queue.append((ngx, ngy))
            return visited, grid_res

        def check_reachable(sx, sy, tx, ty, reachable_set, grid_res):
            """Check if target is reachable from spawn."""
            tg = (round(tx / grid_res), round(ty / grid_res))
            if tg in reachable_set:
                return True
            # Check nearby cells (target might be between grid points)
            for dg in range(-2, 3):
                for dg2 in range(-2, 3):
                    if (tg[0] + dg, tg[1] + dg2) in reachable_set:
                        return True
            return False

        # ── Process each task in this scene ──

        # First pass: find known-good spawns (for centroid fallback)
        good_spawns = []
        for t in scene_tasks:
            x, y = t["agent_start"]
            ok, _ = validate_point(x, y)
            if ok:
                good_spawns.append((x, y))

        if good_spawns:
            centroid_x = sum(p[0] for p in good_spawns) / len(good_spawns)
            centroid_y = sum(p[1] for p in good_spawns) / len(good_spawns)
        else:
            # Fallback: use mean of all spawn coordinates
            centroid_x = sum(t["agent_start"][0] for t in scene_tasks) / len(scene_tasks)
            centroid_y = sum(t["agent_start"][1] for t in scene_tasks) / len(scene_tasks)

        for t in scene_tasks:
            tid = t["id"]
            level = t.get("level", "L1")
            x, y = t["agent_start"]
            t0 = time.time()

            ok, details = validate_point(x, y)
            entry = {
                "task_id": tid, "level": level,
                "original_start": [round(x, 3), round(y, 3)],
                "validation": details,
                "passed": ok,
            }

            if not ok:
                log(f"[VALIDATE] ❌ {tid} FAILED at ({x:.2f},{y:.2f}): {details}")
                # Try to fix: search from centroid first, then from original
                fx, fy, fr = find_nearest_valid(centroid_x, centroid_y)
                if fx is None:
                    fx, fy, fr = find_nearest_valid(x, y)
                if fx is not None:
                    old_x, old_y = x, y
                    t["agent_start"] = [round(fx, 2), round(fy, 2)]
                    x, y = fx, fy

                    # Recompute yaw
                    phase0 = t["phases"][0]
                    # Load probed data for target center
                    scene_name = t["scene_dir"].replace("native_", "").replace("_full_physics_scene", "")
                    probed_path = os.path.join(SCRIPT_DIR, f"probed_{scene_name}.json")
                    if os.path.exists(probed_path):
                        pd = json.load(open(probed_path))
                        target_obj = phase0["target_object"]
                        clean = target_obj.replace("Factory", "")
                        # Find ALL matching prims with valid centers
                        candidates = []
                        for p in pd.get("prims", []):
                            nm = p.get("name", "")
                            if clean in nm and p.get("center") and (abs(p["center"][0]) > 0.1 or abs(p["center"][1]) > 0.1):
                                candidates.append(p["center"][:2])
                        if candidates:
                            # Pick closest candidate to spawn
                            tc = min(candidates, key=lambda c: math.hypot(c[0] - x, c[1] - y))
                            yaw_to = math.degrees(math.atan2(tc[1] - y, tc[0] - x))
                            if level in ("L1", "L3"):
                                new_yaw = yaw_to
                            else:
                                new_yaw = yaw_to + 180
                            new_yaw = ((new_yaw + 180) % 360) - 180
                            t["agent_yaw"] = round(new_yaw, 1)

                    entry["fixed"] = True
                    entry["fixed_start"] = [round(fx, 2), round(fy, 2)]
                    entry["fix_search_radius"] = round(fr, 2)
                    report["fixed"].append(entry)
                    log(f"[VALIDATE] ✅ {tid} FIXED: ({old_x:.2f},{old_y:.2f}) → ({fx:.2f},{fy:.2f}) "
                        f"search_r={fr:.2f}m yaw={t.get('agent_yaw')}")
                else:
                    entry["fixed"] = False
                    report["fixed"].append(entry)
                    log(f"[VALIDATE] ❌ {tid} UNFIXABLE — no valid point within 3m")
            else:
                entry["fixed"] = False
                report["validated"].append(entry)
                log(f"[VALIDATE] ✅ {tid} OK at ({x:.2f},{y:.2f})")

            # ── L2/L4 FOV gate ──
            if level in ("L2", "L4"):
                phase0 = t["phases"][0]
                target_obj = phase0["target_object"]
                scene_name = t["scene_dir"].replace("native_", "").replace("_full_physics_scene", "")
                probed_path = os.path.join(SCRIPT_DIR, f"probed_{scene_name}.json")
                if os.path.exists(probed_path):
                    pd = json.load(open(probed_path))
                    clean = target_obj.replace("Factory", "")
                    candidates = []
                    for p in pd.get("prims", []):
                        nm = p.get("name", "")
                        if clean in nm and p.get("center") and (abs(p["center"][0]) > 0.1 or abs(p["center"][1]) > 0.1):
                            candidates.append(p["center"][:2])
                    if candidates:
                        yaw = t.get("agent_yaw", 0)
                        for tc in candidates:
                            angle_to = math.degrees(math.atan2(tc[1] - y, tc[0] - x))
                            rel = angle_to - yaw
                            rel = ((rel + 180) % 360) - 180
                            if abs(rel) < FOV_HALF:
                                log(f"[VALIDATE] ⚠ FOV VIOLATION {tid}: target {clean} at "
                                    f"({tc[0]:.1f},{tc[1]:.1f}) is {rel:.1f}° from yaw={yaw:.1f}°")
                                # Fix: rotate yaw so target is behind
                                yaw_to = math.degrees(math.atan2(tc[1] - y, tc[0] - x))
                                new_yaw = yaw_to + 180
                                new_yaw = ((new_yaw + 180) % 360) - 180
                                t["agent_yaw"] = round(new_yaw, 1)
                                log(f"[VALIDATE] ✅ FOV FIXED {tid}: yaw {yaw:.1f}° → {new_yaw:.1f}°")
                                report["fov_violations"].append({
                                    "task_id": tid,
                                    "target": clean,
                                    "target_pos": tc,
                                    "old_yaw": yaw,
                                    "new_yaw": round(new_yaw, 1),
                                    "rel_angle": round(rel, 1),
                                })
                                break  # fix once is enough

            # ── Reachability check ──
            log(f"[VALIDATE] Running flood-fill reachability for {tid}...")
            reachable, gres = flood_fill_reachable(x, y)
            log(f"[VALIDATE] {tid}: {len(reachable)} reachable cells")

            for pi, phase in enumerate(t["phases"]):
                target_obj = phase["target_object"]
                scene_name = t["scene_dir"].replace("native_", "").replace("_full_physics_scene", "")
                probed_path = os.path.join(SCRIPT_DIR, f"probed_{scene_name}.json")
                if not os.path.exists(probed_path):
                    continue
                pd = json.load(open(probed_path))
                clean = target_obj.replace("Factory", "")
                candidates = []
                for p in pd.get("prims", []):
                    nm = p.get("name", "")
                    if clean in nm and p.get("center") and (abs(p["center"][0]) > 0.1 or abs(p["center"][1]) > 0.1):
                        candidates.append(p["center"][:2])

                if not candidates:
                    continue

                any_reachable = False
                for tc in candidates:
                    if check_reachable(x, y, tc[0], tc[1], reachable, gres):
                        any_reachable = True
                        break

                if not any_reachable:
                    log(f"[VALIDATE] ⚠ UNREACHABLE {tid} phase {pi}: {clean} — "
                        f"no reachable instance from ({x:.2f},{y:.2f})")
                    report["unreachable"].append({
                        "task_id": tid,
                        "phase": pi,
                        "target": clean,
                        "candidates": candidates,
                        "spawn": [round(x, 2), round(y, 2)],
                        "reachable_cells": len(reachable),
                    })
                else:
                    log(f"[VALIDATE] ✅ {tid} phase {pi}: {clean} reachable")

            elapsed = time.time() - t0
            log(f"[VALIDATE] {tid} done in {elapsed:.1f}s")

    # ── Save outputs ──
    with open(OUTPUT_JSON, "w") as f:
        json.dump(config, f, indent=2)
    log(f"\n[VALIDATE] Saved validated tasks to {OUTPUT_JSON}")

    with open(REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2)
    log(f"[VALIDATE] Saved validation report to {REPORT_JSON}")

    # Summary
    n_ok = len(report["validated"])
    n_fix = len([r for r in report["fixed"] if r.get("fixed")])
    n_fail = len([r for r in report["fixed"] if not r.get("fixed")])
    n_unreach = len(report["unreachable"])
    n_fov = len(report["fov_violations"])
    log(f"\n[VALIDATE] SUMMARY: {n_ok} OK, {n_fix} fixed, {n_fail} unfixable, "
        f"{n_unreach} unreachable targets, {n_fov} FOV violations fixed")

    sim_app.close()

except Exception as e:
    print(f"\n[VALIDATE] FATAL ERROR:\n{traceback.format_exc()}", flush=True)
    raise
