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
# TASKS_JSON / outputs overridable via env so the SAME validator (ALL its checks) can
# gate generate_tasks.py output, not just full_benchmark_0601.json. Defaults unchanged.
TASKS_JSON = os.environ.get("VAL_TASKS_JSON", os.path.join(BENCH_DIR, "full_benchmark_0601.json"))
REPORT_OUT = os.environ.get("VAL_REPORT_OUT", os.path.join(BENCH_DIR, "spawn_validation_report.json"))
# When set, write ONLY valid (PASS/FIXED) tasks here (drops FAIL/ERROR) — used by the
# generation pipeline. When unset, the legacy in-place fix-write behavior is kept.
VALID_OUT  = os.environ.get("VAL_VALID_OUT", "")

FIX_MODE = "--fix" in sys.argv or os.environ.get("VAL_FIX") == "1"

# ── Floodfill reachability (ported from validate_full_spawns.py) ──
# validate_all_spawns historically gated spawn QUALITY (floor/clear/FOV/LOS) but NOT a
# navigable PATH to the target. Without it a spawn can SEE a target through a gap too
# narrow to walk (case003). Added as an EXTRA gate — no existing check is removed.
GRID_RES = 0.25        # metres per floodfill cell
MAX_CELLS = 6000       # BFS budget
AGENT_RADIUS = 0.40    # sphere-sweep radius for walkability
_4DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1)]

# ── Geometry / FOV constants ──
FLOOR_INSET   = 0.3   # metres inward from floor bbox edges (avoid wall-hugging)
SWEEP_RADIUS  = 0.40  # PhysX sweep sphere radius (matches bench_runner)
SWEEP_DIST    = 0.05  # sweep travel distance
FOV_HALF_DEG  = 45.0  # ±45° horizontal FOV cone for visibility check
VFOV_HALF_DEG = 29.0  # ±29° vertical FOV half (960x540, focal 17mm / aperture 19.2mm)
CAM_HEIGHT    = 1.58  # camera eye height in metres (matches bench_runner EYE_H)
CAM_PITCH_DEG = -10.0 # initial camera pitch in degrees (negative = looking down)
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
from bench_helpers import discover_scene_files, find_prim_by_factory, get_prim_world_center, resolve_target

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

def get_prim_half_extent_xy(stage, prim_path):
    """Get the XY half-extent (max of width/2, depth/2) of a prim's world bbox.
    Returns 0.0 if bbox cannot be computed (degenerate/invisible prim)."""
    try:
        from pxr import UsdGeom, Usd
        prim = stage.GetPrimAtPath(prim_path)
        if not prim.IsValid():
            return 0.0
        imageable = UsdGeom.Imageable(prim)
        bound = imageable.ComputeWorldBound(Usd.TimeCode.Default(), "default")
        box = bound.GetBox()
        mn, mx = box.GetMin(), box.GetMax()
        w = mx[0] - mn[0]
        d = mx[1] - mn[1]
        if w < 0 or w > 100:  # degenerate bbox guard
            return 0.0
        return max(w, d) / 2.0
    except Exception:
        return 0.0

def dist_to_edge(agent_x, agent_y, target_x, target_y, half_extent):
    """Compute distance from agent to the nearest surface of the target.
    dist_to_edge = dist_to_center - half_extent, clamped to 0."""
    center_dist = math.hypot(agent_x - target_x, agent_y - target_y)
    return max(0.0, center_dist - half_extent)

def check_line_of_sight(query_if, sx, sy, target_xy, target_z=None,
                        target_prim_path=None, target_factory=None):
    """Multi-ray LOS: cast N rays from agent camera to a spread of points
    around the target. PASS if ANY ray reaches the target unblocked.
    This is an existential check (∃), not universal (∀) — reduces false kills
    from a single unlucky ray hitting shelf edges etc.

    IMPORTANT: If a ray hits the target prim itself (or a prim belonging to the
    same factory), that counts as "can see the target", NOT as an obstruction.
    PhysX collider paths may differ from USD prim paths, so we match by both
    full prim path containment and factory name prefix.

    Returns (pass, detail_string)."""
    if target_xy is None:
        return True, "no_target"
    tx, ty = target_xy
    tz = target_z if target_z is not None else 1.0
    origin_z = CAM_HEIGHT  # camera height (must match bench_runner EYE_H)

    # Build matchers: prim path fragment + factory name for target identification
    target_path_lower = target_prim_path.lower() if target_prim_path else ""
    # Extract the key factory token e.g. "sofafactory" from "SofaFactory"
    target_factory_lower = target_factory.lower() if target_factory else ""

    SPREAD = 0.3  # metres — covers visual extent of small objects
    sample_targets = [
        (tx, ty, tz),
        (tx + SPREAD, ty, tz),
        (tx - SPREAD, ty, tz),
        (tx, ty + SPREAD, tz),
        (tx, ty - SPREAD, tz),
    ]

    blocked_details = []
    for stx, sty, stz in sample_targets:
        dx, dy, dz = stx - sx, sty - sy, stz - origin_z
        dist = math.sqrt(dx*dx + dy*dy + dz*dz)
        if dist < 0.1:
            return True, "agent_at_target"
        nx, ny, nz = dx/dist, dy/dist, dz/dist
        h = query_if.raycast_closest(
            carb.Float3(sx, sy, origin_z),
            carb.Float3(nx, ny, nz), dist)
        if not h["hit"]:
            return True, f"clear (no hit, dist={dist:.2f}m)"
        hit_path = (h.get("rigidBody") or h.get("collider") or "").lower()
        hit_dist = float(h.get("distance", 0))
        # Walkable surface hit → not blocking
        if any(w in hit_path for w in WALKABLE):
            return True, f"clear (walkable hit: {hit_path.split('/')[-1][:40]})"
        # Hit is the TARGET ITSELF → can see target, not an obstruction!
        # Match by: (a) prim path containment, or (b) same factory name
        is_target_hit = False
        if target_path_lower and target_path_lower in hit_path:
            is_target_hit = True
        elif target_factory_lower and target_factory_lower in hit_path:
            is_target_hit = True
        if is_target_hit:
            return True, f"clear (hit target itself: {hit_path.split('/')[-1][:40]} @{hit_dist:.2f}m)"
        # Hit is beyond target → not blocking
        if hit_dist >= dist - 0.3:
            return True, f"clear (hit beyond target, hit_d={hit_dist:.2f} vs tgt_d={dist:.2f})"
        blocked_details.append(f"{hit_path.split('/')[-1][:40]} @{hit_dist:.2f}m")

    # ALL rays blocked
    return False, f"ALL {len(sample_targets)} rays blocked: {'; '.join(blocked_details[:3])}"

def check_fov(sx, sy, yaw_deg, target_xy, level, target_z=None):
    """
    L1/L3 → target MUST be within horizontal AND vertical FOV (visible)
    L2/L4 → target must NOT be within horizontal FOV (hidden)
    Vertical FOV is only enforced for L1/L3 (visibility requirement).
    Returns (pass, detail_string).
    """
    if target_xy is None:
        return True, "no_target"
    tx, ty = target_xy
    # ── Horizontal check ──
    angle_to_target = math.degrees(math.atan2(ty - sy, tx - sx))
    rel = ((angle_to_target - yaw_deg + 180) % 360) - 180
    in_hfov = abs(rel) <= FOV_HALF_DEG
    lnum = int(level.replace("L",""))
    want_visible = (lnum % 2 == 1)  # L1, L3 → odd → visible
    if not want_visible:
        # L2/L4: just check target is NOT in horizontal FOV
        ok = not in_hfov
        detail = f"rel_angle={rel:.1f}° in_hfov={in_hfov} want_visible=False"
        return ok, detail
    # ── L1/L3: target must be in BOTH horizontal and vertical FOV ──
    if not in_hfov:
        return False, f"HFOV fail: rel_angle={rel:.1f}° > ±{FOV_HALF_DEG}°"
    # Vertical check: is target within the camera's vertical viewport?
    tz = target_z if target_z is not None else 1.0  # default to mid-height
    horiz_dist = math.hypot(tx - sx, ty - sy)
    if horiz_dist < 0.1:
        return True, "agent_at_target"
    dz = tz - CAM_HEIGHT
    pitch_to_target = math.degrees(math.atan2(dz, horiz_dist))
    # The camera has initial pitch CAM_PITCH_DEG. The relative vertical angle
    # from the camera center is: pitch_to_target - CAM_PITCH_DEG
    rel_vpitch = pitch_to_target - CAM_PITCH_DEG
    in_vfov = abs(rel_vpitch) <= VFOV_HALF_DEG
    detail = (f"hfov_rel={rel:.1f}° vfov_pitch_to_tgt={pitch_to_target:.1f}° "
              f"cam_pitch={CAM_PITCH_DEG}° rel_vpitch={rel_vpitch:.1f}° "
              f"in_hfov={in_hfov} in_vfov={in_vfov}")
    ok = in_hfov and in_vfov
    if not ok:
        detail = f"VFOV fail: {detail}"
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

    # ── Floodfill reachable set for this scene (walkable cells) ──
    def _to_grid(x, y): return (round(x / GRID_RES), round(y / GRID_RES))
    def _from_grid(gx, gy): return (gx * GRID_RES, gy * GRID_RES)
    def _flood_fill(seedx, seedy):
        start = _to_grid(seedx, seedy); visited = {start}; queue = [start]; idx = 0
        while idx < len(queue) and len(visited) < MAX_CELLS:
            gx, gy = queue[idx]; idx += 1
            wx, wy = _from_grid(gx, gy)
            for ddx, ddy in _4DIRS:
                ngx, ngy = gx + ddx, gy + ddy
                if (ngx, ngy) in visited:
                    continue
                # Two-height sweep (body + head), aligned with validate_and_fix_spawns:
                # a single 0.5m sweep would walk through gaps blocked at head height.
                blocked = False
                for sz in (0.5, 1.0):
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
    def _reachable(rset, tx, ty, tol=2):
        tg = _to_grid(tx, ty)
        for dx in range(-tol, tol + 1):
            for dy in range(-tol, tol + 1):
                if (tg[0] + dx, tg[1] + dy) in rset:
                    return True
        return False
    # Seed from a walkable in-room point (first clear, in-polygon cell of the floor bbox).
    seed = None
    reachable = set()
    if floor_bbox:
        bx0, by0, bx1, by1 = floor_bbox
        gx = bx0 + FLOOR_INSET
        while gx <= bx1 - FLOOR_INSET and seed is None:
            gy = by0 + FLOOR_INSET
            while gy <= by1 - FLOOR_INSET:
                if check_in_floor(gx, gy, floor_poly):
                    ok, _ = check_collision_clear(query_if, gx, gy)
                    if ok:
                        seed = (gx, gy); break
                gy += GRID_STEP
            gx += GRID_STEP
        if seed:
            reachable = _flood_fill(seed[0], seed[1])
    log(f"[VAL] Reachable cells: {len(reachable)} (seed={seed})")

    # ── Per-task validation ──
    for t in scene_task_list:
        tid   = t["id"]
        level = t["level"]
        _astart = t.get("agent_start")
        force_gen = _astart is None  # generate_tasks output has no spawn yet
        if force_gen:
            sx, sy = (seed if seed else (0.0, 0.0))
        else:
            sx, sy = _astart
        yaw    = (t.get("agent_yaw") or 0.0)
        phases = t.get("phases", [])

        result = {
            "task_id": tid, "level": level,
            "original_start": [sx, sy], "original_yaw": yaw,
            "checks": {}, "status": "PASS", "fixes": []
        }
        if force_gen:
            # No spawn provided → force the auto-fix grid search to place a valid,
            # reachable one (all quality checks below still apply to the result).
            result["status"] = "FAIL"
            result["checks"]["needs_spawn"] = {"pass": False, "detail": "agent_start=None (generate)"}

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
        first_target_z = None
        first_target_prim_path = None
        first_target_factory = None
        first_target_half_ext = 0.0  # XY half-extent for edge-distance
        if phases:
            tobj = phases[0]["target_object"]
            if not tobj.startswith("__human_"):
                first_target_factory = tobj
                # Shared resolver — the SAME prim the runner will use (real Obj_
                # geometry, not the inactive __spawn_asset_ over). This is what makes
                # the validator's guarantee equal to what the runner executes.
                res = resolve_target(stage, phases[0])
                if res:
                    first_target_prim_path = res["prim_path"]
                    first_target_half_ext = res["half_extent_xy"]
                else:
                    result["status"] = "FAIL"
                    result["checks"]["target_resolve"] = {
                        "pass": False,
                        "detail": f"unresolvable target {tobj} prim={phases[0].get('target_prim','')}"}
                    log(f"[VAL] ❌ {tid}: unresolvable target {tobj} — would FAIL in runner")
                # Use place_at if present (L3 pick-up tasks relocate the
                # object at runtime; validator must check the ACTUAL position)
                pa = phases[0].get("place_at")
                if pa and pa is not None:
                    first_target_xy = (pa[0], pa[1])
                    first_target_z = pa[2] if len(pa) > 2 else None
                    log(f"[VAL]   Using place_at ({pa[0]:.1f},{pa[1]:.1f},{pa[2] if len(pa)>2 else '?'}) as target position")
                elif res:
                    c = res["center"]
                    first_target_xy = c[:2]
                    first_target_z = c[2] if len(c) > 2 else None
                if first_target_half_ext > 0:
                    log(f"[VAL]   Target half-extent={first_target_half_ext:.2f}m (edge-distance mode)")

        # ── Check 3: FOV ──
        fov_ok, fov_detail = check_fov(sx, sy, yaw, first_target_xy, level, target_z=first_target_z)
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

        # ── Check 4: Target in Room (audit — same room region) ──
        target_in_room = True
        if first_target_xy and floor_poly:
            target_in_room = point_in_polygon_xy(
                first_target_xy[0], first_target_xy[1], floor_poly)
            agent_to_target = math.hypot(sx - first_target_xy[0], sy - first_target_xy[1])
            result["checks"]["target_in_room"] = {
                "pass": target_in_room,
                "target_xy": list(first_target_xy),
                "target_prim": first_target_prim_path,
                "agent_to_target_dist": round(agent_to_target, 2),
                "detail": (f"target at ({first_target_xy[0]:.2f},{first_target_xy[1]:.2f}) "
                           f"{'inside' if target_in_room else 'OUTSIDE'} floor polygon, "
                           f"dist={agent_to_target:.2f}m")
            }
            if not target_in_room:
                log(f"[VAL] ⚠ {tid}: Target OUTSIDE floor polygon at "
                    f"({first_target_xy[0]:.2f},{first_target_xy[1]:.2f}) — "
                    f"may be in different room region (dist={agent_to_target:.2f}m)")

        # ── Check 5: Line of Sight (L1/L3 only) ──
        lnum = int(level.replace("L",""))
        want_visible = (lnum % 2 == 1)
        los_ok = True
        if want_visible and first_target_xy:
            los_ok, los_detail = check_line_of_sight(
                query_if, sx, sy, first_target_xy, first_target_z,
                target_prim_path=first_target_prim_path,
                target_factory=first_target_factory)
            result["checks"]["line_of_sight"] = {
                "pass": los_ok,
                "detail": los_detail
            }
            if not los_ok:
                result["status"] = "FAIL"
                log(f"[VAL] ❌ {tid}: LOS blocked — {los_detail}")

        # ── Check 5b: Reachability — target must have a navigable path (floodfill) ──
        # (The gate validate_all_spawns lacked; LOS alone passed case003's narrow gap.)
        tgt_reach = True
        if first_target_xy and reachable:
            rtol = max(2, math.ceil(phases[0].get("radius", 0.5) / GRID_RES)) if phases else 4
            tgt_reach = _reachable(reachable, first_target_xy[0], first_target_xy[1], tol=rtol)
            result["checks"]["target_reachable"] = {
                "pass": tgt_reach,
                "detail": f"target {'reachable' if tgt_reach else 'UNREACHABLE'} via floodfill (tol={rtol})"}
            if not tgt_reach:
                result["status"] = "FAIL"
                log(f"[VAL] ❌ {tid}: target UNREACHABLE — no navigable path within radius")

        # ── Check 6: Spawn-win (agent already within success radius) ──
        # Uses edge-distance: dist_to_surface = dist_to_center - half_extent
        tgt_radius = phases[0]["radius"] if phases else 0
        spawn_win = False
        if first_target_xy and tgt_radius > 0:
            agent_to_center = math.hypot(sx - first_target_xy[0], sy - first_target_xy[1])
            edge_dist = dist_to_edge(sx, sy, first_target_xy[0], first_target_xy[1], first_target_half_ext)
            if edge_dist <= tgt_radius:
                spawn_win = True
                result["status"] = "FAIL"
                result["checks"]["min_dist"] = {
                    "pass": False,
                    "agent_to_center": round(agent_to_center, 2),
                    "edge_dist": round(edge_dist, 2),
                    "half_extent": round(first_target_half_ext, 2),
                    "required": tgt_radius,
                    "detail": f"SPAWN_WIN: edge_dist={edge_dist:.2f}m (center={agent_to_center:.2f}-half={first_target_half_ext:.2f}) <= radius={tgt_radius:.1f}m"
                }
                log(f"[VAL] ❌ {tid}: SPAWN_WIN — edge_dist={edge_dist:.2f}m <= radius={tgt_radius:.1f}m")
            else:
                result["checks"]["min_dist"] = {
                    "pass": True,
                    "agent_to_center": round(agent_to_center, 2),
                    "edge_dist": round(edge_dist, 2),
                    "half_extent": round(first_target_half_ext, 2),
                    "required": tgt_radius,
                    "detail": f"edge_dist={edge_dist:.2f}m > radius={tgt_radius:.1f}m"
                }

        # ── Auto-fix ── (skip entirely if the TARGET is unreachable — moving the spawn
        # cannot fix an unreachable target, so such tasks stay FAIL and get dropped.)
        if result["status"] == "FAIL" and FIX_MODE and tgt_reach:
            fixed = False
            new_x, new_y, new_yaw = sx, sy, yaw

            # Strategy 1: If only FOV/clearance/LOS is wrong AND not spawn_win, just fix yaw
            if in_floor and coll_ok and not spawn_win and (not fov_ok or not fwd_ok or not los_ok):
                candidate_yaw = fix_yaw_for_fov(query_if, sx, sy, first_target_xy, level)
                f_fov, _ = check_fov(sx, sy, candidate_yaw, first_target_xy, level, target_z=first_target_z)
                f_fwd = check_forward_clearance(query_if, sx, sy, candidate_yaw, min_dist=1.2)
                # For L1/L3, also check LOS at the candidate yaw (position unchanged)
                f_los = True
                if want_visible and first_target_xy:
                    f_los, _ = check_line_of_sight(
                        query_if, sx, sy, first_target_xy, first_target_z,
                        target_prim_path=first_target_prim_path,
                        target_factory=first_target_factory)
                if f_fov and f_fwd and f_los:
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
                        # Reachability: candidate spawn must be in the walkable component.
                        if reachable and not _reachable(reachable, gx, gy, tol=1):
                            gy += GRID_STEP
                            continue
                        c_ok, _ = check_collision_clear(query_if, gx, gy)
                        if c_ok:
                            candidate_yaw = fix_yaw_for_fov(query_if, gx, gy, first_target_xy, level)
                            fov_c, _ = check_fov(gx, gy, candidate_yaw, first_target_xy, level, target_z=first_target_z)
                            fwd_c = check_forward_clearance(query_if, gx, gy, candidate_yaw, min_dist=1.2)
                            # For L1/L3, also require LOS from this candidate
                            los_c = True
                            if want_visible and first_target_xy:
                                los_c, _ = check_line_of_sight(
                                    query_if, gx, gy, first_target_xy, first_target_z,
                                    target_prim_path=first_target_prim_path,
                                    target_factory=first_target_factory)
                            if fov_c and fwd_c and los_c:
                                # Check min distance to target (prevent spawn_win) — edge-based
                                d_to_tgt_edge = dist_to_edge(gx, gy, first_target_xy[0], first_target_xy[1], first_target_half_ext) if first_target_xy else 999
                                if d_to_tgt_edge <= tgt_radius:
                                    gy += GRID_STEP
                                    continue
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

# Write valid-only tasks (PASS/FIXED) for the generation pipeline — drops FAIL/ERROR.
if VALID_OUT:
    valid_ids = {r["task_id"] for r in all_results if r["status"] in ("PASS", "FIXED")}
    kept = [t for t in tasks if t["id"] in valid_ids]
    with open(VALID_OUT, "w") as f:
        json.dump({"tasks": kept}, f, indent=2)
    log(f"[VAL] ✅ Wrote {len(kept)}/{len(tasks)} valid tasks → {VALID_OUT}")

# Write fixed JSON (legacy in-place behavior; only when not using VALID_OUT)
if not VALID_OUT and FIX_MODE and fixes_applied > 0:
    with open(TASKS_JSON, "w") as f:
        json.dump(task_cfg, f, indent=2)
    log(f"[VAL] ✅ Fixed {fixes_applied} tasks → written to {TASKS_JSON}")
elif not VALID_OUT and FIX_MODE:
    log(f"[VAL] No fixes needed")

sim_app.close()
