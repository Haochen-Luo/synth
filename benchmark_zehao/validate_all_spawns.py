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
WALKABLE      = ("floor", "ground", "rug", "blanket", "towel", "mat")

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

# ── 2D Geometry utilities (ported from extract_bev_annotation_data_blender.py) ──

def polygon_area(poly):
    """Signed area of a 2D polygon (positive = CCW)."""
    n = len(poly)
    if n < 3:
        return 0.0
    acc = 0.0
    for i in range(n):
        j = (i + 1) % n
        acc += poly[i][0] * poly[j][1] - poly[j][0] * poly[i][1]
    return acc * 0.5

def convex_hull(points):
    """Andrew's monotone chain convex hull. Returns CCW polygon."""
    uniq = sorted({(round(float(p[0]), 5), round(float(p[1]), 5)) for p in points})
    if len(uniq) <= 2:
        return [[float(x), float(y)] for x, y in uniq]
    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    lower = []
    for p in uniq:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(uniq):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [[float(x), float(y)] for x, y in lower[:-1] + upper[:-1]]

def order_boundary_loop(edges, coords):
    if not edges:
        return []
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    start = min(adj, key=lambda idx: (coords[idx][0], coords[idx][1]))
    loop = [start]
    prev = None
    cur = start
    for _ in range(max(4, len(edges) + 4)):
        nxts = [n for n in adj.get(cur, []) if n != prev]
        if not nxts:
            break
        if len(nxts) > 1:
            nxts.sort(key=lambda n: (coords[n][0], coords[n][1]))
        nxt = nxts[0]
        if nxt == start:
            break
        loop.append(nxt)
        prev, cur = cur, nxt
    poly = [coords[idx] for idx in loop]
    if polygon_area(poly) < 0:
        poly.reverse()
    return poly

def boundary_edge_components(edges):
    adj = {}
    for edge in edges:
        a, b = edge
        adj.setdefault(a, []).append(edge)
        adj.setdefault(b, []).append(edge)
    visited = set()
    components = []
    for edge in edges:
        key = (min(edge[0], edge[1]), max(edge[0], edge[1]))
        if key in visited:
            continue
        stack = [edge]
        comp = []
        while stack:
            cur = stack.pop()
            cur_key = (min(cur[0], cur[1]), max(cur[0], cur[1]))
            if cur_key in visited:
                continue
            visited.add(cur_key)
            comp.append(cur)
            for v in cur:
                for nxt in adj.get(v, []):
                    nxt_key = (min(nxt[0], nxt[1]), max(nxt[0], nxt[1]))
                    if nxt_key not in visited:
                        stack.append(nxt)
        if comp:
            components.append(comp)
    return components

def candidate_boundary_loops(edges, coords):
    loops = []
    for comp in boundary_edge_components(edges):
        loop = order_boundary_loop(comp, coords)
        if len(loop) >= 3 and abs(polygon_area(loop)) > 1e-5:
            loops.append(loop)
    loops.sort(key=lambda poly: -abs(polygon_area(poly)))
    return loops

def point_in_polygon_xy(px, py, poly):
    """Ray-casting point-in-polygon test."""
    if len(poly) < 3:
        return True
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = float(poly[i][0]), float(poly[i][1])
        xj, yj = float(poly[j][0]), float(poly[j][1])
        if (yi > py) != (yj > py):
            x_cross = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < x_cross:
                inside = not inside
        j = i
    return inside

# ── Floor detection (replicates BEV pipeline room selection logic) ──

ROOM_TOKENS = ("living_room", "living-room", "dining_room", "dining-room",
               "bedroom", "bathroom", "kitchen", "hallway")
ROOM_PRIORITY = ("living_room", "living-room", "dining_room", "dining-room",
                 "bedroom", "kitchen", "hallway", "bathroom")

def find_floor_polygon(stage):
    """Extract the primary room's floor polygon from USD mesh vertices.
    Returns (polygon_2d, bbox_tuple) or (None, None).
    polygon_2d is a list of [x,y] points forming the convex hull.
    bbox_tuple is (xmin, ymin, xmax, ymax) for reference.
    """
    # Collect all floor Mesh prims grouped by room key
    rooms = {}  # room_key -> {"floor_meshes": [...], "all_xy": [...]}
    for prim in stage.Traverse():
        name_lower = prim.GetName().lower()
        path_lower = str(prim.GetPath()).lower()
        # Must be a room structure prim
        if not any(tok in path_lower for tok in ROOM_TOKENS):
            continue
        # Must be a floor part
        if "floor" not in name_lower:
            continue
        # Must be active & visible
        if not prim.IsActive():
            continue
        # Get room key from parent path
        # e.g. /World/Env/living_room_0_0_floor/living_room_0_0_floor
        # room key = "living_room_0_0"
        parts = prim.GetPath().pathString.split("/")
        room_key = None
        for part in parts:
            pl = part.lower()
            if any(tok in pl for tok in ROOM_TOKENS) and "floor" in pl:
                room_key = pl.replace("_floor", "")
                break
        if not room_key:
            continue

        # Try to read mesh vertices
        mesh = UsdGeom.Mesh(prim)
        if not mesh:
            continue
        points_attr = mesh.GetPointsAttr()
        if not points_attr or not points_attr.HasValue():
            # If this is an Xform, look for child Mesh
            continue
        pts = points_attr.Get()
        if not pts or len(pts) == 0:
            continue

        # Transform vertices to world space
        xf_cache = UsdGeom.XformCache()
        world_xf = xf_cache.GetLocalToWorldTransform(prim)
        room = rooms.setdefault(room_key, {"xy": [], "edges": [], "coords": {}, "kind": ""})
        
        # Read faces to extract precise boundary edges
        face_counts_attr = mesh.GetFaceVertexCountsAttr()
        face_indices_attr = mesh.GetFaceVertexIndicesAttr()
        if face_counts_attr and face_counts_attr.HasValue() and face_indices_attr and face_indices_attr.HasValue():
            face_counts = face_counts_attr.Get()
            face_indices = face_indices_attr.Get()
            offset = 0
            edge_counts = {}
            for count in face_counts:
                face_verts = face_indices[offset:offset+count]
                for i in range(count):
                    a = face_verts[i]
                    b = face_verts[(i + 1) % count]
                    # Local index to global offset for this specific room key
                    ga = a + len(room["coords"])
                    gb = b + len(room["coords"])
                    key = (min(ga, gb), max(ga, gb))
                    edge_counts[key] = edge_counts.get(key, 0) + 1
                offset += count
            
            # Map new vertices to coords dict
            base_idx = len(room["coords"])
            for i, p in enumerate(pts):
                wp = world_xf.Transform(Gf.Vec3d(float(p[0]), float(p[1]), float(p[2])))
                xy = [float(wp[0]), float(wp[1])]
                room["coords"][base_idx + i] = xy
                room["xy"].append(xy)
            
            # Store edges that appear exactly once (boundary edges)
            for edge, count in edge_counts.items():
                if count == 1:
                    room["edges"].append(edge)
        else:
            # Fallback if no faces: just add points for convex hull
            for p in pts:
                wp = world_xf.Transform(Gf.Vec3d(float(p[0]), float(p[1]), float(p[2])))
                room["xy"].append([float(wp[0]), float(wp[1])])
        # Determine room kind for priority
        for tok in ROOM_PRIORITY:
            if tok.replace("-", "_") in room_key or tok in room_key:
                room["kind"] = tok
                break

    if not rooms:
        log("[VAL]   No floor mesh vertices found, falling back to Xform bbox")
        return _find_floor_bbox_fallback(stage)

    # Select primary room (highest priority, largest area)
    best_room = None
    best_priority = len(ROOM_PRIORITY)
    best_area = 0
    for rk, rv in rooms.items():
        if len(rv["xy"]) < 3:
            continue
        hull = convex_hull(rv["xy"])
        area = abs(polygon_area(hull))
        kind = rv["kind"]
        priority = len(ROOM_PRIORITY)
        for i, tok in enumerate(ROOM_PRIORITY):
            if tok.replace("-", "_") in rk or tok in rk:
                priority = i
                break
        if priority < best_priority or (priority == best_priority and area > best_area):
            best_priority = priority
            best_area = area
            best_room = rv

    if not best_room or len(best_room["xy"]) < 3:
        log("[VAL]   No valid floor polygon found, falling back to Xform bbox")
        return _find_floor_bbox_fallback(stage)

    # Attempt to build precise concave boundary loop from edges
    boundary_poly = None
    if best_room["edges"]:
        loops = candidate_boundary_loops(best_room["edges"], best_room["coords"])
        if loops:
            boundary_poly = loops[0]
            log(f"[VAL]   Using precise concave boundary: {len(boundary_poly)} vertices")

    if not boundary_poly:
        boundary_poly = convex_hull(best_room["xy"])
        log(f"[VAL]   Using convex hull fallback: {len(boundary_poly)} vertices")

    xs = [p[0] for p in boundary_poly]
    ys = [p[1] for p in boundary_poly]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    log(f"[VAL]   Floor polygon area={abs(polygon_area(boundary_poly)):.1f}m²")
    return boundary_poly, bbox

def _find_floor_bbox_fallback(stage):
    """Fallback: use Xform bbox when mesh vertices aren't readable."""
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
                bbox = (float(mn[0]), float(mn[1]), float(mx[0]), float(mx[1]))
                # Create rectangle polygon from bbox
                poly = [
                    [bbox[0], bbox[1]], [bbox[2], bbox[1]],
                    [bbox[2], bbox[3]], [bbox[0], bbox[3]]
                ]
                return poly, bbox
    return None, None

def check_in_floor(x, y, floor_poly):
    """Check if (x,y) is within the floor polygon."""
    if floor_poly is None:
        return True  # can't check, assume OK
    return point_in_polygon_xy(x, y, floor_poly)

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

def check_forward_clearance(query_if, sx, sy, yaw_deg, min_dist=1.2):
    """Raycast forward to ensure the agent isn't staring point-blank at a wall."""
    rad = math.radians(yaw_deg)
    dx = math.cos(rad)
    dy = math.sin(rad)
    # Cast ray from approx camera height (z=1.0)
    origin = carb.Float3(sx, sy, 1.0)
    dir_vec = carb.Float3(dx, dy, 0.0)
    h = query_if.raycast_closest(origin, dir_vec, min_dist)
    if h["hit"]:
        hit_path = h["rigidBody"] or h["collider"]
        hit_name = hit_path.split("/")[-1].lower()
        if any(w in hit_name for w in WALKABLE):
            return True
        return False
    return True

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

def fix_yaw_for_fov(query_if, sx, sy, target_xy, level):
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
        # face AWAY from target (try multiple angles outside FOV cone)
        for offset in [180, 150, 210, 120, 240, 90, 270]:
            test_yaw = ((angle_to_target + offset + 180) % 360) - 180
            if check_forward_clearance(query_if, sx, sy, test_yaw, min_dist=1.2):
                return test_yaw
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

    # ── Floor polygon (precise room boundary) ──
    floor_poly, floor_bbox = find_floor_polygon(stage)
    if floor_bbox:
        log(f"[VAL] Floor bbox: x=[{floor_bbox[0]:.2f}, {floor_bbox[2]:.2f}] "
            f"y=[{floor_bbox[1]:.2f}, {floor_bbox[3]:.2f}]")
    else:
        log(f"[VAL] ⚠ No floor polygon found — skipping floor check")

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

        # ── Check 1: Floor Polygon ──
        in_floor = check_in_floor(sx, sy, floor_poly)
        result["checks"]["floor_bbox"] = {
            "pass": in_floor,
            "floor_bbox": list(floor_bbox) if floor_bbox else None,
            "detail": f"({sx:.2f}, {sy:.2f}) {'inside' if in_floor else 'OUTSIDE'} floor polygon"
        }
        if not in_floor:
            result["status"] = "FAIL"
            log(f"[VAL] ❌ {tid}: OUTSIDE floor polygon ({sx:.2f}, {sy:.2f})")

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

        fwd_ok = check_forward_clearance(query_if, sx, sy, yaw, min_dist=1.2)
        if not fwd_ok:
            result["status"] = "FAIL"
            log(f"[VAL] ❌ {tid}: Forward clearance failed (staring at a wall)")

        # ── Auto-fix ──
        if result["status"] == "FAIL" and FIX_MODE:
            fixed = False
            new_x, new_y, new_yaw = sx, sy, yaw

            # Strategy 1: If only FOV/clearance is wrong, just fix yaw
            if in_floor and coll_ok and (not fov_ok or not fwd_ok):
                candidate_yaw = fix_yaw_for_fov(query_if, sx, sy, first_target_xy, level)
                f_fov, _ = check_fov(sx, sy, candidate_yaw, first_target_xy, level)
                f_fwd = check_forward_clearance(query_if, sx, sy, candidate_yaw, min_dist=1.2)
                if f_fov and f_fwd:
                    new_yaw = candidate_yaw
                    result["fixes"].append(f"yaw: {yaw:.1f} → {new_yaw:.1f}")
                    log(f"[VAL] 🔧 {tid}: Fixed yaw {yaw:.1f} → {new_yaw:.1f}")
                    fixed = True

            # Strategy 2: If position is bad or yaw couldn't be fixed, search within floor bbox
            if not fixed and floor_bbox:
                xmin, ymin, xmax, ymax = floor_bbox
                best_dist = float('inf')
                best_pt = None
                # Grid search within floor bbox, filtered by polygon
                gx = xmin + FLOOR_INSET
                while gx <= xmax - FLOOR_INSET:
                    gy = ymin + FLOOR_INSET
                    while gy <= ymax - FLOOR_INSET:
                        if not check_in_floor(gx, gy, floor_poly):
                            gy += GRID_STEP
                            continue
                        c_ok, _ = check_collision_clear(query_if, gx, gy)
                        if c_ok:
                            candidate_yaw = fix_yaw_for_fov(query_if, gx, gy, first_target_xy, level)
                            fov_c, _ = check_fov(gx, gy, candidate_yaw, first_target_xy, level)
                            fwd_c = check_forward_clearance(query_if, gx, gy, candidate_yaw, min_dist=1.2)
                            if fov_c and fwd_c:
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
