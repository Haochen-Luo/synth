#!/usr/bin/env python3
"""full_task_gen.py — 4DSynth-Nav task generator with geometric visibility.

Replaces the implicit "face away == hidden" heuristic of gen_tasks.py with a
real geometric test, run inside Isaac Sim:

  * L1 / L3  (target VISIBLE)  : the target's bounding box must be inside the
    camera FOV frustum AND a sightline to it must not be blocked by a wall.
  * L2 / L4  (target HIDDEN)   : the target's bounding box must be PROVABLY not
    visible — every sampled bbox point is either outside the FOV frustum, or
    inside it but blocked by a wall. The frustum test is what makes "hidden" a
    hard guarantee: if the frustum never even covers the target, it is
    absolutely off-screen.

NOTE on physics: the PhysX collision scene is only built once the timeline is
playing — raycast_closest hits nothing (not even walls) without timeline.play().
Furniture and walls both carry colliders. The occlusion ray treats only
STRUCTURAL prims (wall/door/window/ceiling) as blockers: a furniture prim in
front of the target means partial occlusion, which still leaves the target
visible, so it is not counted as a blocker. The FOV frustum is the primary
visibility gate; the wall-ray additionally rules out "seeing through a wall".

Start poses are sampled with a dynamic wall margin (15% of room size, clamped
to [1.5m, 2.0m]) so the agent never spawns face-planted into a wall — the real
reason the old plain-180° flip sometimes failed.

Tasks that cannot be made to satisfy their visibility class after MAX_ATTEMPTS
resamples are MARKED FAILED and excluded. A full report (per task: success /
failure + reason) is written to the workspace docs folder.

Run inside the vlm-jupyter container:
  SCENE_DIR=/abs/path/to/native_caseNN_..._scene \
  /isaac-sim/python.sh full_task_gen.py

Outputs:
  benchmark_tasks_fullgen.json                — generated + verified tasks
  <workspace>/docs/task_gen_report_<scene>.md — human-readable report
"""
import sys, os, json, math, random, time, datetime, traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bench_helpers import discover_scene_files, find_prim_by_factory, get_prim_world_center
from semantic_classes import semantic_class_of

# ── Config ──
# Reports live with the benchmark itself (Puppeteer/zehao_task), NOT in the
# unrelated ParadigmShiftEvolve project.
SCENE_DIR = os.environ.get("SCENE_DIR", "")
OUT_JSON = os.environ.get("OUT_JSON", os.path.join(SCRIPT_DIR, "benchmark_tasks_fullgen.json"))
REPORT_DIR = os.environ.get("REPORT_DIR", os.path.join(SCRIPT_DIR, "task_gen_reports"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "120"))
SEED = int(os.environ.get("SEED", "42"))

# Camera: FPV uses focal 17mm / aperture 34mm -> horizontal FOV 90 deg.
# Render is 1920x1080 -> vertical FOV ~58.7 deg. Eye height 1.58m, pitch -10.
H_FOV_DEG = 90.0
V_FOV_DEG = 2.0 * math.degrees(math.atan(math.tan(math.radians(H_FOV_DEG / 2)) * 1080.0 / 1920.0))
EYE_H = 1.58
CAM_PITCH_DEG = -10.0
AGENT_RADIUS = 0.40

if not SCENE_DIR:
    print("ERROR: SCENE_DIR env var is required"); sys.exit(1)
random.seed(SEED)
scene_basename = os.path.basename(SCENE_DIR.rstrip("/"))
scene_key = scene_basename.replace("native_", "").replace("_full_physics_scene", "")
scene_id = scene_key.split("_")[0]  # caseNN
room = "living" if "living" in scene_basename else "dining"


def log(m): print(m, flush=True)


# ── Geometry helpers ─────────────────────────────────────────────────────────
def bbox_sample_points(center, size):
    """Return representative points of an axis-aligned bbox: the 8 corners plus
    the center. Used to test whether ANY part of the target is visible."""
    cx, cy, cz = center
    hx, hy, hz = size[0] / 2.0, size[1] / 2.0, size[2] / 2.0
    pts = [(cx, cy, cz)]
    for sx in (-1, 1):
        for sy in (-1, 1):
            for sz in (-1, 1):
                pts.append((cx + sx * hx, cy + sy * hy, cz + sz * hz))
    return pts


def point_in_frustum(cam_pos, cam_yaw_deg, cam_pitch_deg, pt):
    """True if pt lies within the camera view frustum (in front + inside the
    horizontal & vertical FOV cones). This is the hard visibility gate."""
    dx = pt[0] - cam_pos[0]
    dy = pt[1] - cam_pos[1]
    dz = pt[2] - cam_pos[2]
    # Rotate world delta into camera frame: forward = +x after yaw.
    yaw = math.radians(cam_yaw_deg)
    fwd = dx * math.cos(yaw) + dy * math.sin(yaw)        # along view axis
    side = -dx * math.sin(yaw) + dy * math.cos(yaw)      # left/right
    if fwd <= 0.05:
        return False  # behind the camera
    # Horizontal angle from view axis.
    h_ang = math.degrees(math.atan2(abs(side), fwd))
    if h_ang > H_FOV_DEG / 2.0:
        return False
    # Vertical: account for camera pitch. Build pitch-corrected vertical angle.
    horiz_dist = math.sqrt(fwd * fwd + side * side)
    v_ang = math.degrees(math.atan2(dz, horiz_dist)) - cam_pitch_deg
    if abs(v_ang) > V_FOV_DEG / 2.0:
        return False
    return True


# ── Main ─────────────────────────────────────────────────────────────────────
report_lines = []
generated_tasks = []
try:
    from isaacsim import SimulationApp
    sim_app = SimulationApp({"headless": True})
    import omni.usd, carb, omni.physx, omni.timeline
    from omni.isaac.core.utils.stage import open_stage, is_stage_loading
    from pxr import UsdGeom

    sf = discover_scene_files(SCENE_DIR)
    open_stage(sf["stage"])
    while is_stage_loading():
        sim_app.update()
    stage = omni.usd.get_context().get_stage()
    # IMPORTANT: the PhysX collision scene is only built once the timeline is
    # playing. Without timeline.play() raycast_closest hits NOTHING — not even
    # walls. Play it, then let physics settle for a number of updates.
    timeline = omni.timeline.get_timeline_interface()
    timeline.play()
    for _ in range(60):
        sim_app.update()
    query_if = omni.physx.get_physx_scene_query_interface()
    log(f"[GEN] Scene loaded: {scene_basename}")

    # ── Object inventory from probed JSON (exact bbox center + size) ──
    probed_file = os.path.join(SCRIPT_DIR, f"probed_{scene_key}.json")
    if not os.path.exists(probed_file):
        log(f"ERROR: probed file not found: {probed_file}")
        sim_app.close(); sys.exit(1)
    pdata = json.load(open(probed_file))
    # category -> the LARGEST prim of that category (by bbox volume). The old
    # "first prim" rule sometimes picked a tiny instance — e.g. case01 has two
    # SimpleBookcase prims both ~0.7-0.8m tall (decorative, not real shelves).
    # Picking the largest gives the most prominent, instruction-matching object.
    def _vol(p):
        s = p["size"]
        return s[0] * s[1] * s[2]
    objs = {}
    all_centers = []
    for p in pdata["prims"]:
        if not (p["center"] and p["size"]):
            continue
        all_centers.append(p["center"])
        cat = p["category"]
        if cat not in objs or _vol(p) > _vol(objs[cat]):
            objs[cat] = p
    log(f"[GEN] {len(objs)} object categories with bbox")

    # ── Room bounds = the REAL floor bbox, not the furniture-center range ──
    # Furniture does not fill the room: a furniture-center range both MISSES
    # the true room corners and INCLUDES void outside the floor (where the
    # agent would spawn off-map). Read the actual floor prim's world bbox.
    # Prefer probed_*.json's room_bounds; otherwise read the floor prim live.
    room_bounds = pdata.get("room_bounds")
    if not room_bounds:
        floor_prim = None
        for p in stage.Traverse():
            nm = p.GetName().lower()
            if "floor" in nm and "ground" not in nm:
                floor_prim = p
                break
        if floor_prim:
            r = UsdGeom.Imageable(floor_prim).ComputeWorldBound(0, "default").GetRange()
            if not r.IsEmpty():
                mn, mx = r.GetMin(), r.GetMax()
                room_bounds = {"x": [float(mn[0]), float(mx[0])],
                               "y": [float(mn[1]), float(mx[1])]}
                log(f"[GEN] Room bounds from live floor prim {floor_prim.GetPath()}")
    if room_bounds:
        x_min, x_max = room_bounds["x"]
        y_min, y_max = room_bounds["y"]
    else:
        # Last-resort fallback: furniture-center range (known imprecise).
        log("[GEN] WARNING: no floor prim found — falling back to furniture-center range")
        x_min = min(c[0] for c in all_centers); x_max = max(c[0] for c in all_centers)
        y_min = min(c[1] for c in all_centers); y_max = max(c[1] for c in all_centers)
    room_w, room_h = x_max - x_min, y_max - y_min
    # Dynamic margin: 15% of the smaller room dimension, clamped to [1.5, 2.0].
    margin = min(room_w, room_h) * 0.15
    margin = max(1.5, min(2.0, margin))
    safe_xmin, safe_xmax = x_min + margin, x_max - margin
    safe_ymin, safe_ymax = y_min + margin, y_max - margin
    if safe_xmin >= safe_xmax:
        safe_xmin, safe_xmax = x_min + 0.5, x_max - 0.5
    if safe_ymin >= safe_ymax:
        safe_ymin, safe_ymax = y_min + 0.5, y_max - 0.5
    log(f"[GEN] Room {room_w:.1f}x{room_h:.1f}m (floor x[{x_min:.1f},{x_max:.1f}] "
        f"y[{y_min:.1f},{y_max:.1f}]), wall margin={margin:.2f}m, "
        f"safe zone x[{safe_xmin:.1f},{safe_xmax:.1f}] y[{safe_ymin:.1f},{safe_ymax:.1f}]")
    report_lines.append(f"- Room: {room_w:.1f}m x {room_h:.1f}m, dynamic wall margin "
                        f"= {margin:.2f}m (15% of min dim, clamped to [1.5, 2.0])")

    def _hit_path(hit):
        """Path of whatever a PhysX query hit. The result dict carries
        'rigidBody' (and sometimes 'collider'); take whichever is present."""
        return (hit.get("rigidBody") or hit.get("collider") or "")

    # ── Collision check (sphere sweep, reused from verify_tasks_isaac) ──
    def check_collision(x, y):
        hit = query_if.sweep_sphere_closest(AGENT_RADIUS, carb.Float3(x, y, 1.5),
                                            carb.Float3(0, 0, -1.0), 1.0)
        if hit["hit"]:
            path = _hit_path(hit).lower()
            if "floor" not in path and "ground" not in path:
                return True
        hit2 = query_if.sweep_sphere_closest(AGENT_RADIUS, carb.Float3(x, y, 0.7),
                                             carb.Float3(1.0, 0.0, 0.0), 0.01)
        return bool(hit2["hit"])

    # ── Clearance check ──
    # A start point that is merely collision-free can still be USELESS: if the
    # agent is wedged against a wall, every MOVE_FORWARD collides regardless of
    # how it turns, and the episode is stuck from step 0 (observed bug).
    # Require room to actually MOVE: sweep a step forward in many headings,
    # using bench_runner's EXACT MOVE_FORWARD parameters so the two agree —
    # sphere radius 0.40, heights z=0.5 and z=1.0, sweep length CLEAR_DIST.
    # CLEAR_DIST = 2 * STEP_DIST so both turning and stepping have space.
    STEP_DIST = 0.25
    CLEAR_DIST = 2.0 * STEP_DIST   # 0.5 m — must be clear to move + turn
    MIN_FREE_HEADINGS = 6          # of 12 — start must not be boxed in

    def count_free_headings(x, y):
        """How many of 12 evenly-spaced headings allow a CLEAR_DIST step
        without collision. Uses bench_runner's MOVE_FORWARD sweep params."""
        free = 0
        for i in range(12):
            ang = math.radians(i * 30.0)
            dx, dy = math.cos(ang), math.sin(ang)
            blocked = False
            for sz in (0.5, 1.0):
                hit = query_if.sweep_sphere_closest(
                    0.40, carb.Float3(x, y, sz), carb.Float3(dx, dy, 0.0), CLEAR_DIST)
                if hit["hit"]:
                    blocked = True
                    break
            if not blocked:
                free += 1
        return free

    # ── Wall-occlusion test ──
    # Cast a ray from the camera toward a target bbox point. The sightline is
    # "blocked" only if the first hit is an INTERIOR WALL or DOOR sitting
    # clearly BEFORE the target point.
    #
    # IMPORTANT: only interior-partition prims count. The room SHELL prims
    # (`*_exterior`, `*_floor`, `*_ceiling`) are watertight enclosing meshes —
    # a raycast between two interior points spuriously "hits" the exterior
    # shell, so including them flags every sightline as blocked. The real
    # interior wall prim is `*_wall`; partitions/doors are the genuine
    # occluders. Furniture hits don't count either (partial occlusion still
    # leaves the target visible).
    STRUCT_KEYWORDS = ("_wall", "door")
    BLOCK_MARGIN = 0.3  # m — hit must be this much nearer than target to block

    def ray_blocked_by_wall(cam_pos, pt):
        direction = [pt[i] - cam_pos[i] for i in range(3)]
        dist = math.sqrt(sum(d * d for d in direction))
        if dist < 0.05:
            return False
        dir_norm = [d / dist for d in direction]
        hit = query_if.raycast_closest(carb.Float3(*cam_pos),
                                       carb.Float3(*dir_norm), dist + 1.0)
        if not hit["hit"]:
            return False  # nothing in the way
        hit_dist = hit.get("distance", dist + 1.0)
        hit_path = _hit_path(hit).lower()
        is_struct = any(k in hit_path for k in STRUCT_KEYWORDS)
        # Blocked only if a structural prim sits clearly before the target.
        return is_struct and hit_dist < dist - BLOCK_MARGIN

    # ── Visibility classification ──
    def classify_visibility(cam_pos, cam_yaw, target_center, target_size, target_factory):
        """Return (n_in_frustum, n_visible) over bbox sample points.
        - n_in_frustum: points inside the FOV frustum.
        - n_visible: points inside frustum AND whose sightline is not wall-blocked.
        A target is VISIBLE iff n_visible > 0; PROVABLY HIDDEN iff n_in_frustum == 0
        (never even covered by the frustum) or n_visible == 0 (covered but every
        sightline runs through a wall)."""
        pts = bbox_sample_points(target_center, target_size)
        n_in_frustum, n_visible = 0, 0
        for pt in pts:
            if point_in_frustum(cam_pos, cam_yaw, CAM_PITCH_DEG, pt):
                n_in_frustum += 1
                if not ray_blocked_by_wall(cam_pos, pt):
                    n_visible += 1
        return n_in_frustum, n_visible

    def cam_from_pose(x, y, yaw):
        cx = x + 0.3 * math.cos(math.radians(yaw))
        cy = y + 0.3 * math.sin(math.radians(yaw))
        return (cx, cy, EYE_H)

    # Room centre — sampling is biased toward it (see sample_start).
    room_cx = (safe_xmin + safe_xmax) / 2.0
    room_cy = (safe_ymin + safe_ymax) / 2.0

    def _sample_xy(attempt):
        """Sample a candidate start (x,y), biased toward the room centre.
        Early attempts stay near the centre (open space, room to move); later
        attempts spread out toward the safe-zone edges as a fallback."""
        # Spread grows with attempt index: 0.3 of the half-extent early,
        # ramping to 1.0 (full safe zone) by the last attempts.
        spread = 0.3 + 0.7 * min(1.0, attempt / max(1, MAX_ATTEMPTS * 0.6))
        hx = (safe_xmax - safe_xmin) / 2.0 * spread
        hy = (safe_ymax - safe_ymin) / 2.0 * spread
        x = random.uniform(room_cx - hx, room_cx + hx)
        y = random.uniform(room_cy - hy, room_cy + hy)
        return (max(safe_xmin, min(safe_xmax, x)),
                max(safe_ymin, min(safe_ymax, y)))

    # ── Sample a start pose satisfying the visibility class ──
    def sample_start(target_center, target_size, target_factory, want_visible):
        """Resample start (x,y) + yaw until the visibility class is satisfied.
        Every candidate must be (a) collision-free, (b) have >= MIN_FREE_HEADINGS
        clear headings so the agent can actually move and turn — not wedged
        against a wall — and (c) match the requested visibility class.
        Returns (pos, yaw, status, detail)."""
        tc = list(target_center)
        if tc[2] < 0.1:           # flat bbox center -> aim at waist level
            tc = [tc[0], tc[1], 0.5]
        best = None
        for attempt in range(MAX_ATTEMPTS):
            tx, ty = _sample_xy(attempt)
            # Keep a sensible distance from the target (>=2.5m).
            d = math.hypot(tx - tc[0], ty - tc[1])
            if d < 2.5:
                continue
            if check_collision(tx, ty):
                continue
            # Clearance: the start must not be boxed in — otherwise the agent
            # is stuck from step 0 no matter how it turns.
            free = count_free_headings(tx, ty)
            if free < MIN_FREE_HEADINGS:
                continue
            yaw_to = math.degrees(math.atan2(tc[1] - ty, tc[0] - tx))
            yaw = yaw_to if want_visible else yaw_to + 180.0
            yaw = ((yaw + 180) % 360) - 180
            cam = cam_from_pose(tx, ty, yaw)
            n_fr, n_vis = classify_visibility(cam, yaw, target_center, target_size, target_factory)
            if want_visible and n_vis > 0:
                return ([round(tx, 2), round(ty, 2)], round(yaw, 1), "OK",
                        f"visible: {n_vis}/9 bbox points reachable, "
                        f"{free}/12 headings clear (attempt {attempt})")
            if not want_visible and n_vis == 0:
                why = "frustum miss" if n_fr == 0 else f"occluded ({n_fr}/9 in frustum)"
                return ([round(tx, 2), round(ty, 2)], round(yaw, 1), "OK",
                        f"hidden: {why}, {free}/12 headings clear (attempt {attempt})")
            if best is None:
                best = ([round(tx, 2), round(ty, 2)], round(yaw, 1), n_fr, n_vis)
        # Failed: report the closest attempt found.
        if best:
            return (best[0], best[1], "FAIL",
                    f"could not satisfy {'visible' if want_visible else 'hidden'} "
                    f"in {MAX_ATTEMPTS} tries (best: {best[3]}/9 visible, {best[2]}/9 in frustum)")
        return (None, None, "FAIL",
                f"no collision-free start with >={MIN_FREE_HEADINGS}/12 clear "
                f"headings in {MAX_ATTEMPTS} tries")

    # ── Target size gate ──
    # A navigation target must be visually prominent enough that the
    # instruction word matches what the agent sees. A target passes if it is
    # tall enough (>= MIN_TARGET_HEIGHT) OR has a large enough footprint
    # (>= MIN_TARGET_FOOTPRINT on its longer horizontal side) — the OR lets
    # through low-but-wide furniture like sofas and tables.
    # Targets that fail the gate are "small" — usable only as <=10% hard
    # examples (the agent must TILT to find them), never as the mainstream.
    MIN_TARGET_HEIGHT = 1.2      # m
    MIN_TARGET_FOOTPRINT = 0.9   # m (longer horizontal edge)

    def passes_size_gate(p):
        s = p["size"]
        return s[2] >= MIN_TARGET_HEIGHT or max(s[0], s[1]) >= MIN_TARGET_FOOTPRINT

    def has(cat): return cat in objs
    def center(cat): return objs[cat]["center"] if cat in objs else None
    def size(cat): return objs[cat]["size"] if cat in objs else None

    # ── L1 / L2 target selection ──
    if room == "living":
        l1_cat = "Sofa" if has("Sofa") else "CoffeeTable" if has("CoffeeTable") else "TVStand"
    else:
        l1_cat = "TableDining" if has("TableDining") else "Chair"

    # L2 target: among ALL categories whose semantic class is "bookshelf"
    # (SimpleBookcase / LargeShelf / CellShelf / ...), prefer the LARGEST one
    # that passes the size gate — so "go to the bookshelf" points at a real,
    # visually-obvious shelf, not a 0.8m decorative box.
    bookshelf_cats = [c for c in objs
                      if semantic_class_of(c + "Factory") == "bookshelf"]
    gated = [c for c in bookshelf_cats if passes_size_gate(objs[c])]
    if gated:
        l2_cat = max(gated, key=lambda c: objs[c]["size"][2])  # tallest
        l2_small = False
    elif bookshelf_cats:
        # Only undersized shelves exist — keep one as a HARD example.
        l2_cat = max(bookshelf_cats, key=lambda c: _vol(objs[c]))
        l2_small = True
    else:
        l2_cat = None
        l2_small = False

    # All bookshelf-semantic-class categories share the instruction word
    # "bookshelf" — the agent sees one unique shelf (others are deactivated by
    # bench_runner's semantic dedup), so the generic word is unambiguous.
    desc_map = {"Sofa": "the sofa", "CoffeeTable": "the coffee table", "TVStand": "the TV stand",
                "TableDining": "the dining table", "Chair": "a chair",
                "SimpleBookcase": "the bookshelf", "LargeShelf": "the bookshelf",
                "CellShelf": "the bookshelf", "TriangleShelf": "the bookshelf",
                "DeskLamp": "the desk lamp", "Mirror": "the mirror",
                "SingleCabinet": "the cabinet", "KitchenCabinet": "the kitchen cabinet"}

    def build_task(level, cat, want_visible, is_small=False):
        if not cat or not has(cat):
            report_lines.append(f"- {scene_id}-{level}: SKIP — no suitable target object")
            return
        tcen, tsize = center(cat), size(cat)
        factory = cat + "Factory"
        pos, yaw, status, detail = sample_start(tcen, tsize, factory, want_visible)
        radius = 3.0 if cat in ("Sofa", "TableDining") else 2.0
        tid = f"{scene_id}-{level}"
        small_note = (f" [SMALL TARGET — {tsize[2]:.2f}m tall, hard example]"
                      if is_small else "")
        if status == "OK":
            generated_tasks.append({
                "id": tid, "level": level, "scene_dir": scene_basename,
                "instruction": f"Go to {desc_map.get(cat, 'the ' + cat.lower())}.",
                "agent_start": pos, "agent_yaw": yaw,
                "phases": [{"name": f"nav_{cat.lower()}", "target_object": factory,
                            "radius": radius, "action": "STOP",
                            "desc": desc_map.get(cat, cat.lower()), "place_at": None}],
                "_visibility": {"class": "visible" if want_visible else "hidden",
                                "verified": True, "detail": detail},
                "_target_size": [round(x, 2) for x in tsize],
                # Small targets are hard examples — keep their share <=10%.
                "_difficulty_tag": "hard_small_target" if is_small else "normal",
            })
            report_lines.append(f"- {tid}: ✅ SUCCESS — target={cat}, start={pos}, "
                                f"yaw={yaw} — {detail}{small_note}")
            log(f"[GEN] {tid}: OK — {detail}{small_note}")
        else:
            report_lines.append(f"- {tid}: ❌ FAILED — target={cat} — {detail}{small_note}")
            log(f"[GEN] {tid}: FAILED — {detail}")

    log(f"[GEN] L1 target={l1_cat}, L2 target={l2_cat} (small={l2_small})")
    build_task("L1", l1_cat, want_visible=True)
    build_task("L2", l2_cat, want_visible=False, is_small=l2_small)

    # ── Write outputs ──
    bench = {"benchmark": "4DSynth-Nav", "version": "2.0-fullgen", "max_steps": 150,
             "generator": "full_task_gen.py", "tasks": generated_tasks}
    # Merge into existing output file if present (one scene at a time).
    if os.path.exists(OUT_JSON):
        existing = json.load(open(OUT_JSON))
        kept = [t for t in existing.get("tasks", []) if not t["id"].startswith(scene_id + "-")]
        bench["tasks"] = kept + generated_tasks
    json.dump(bench, open(OUT_JSON, "w"), indent=2)
    log(f"[GEN] Wrote {len(generated_tasks)} task(s) for {scene_id} -> {OUT_JSON}")

    # ── Report to workspace ──
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f"task_gen_report_{scene_id}.md")
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_ok = len(generated_tasks)
    with open(report_path, "w") as f:
        f.write(f"# Task generation report — {scene_basename}\n\n")
        f.write(f"Generated: {ts}\n\n")
        f.write(f"Generator: full_task_gen.py (FOV-frustum + multi-ray visibility)\n\n")
        f.write(f"Camera: H-FOV {H_FOV_DEG:.0f}°, V-FOV {V_FOV_DEG:.1f}°, "
                f"eye {EYE_H}m, pitch {CAM_PITCH_DEG:.0f}°\n\n")
        f.write(f"Result: {n_ok} task(s) generated successfully.\n\n")
        f.write("## Per-task\n\n")
        f.write("\n".join(report_lines) + "\n")
    log(f"[GEN] Report written: {report_path}")

    sim_app.close()

except Exception as e:
    traceback.print_exc()
    # Still try to dump a failure report.
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(os.path.join(REPORT_DIR, f"task_gen_report_{scene_id}.md"), "w") as f:
            f.write(f"# Task generation report — {scene_basename}\n\nFATAL ERROR:\n\n")
            f.write("```\n" + traceback.format_exc() + "\n```\n")
    except Exception:
        pass
    sys.exit(1)
