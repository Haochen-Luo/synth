#!/usr/bin/env python3
"""
Validate all benchmark spawn points against 3 criteria:
  1. Floor BBox — spawn must be within the room's floor mesh bounds
  2. Collision Clearance — no geometry overlap at spawn (PhysX sweep)
  3. FOV Constraint — L1/L3: target visible, L2/L4: target hidden

Usage (inside vlm-jupyter container):
  /isaac-sim/python.sh validate_all_spawns.py           # report only
  /isaac-sim/python.sh validate_all_spawns.py --fix      # auto-fix & write JSON

Results are written to spawn_validation_report.json.
"""
import sys, os, json, math, traceback
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BENCH_DIR  = os.path.dirname(os.path.abspath(__file__))
TASKS_JSON = os.path.join(BENCH_DIR, "benchmark_tasks.json")
REPORT_OUT = os.path.join(BENCH_DIR, "spawn_validation_report.json")

FIX_MODE = "--fix" in sys.argv

# ── Geometry / FOV constants ──
FLOOR_INSET   = 0.3   # metres inward from floor bbox edges (avoid wall-hugging)
SWEEP_RADIUS  = 0.40  # PhysX sweep sphere radius (matches bench_runner)
SWEEP_DIST    = 0.05  # sweep travel distance
FOV_HALF_DEG  = 45.0  # ±45° FOV cone for visibility check
# Prims that the sweep sphere may touch but are not real obstacles.
# Room structure (wall, ceiling, exterior, skirting) is always near spawn
# because rooms are small; we only care about furniture overlaps.
WALKABLE      = ("floor", "ground", "rug", "blanket", "towel", "mat",
                 "wall", "ceiling", "exterior", "skirtingboard",
                 "skirting", "baseboard")

# ── Search grid for auto-fix ──
GRID_STEP     = 0.5   # metres between candidate points
SPIRAL_STEP   = 0.25
SPIRAL_MAX_R  = 2.0

def log(msg):
    print(msg, flush=True)

# ─────────────────────────────────────────────────────────────────
# Load tasks & group by scene
# ─────────────────────────────────────────────────────────────────
with open(TASKS_JSON) as f:
    task_cfg = json.load(f)
tasks = task_cfg["tasks"]
log(f"[VAL] Loaded {len(tasks)} tasks from {TASKS_JSON}")

scene_tasks = defaultdict(list)
for t in tasks:
    scene_tasks[t["scene_dir"]].append(t)
log(f"[VAL] {len(scene_tasks)} unique scenes")

# ─────────────────────────────────────────────────────────────────
# Isaac Sim startup
# ─────────────────────────────────────────────────────────────────
from isaacsim import SimulationApp
sim_app = SimulationApp({"headless": True, "width": 64, "height": 64})

import omni, omni.physx, omni.timeline, carb
from omni.isaac.core.utils.stage import is_stage_loading
from pxr import Usd, UsdGeom, Gf
from bench_helpers import discover_scene_files, find_prim_by_factory, get_prim_world_center

# ─────────────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────────────
_8DIRS = [(1,0),(-1,0),(0,1),(0,-1),
          (0.707,0.707),(-0.707,0.707),(0.707,-0.707),(-0.707,-0.707)]

def find_floor_bbox(stage):
    """Return (xmin, ymin, xmax, ymax) from the living_room floor Xform."""
    for prim in stage.Traverse():
        name = prim.GetName().lower()
        if name.endswith("_floor") and prim.GetTypeName() in ("Xform", "Scope"):
            imageable = UsdGeom.Imageable(prim)
            if not imageable:
                continue
            bound = imageable.ComputeWorldBound(Usd.TimeCode.Default(), "default")
            box = bound.GetBox()
            mn, mx = box.GetMin(), box.GetMax()
            if mx[0] > mn[0] and mx[1] > mn[1]:
                return (float(mn[0]), float(mn[1]), float(mx[0]), float(mx[1]))
    return None

def check_in_floor(x, y, bbox, margin=FLOOR_INSET):
    """Check if (x,y) is within floor bbox, inset by margin."""
    if bbox is None:
        return True  # can't check, assume OK
    xmin, ymin, xmax, ymax = bbox
    return (xmin + margin <= x <= xmax - margin and
            ymin + margin <= y <= ymax - margin)

def check_collision_clear(query_if, cx, cy):
    """PhysX sweep — returns True if spawn is clear."""
    for sz in [0.5, 1.0]:
        for dx, dy in _8DIRS:
            h = query_if.sweep_sphere_closest(
                SWEEP_RADIUS, carb.Float3(cx, cy, sz),
                carb.Float3(dx, dy, 0), SWEEP_DIST)
            if h["hit"]:
                wp = (h.get("rigidBody") or h.get("collider") or "").lower()
                if any(w in wp for w in WALKABLE):
                    continue
                d = float(h.get("distance", 0))
                if d < 0.01:
                    return False, wp.split("/")[-1][:60]
    return True, ""

def check_fov(sx, sy, yaw_deg, target_xy, level):
    """
    L1/L3 → target MUST be within ±FOV_HALF_DEG of yaw (visible)
    L2/L4 → target must NOT be within ±FOV_HALF_DEG of yaw (hidden)
    Returns (pass, detail_string).
    """
    if target_xy is None:
        return True, "no_target"
    tx, ty = target_xy
    angle_to_target = math.degrees(math.atan2(ty - sy, tx - sx))
    rel = ((angle_to_target - yaw_deg + 180) % 360) - 180
    in_fov = abs(rel) <= FOV_HALF_DEG
    lnum = int(level.replace("L",""))
    want_visible = (lnum % 2 == 1)  # L1, L3 → odd → visible
    ok = (in_fov == want_visible)
    detail = f"rel_angle={rel:.1f}° in_fov={in_fov} want_visible={want_visible}"
    return ok, detail

def fix_yaw_for_fov(sx, sy, target_xy, level):
    """Compute a yaw that satisfies the FOV constraint for the given level."""
    if target_xy is None:
        return 0.0
    tx, ty = target_xy
    angle_to_target = math.degrees(math.atan2(ty - sy, tx - sx))
    lnum = int(level.replace("L",""))
    want_visible = (lnum % 2 == 1)
    if want_visible:
        # face toward target
        return angle_to_target
    else:
        # face AWAY from target (rotate 180°)
        return ((angle_to_target + 180 + 180) % 360) - 180

# ─────────────────────────────────────────────────────────────────
# Main validation loop
# ─────────────────────────────────────────────────────────────────
all_results = []
fixes_applied = 0

for scene_dir_name, scene_task_list in sorted(scene_tasks.items()):
    scene_dir = os.path.join(BENCH_DIR, scene_dir_name)
    sf = discover_scene_files(scene_dir)
    if not sf["stage"]:
        log(f"[VAL] ❌ No stage found for {scene_dir_name}")
        for t in scene_task_list:
            all_results.append({"task_id": t["id"], "status": "ERROR", "reason": "no_stage"})
        continue

    log(f"\n{'='*60}")
    log(f"[VAL] Loading scene: {scene_dir_name} ({len(scene_task_list)} tasks)")
    log(f"{'='*60}")

    omni.usd.get_context().open_stage(sf["stage"])
    while is_stage_loading():
        sim_app.update()
    stage = omni.usd.get_context().get_stage()

    # ── Floor bbox ──
    floor_bbox = find_floor_bbox(stage)
    if floor_bbox:
        log(f"[VAL] Floor bbox: x=[{floor_bbox[0]:.2f}, {floor_bbox[2]:.2f}] "
            f"y=[{floor_bbox[1]:.2f}, {floor_bbox[3]:.2f}]")
    else:
        log(f"[VAL] ⚠ No floor bbox found — skipping bbox check")

    # ── PhysX init: need timeline to cook collision meshes ──
    timeline = omni.timeline.get_timeline_interface()
    timeline.play()
    for _ in range(20):
        sim_app.update()
    timeline.pause()

    query_if = omni.physx.get_physx_scene_query_interface()

    # ── Per-task validation ──
    for t in scene_task_list:
        tid   = t["id"]
        level = t["level"]
        sx, sy = t["agent_start"]
        yaw    = t.get("agent_yaw", 0.0)
        phases = t.get("phases", [])

        result = {
            "task_id": tid, "level": level,
            "original_start": [sx, sy], "original_yaw": yaw,
            "checks": {}, "status": "PASS", "fixes": []
        }

        # ── Check 1: Floor BBox ──
        in_floor = check_in_floor(sx, sy, floor_bbox)
        result["checks"]["floor_bbox"] = {
            "pass": in_floor,
            "floor_bbox": list(floor_bbox) if floor_bbox else None,
            "detail": f"({sx:.2f}, {sy:.2f}) {'inside' if in_floor else 'OUTSIDE'} floor"
        }
        if not in_floor:
            result["status"] = "FAIL"
            log(f"[VAL] ❌ {tid}: OUTSIDE floor bbox ({sx:.2f}, {sy:.2f})")

        # ── Check 2: Collision Clearance ──
        coll_ok, coll_hit = check_collision_clear(query_if, sx, sy)
        result["checks"]["collision"] = {
            "pass": coll_ok,
            "detail": f"clear" if coll_ok else f"overlap with {coll_hit}"
        }
        if not coll_ok:
            result["status"] = "FAIL"
            log(f"[VAL] ❌ {tid}: Collision at spawn — {coll_hit}")

        # ── Resolve first-phase target for FOV check ──
        first_target_xy = None
        if phases:
            tobj = phases[0]["target_object"]
            if not tobj.startswith("__human_"):
                pp = find_prim_by_factory(stage, tobj)
                if pp:
                    c = get_prim_world_center(stage, pp)
                    if c:
                        first_target_xy = c[:2]

        # ── Check 3: FOV ──
        fov_ok, fov_detail = check_fov(sx, sy, yaw, first_target_xy, level)
        result["checks"]["fov"] = {
            "pass": fov_ok,
            "target_xy": first_target_xy,
            "detail": fov_detail
        }
        if not fov_ok:
            result["status"] = "FAIL"
            log(f"[VAL] ❌ {tid}: FOV constraint failed — {fov_detail}")

        # ── Auto-fix ──
        if result["status"] == "FAIL" and FIX_MODE:
            fixed = False
            new_x, new_y, new_yaw = sx, sy, yaw

            # Strategy 1: If only FOV is wrong, just fix yaw
            if in_floor and coll_ok and not fov_ok:
                new_yaw = fix_yaw_for_fov(sx, sy, first_target_xy, level)
                result["fixes"].append(f"yaw: {yaw:.1f} → {new_yaw:.1f}")
                log(f"[VAL] 🔧 {tid}: Fixed yaw {yaw:.1f} → {new_yaw:.1f}")
                fixed = True

            # Strategy 2: If position is bad, search within floor bbox
            if not fixed and floor_bbox:
                xmin, ymin, xmax, ymax = floor_bbox
                best_dist = float('inf')
                best_pt = None
                # Grid search within floor bbox
                gx = xmin + FLOOR_INSET
                while gx <= xmax - FLOOR_INSET:
                    gy = ymin + FLOOR_INSET
                    while gy <= ymax - FLOOR_INSET:
                        c_ok, _ = check_collision_clear(query_if, gx, gy)
                        if c_ok:
                            candidate_yaw = fix_yaw_for_fov(gx, gy, first_target_xy, level)
                            fov_c, _ = check_fov(gx, gy, candidate_yaw, first_target_xy, level)
                            if fov_c:
                                d = math.hypot(gx - sx, gy - sy)
                                if d < best_dist:
                                    best_dist = d
                                    best_pt = (gx, gy, candidate_yaw)
                        gy += GRID_STEP
                    gx += GRID_STEP

                if best_pt:
                    new_x, new_y, new_yaw = best_pt
                    result["fixes"].append(
                        f"pos: ({sx:.2f},{sy:.2f}) → ({new_x:.2f},{new_y:.2f}), "
                        f"yaw: {yaw:.1f} → {new_yaw:.1f}, "
                        f"moved {best_dist:.2f}m"
                    )
                    log(f"[VAL] 🔧 {tid}: Fixed pos ({sx:.2f},{sy:.2f}) → ({new_x:.2f},{new_y:.2f}) "
                        f"yaw {yaw:.1f} → {new_yaw:.1f}")
                    fixed = True
                else:
                    log(f"[VAL] ❌ {tid}: NO VALID POSITION FOUND in floor bbox!")
                    result["fixes"].append("FAILED: no valid position found")

            if fixed:
                # Write back to task dict
                t["agent_start"] = [round(new_x, 2), round(new_y, 2)]
                t["agent_yaw"] = round(new_yaw, 1)
                result["fixed_start"] = t["agent_start"]
                result["fixed_yaw"] = t["agent_yaw"]
                result["status"] = "FIXED"
                fixes_applied += 1

        # Log pass
        if result["status"] == "PASS":
            log(f"[VAL] ✅ {tid}: All checks passed")

        all_results.append(result)

    timeline.stop()

# ─────────────────────────────────────────────────────────────────
# Summary & output
# ─────────────────────────────────────────────────────────────────
n_pass = sum(1 for r in all_results if r["status"] == "PASS")
n_fail = sum(1 for r in all_results if r["status"] == "FAIL")
n_fixed = sum(1 for r in all_results if r["status"] == "FIXED")
n_err = sum(1 for r in all_results if r["status"] == "ERROR")

log(f"\n{'='*60}")
log(f"[VAL] VALIDATION SUMMARY")
log(f"{'='*60}")
log(f"  Total: {len(all_results)}")
log(f"  PASS:  {n_pass}")
log(f"  FAIL:  {n_fail}")
log(f"  FIXED: {n_fixed}")
log(f"  ERROR: {n_err}")

if n_fail > 0:
    log(f"\n  Failed tasks:")
    for r in all_results:
        if r["status"] == "FAIL":
            checks = r.get("checks", {})
            reasons = []
            for k, v in checks.items():
                if not v.get("pass", True):
                    reasons.append(f"{k}: {v.get('detail','')}")
            log(f"    {r['task_id']}: {'; '.join(reasons)}")

# Write report
with open(REPORT_OUT, "w") as f:
    json.dump({"summary": {"pass": n_pass, "fail": n_fail, "fixed": n_fixed, "error": n_err},
               "results": all_results}, f, indent=2)
log(f"\n[VAL] Report written to {REPORT_OUT}")

# Write fixed JSON
if FIX_MODE and fixes_applied > 0:
    with open(TASKS_JSON, "w") as f:
        json.dump(task_cfg, f, indent=2)
    log(f"[VAL] ✅ Fixed {fixes_applied} tasks → written to {TASKS_JSON}")
elif FIX_MODE:
    log(f"[VAL] No fixes needed")

sim_app.close()
