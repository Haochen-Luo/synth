#!/usr/bin/env python3
"""full_task_gen.py — 4DSynth-Nav L1/L2 task generator.

DESIGN (kept deliberately simple — the previous version over-engineered this
and spent a long time fighting secondary bugs of its own making):

  * Start placement = the old, proven heuristic: a point offset from the room
    centre, away from the target. Centre-ish points are naturally open, lit,
    and walkable — no raycast, no collider, no floor-prim dependency needed.

  * L1 (target VISIBLE): agent faces the target. A target counts as visible if
    its bounding box falls inside the camera FOV frustum (pure geometry from
    the probed bbox). The FPV is wide and the start is open, so frustum
    coverage is a good-enough visibility criterion.

  * L2 (target HIDDEN): agent faces 180° AWAY from the target. Facing away
    makes the target geometrically impossible to see — no verification needed.

  * Target selection: among the candidate categories, pick the TALLEST prim
    (by bbox height). Standing furniture like shelves reads as "tall = obvious";
    this avoids picking a tiny decorative instance — e.g. case01 has a 0.81 m
    "SimpleBookcase"; the 2.1 m LargeShelf is the real "bookshelf".

  * Semantic-class disambiguation is handled at RUN time by bench_runner
    (deactivating same-semantic-class non-target props) — not here.

OPTIONAL raycast report (informational only, never affects generation):
  After generating L1 tasks, an occlusion raycast is run. If it disagrees with
  the frustum heuristic (target may be occluded), the task is NOT dropped —
  instead it is logged to review_flagged/ with a metadata file for the user to
  manually inspect. This keeps the fragile raycast OUT of the critical path.

Run inside the vlm-jupyter container:
  SCENE_DIR=/abs/path/to/native_caseNN_..._scene \
  /isaac-sim/python.sh full_task_gen.py
"""
import sys, os, json, math, datetime, traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bench_helpers import discover_scene_files
from semantic_classes import semantic_class_of

# ── Config ──
SCENE_DIR = os.environ.get("SCENE_DIR", "")
OUT_JSON = os.environ.get("OUT_JSON", os.path.join(SCRIPT_DIR, "benchmark_tasks_fullgen.json"))
REPORT_DIR = os.environ.get("REPORT_DIR", os.path.join(SCRIPT_DIR, "task_gen_reports"))
FLAG_DIR = os.environ.get("FLAG_DIR", os.path.join(SCRIPT_DIR, "review_flagged"))

# Camera: FPV focal 17mm / aperture 34mm -> 90° horizontal FOV.
# Render 1920x1080 -> ~58.7° vertical FOV. Eye height 1.58 m, pitch -10°.
H_FOV_DEG = 90.0
V_FOV_DEG = 2.0 * math.degrees(math.atan(math.tan(math.radians(H_FOV_DEG / 2)) * 1080.0 / 1920.0))
EYE_H = 1.58
CAM_PITCH_DEG = -10.0

if not SCENE_DIR:
    print("ERROR: SCENE_DIR env var is required"); sys.exit(1)
scene_basename = os.path.basename(SCENE_DIR.rstrip("/"))
scene_key = scene_basename.replace("native_", "").replace("_full_physics_scene", "")
scene_id = scene_key.split("_")[0]  # caseNN
room = "living" if "living" in scene_basename else "dining"


def log(m): print(m, flush=True)


# ── FOV frustum test ─────────────────────────────────────────────────────────
def bbox_corners(center, size):
    """8 corners + centre of an axis-aligned bbox."""
    cx, cy, cz = center
    hx, hy, hz = size[0] / 2.0, size[1] / 2.0, size[2] / 2.0
    pts = [(cx, cy, cz)]
    for sx in (-1, 1):
        for sy in (-1, 1):
            for sz in (-1, 1):
                pts.append((cx + sx * hx, cy + sy * hy, cz + sz * hz))
    return pts


def point_in_frustum(cam_pos, cam_yaw_deg, pt):
    """True if pt is within the camera view frustum (in front + inside the
    horizontal & vertical FOV cones, given the fixed camera pitch)."""
    dx, dy, dz = pt[0] - cam_pos[0], pt[1] - cam_pos[1], pt[2] - cam_pos[2]
    yaw = math.radians(cam_yaw_deg)
    fwd = dx * math.cos(yaw) + dy * math.sin(yaw)
    side = -dx * math.sin(yaw) + dy * math.cos(yaw)
    if fwd <= 0.05:
        return False
    if math.degrees(math.atan2(abs(side), fwd)) > H_FOV_DEG / 2.0:
        return False
    horiz = math.hypot(fwd, side)
    v_ang = math.degrees(math.atan2(dz, horiz)) - CAM_PITCH_DEG
    return abs(v_ang) <= V_FOV_DEG / 2.0


def frustum_coverage(cam_pos, cam_yaw, center, size):
    """Fraction of bbox sample points inside the frustum (0..1)."""
    pts = bbox_corners(center, size)
    n = sum(1 for p in pts if point_in_frustum(cam_pos, cam_yaw, p))
    return n / len(pts)


# ── Main ─────────────────────────────────────────────────────────────────────
report_lines = []
flagged = []          # L1 tasks the optional raycast flags as maybe-occluded
generated_tasks = []
try:
    from isaacsim import SimulationApp
    sim_app = SimulationApp({"headless": True})
    import omni.usd, carb, omni.physx, omni.timeline
    from omni.isaac.core.utils.stage import open_stage, is_stage_loading

    sf = discover_scene_files(SCENE_DIR)
    open_stage(sf["stage"])
    while is_stage_loading():
        sim_app.update()
    stage = omni.usd.get_context().get_stage()
    # Timeline must play for the PhysX collision scene (used by the optional
    # raycast report) to exist.
    omni.timeline.get_timeline_interface().play()
    for _ in range(60):
        sim_app.update()
    query_if = omni.physx.get_physx_scene_query_interface()
    log(f"[GEN] Scene loaded: {scene_basename}")

    # ── Object inventory: largest prim per category ──
    probed_file = os.path.join(SCRIPT_DIR, f"probed_{scene_key}.json")
    if not os.path.exists(probed_file):
        log(f"ERROR: probed file not found: {probed_file}")
        sim_app.close(); sys.exit(1)
    pdata = json.load(open(probed_file))

    def _height(p):
        return p["size"][2]

    objs = {}            # category -> tallest prim of that category
    all_centers = []
    for p in pdata["prims"]:
        if not (p["center"] and p["size"]):
            continue
        all_centers.append(p["center"])
        cat = p["category"]
        if cat not in objs or _height(p) > _height(objs[cat]):
            objs[cat] = p
    log(f"[GEN] {len(objs)} object categories with bbox")

    # Approximate room centre (furniture-center range midpoint — only needs to
    # be roughly central, not a precise boundary).
    room_cx = (min(c[0] for c in all_centers) + max(c[0] for c in all_centers)) / 2.0
    room_cy = (min(c[1] for c in all_centers) + max(c[1] for c in all_centers)) / 2.0
    room_hx = (max(c[0] for c in all_centers) - min(c[0] for c in all_centers)) / 2.0
    room_hy = (max(c[1] for c in all_centers) - min(c[1] for c in all_centers)) / 2.0

    # ── Start validity: must be ON the real floor mesh, and clear of furniture ──
    # The room floor bbox is just an axis-aligned box; the real floor mesh does
    # NOT fill it (rooms are L-shaped / have alcoves). A point can be inside the
    # bbox yet over VOID — the agent then renders into a black off-floor space.
    # The only reliable "inside the room" test is a downward raycast that hits
    # `living_room_*_floor`.
    def on_floor(x, y):
        """True iff a downward ray from above (x,y) hits the room floor mesh."""
        hit = query_if.raycast_closest(carb.Float3(x, y, 3.0),
                                       carb.Float3(0, 0, -1.0), 5.0)
        if not hit["hit"]:
            return False
        path = (hit.get("rigidBody") or hit.get("collider") or "").lower()
        return "floor" in path

    def furniture_overlap(x, y):
        """True iff a sphere at standing height overlaps furniture/wall here."""
        hit = query_if.sweep_sphere_closest(0.40, carb.Float3(x, y, 1.5),
                                            carb.Float3(0, 0, -1.0), 1.0)
        if hit["hit"]:
            path = (hit.get("rigidBody") or hit.get("collider") or "").lower()
            if "floor" not in path and "ground" not in path:
                return True
        return False

    # ── Start placement: sample near the ROOM CENTRE, >= MIN_TARGET_DIST away ──
    # Centre-biased sampling favours the open, lit part of the room. The
    # MIN_TARGET_DIST floor (> the 3 m STOP success radius) guarantees the task
    # has real navigation distance — a start closer than 3 m would already
    # count as "arrived". MAX keeps the route from being needlessly long.
    MIN_TARGET_DIST = 4.0   # m — must exceed the 3 m STOP success radius
    MAX_TARGET_DIST = 10.0  # m

    def pick_start(target_xy, want_visible):
        """Sample a collision-free start biased toward the room centre, between
        MIN_TARGET_DIST and MAX_TARGET_DIST from the target. Yaw faces the
        target (L1) or 180 deg away (L2). Returns (pos, yaw)."""
        import random as _r
        tx, ty = target_xy
        for attempt in range(300):
            # Centre-biased: spread grows with attempt index as a fallback.
            spread = 0.35 + 0.65 * min(1.0, attempt / 180.0)
            sx = _r.uniform(room_cx - room_hx * spread, room_cx + room_hx * spread)
            sy = _r.uniform(room_cy - room_hy * spread, room_cy + room_hy * spread)
            d = math.hypot(sx - tx, sy - ty)
            if d < MIN_TARGET_DIST or d > MAX_TARGET_DIST:
                continue
            if not on_floor(sx, sy):     # over void / off the real floor mesh
                continue
            if furniture_overlap(sx, sy):
                continue
            yaw_to = math.degrees(math.atan2(ty - sy, tx - sx))
            yaw = yaw_to if want_visible else yaw_to + 180.0
            yaw = ((yaw + 180) % 360) - 180
            return [round(sx, 2), round(sy, 2)], round(yaw, 1), True
        # No on-floor, clear, distance-valid sample found in 300 tries.
        return None, None, False

    def cam_from_pose(x, y, yaw):
        return (x + 0.3 * math.cos(math.radians(yaw)),
                y + 0.3 * math.sin(math.radians(yaw)), EYE_H)

    # ── Optional occlusion raycast (INFORMATIONAL ONLY) ──
    # Cast rays from camera to the target bbox corners; if every ray is blocked
    # by something well before the target, the target may be occluded. This
    # NEVER drops a task — it only flags it for manual review.
    def occlusion_check(cam_pos, center, size):
        """Return (n_clear, n_total) — how many bbox-corner sightlines reach
        close to the target without an early blocker."""
        pts = bbox_corners(center, size)
        n_clear = 0
        for pt in pts:
            d = [pt[i] - cam_pos[i] for i in range(3)]
            dist = math.sqrt(sum(v * v for v in d))
            if dist < 0.05:
                n_clear += 1
                continue
            dn = [v / dist for v in d]
            hit = query_if.raycast_closest(carb.Float3(*cam_pos),
                                           carb.Float3(*dn), dist + 1.0)
            if not hit["hit"]:
                n_clear += 1            # nothing in the way
                continue
            hit_dist = hit.get("distance", dist + 1.0)
            if hit_dist >= dist - 0.3:   # blocker is at/after the target
                n_clear += 1
        return n_clear, len(pts)

    # ── Connectivity raycast (INFORMATIONAL ONLY) ──
    # Cast a waist-height ray from the start straight toward the target. If it
    # hits an INTERIOR WALL well before reaching the target, the start and the
    # target are probably in different room partitions — the agent may not be
    # able to walk a straight-ish route. This NEVER drops a task; it only flags
    # it for manual review. (A straight ray is a coarse proxy: a real route may
    # go around, so a flag is a "look at this", not a verdict.)
    def connectivity_blocked(start_xy, target_xy):
        """Return (blocked_bool, detail). blocked = an interior wall sits on
        the straight start->target line before the target."""
        sx, sy = start_xy
        tx, ty = target_xy
        dx, dy = tx - sx, ty - sy
        dist = math.hypot(dx, dy)
        if dist < 0.1:
            return False, "start≈target"
        dn = (dx / dist, dy / dist, 0.0)
        origin = carb.Float3(sx, sy, 1.0)   # waist height
        hit = query_if.raycast_closest(origin, carb.Float3(*dn), dist)
        if not hit["hit"]:
            return False, "clear straight line to target"
        hit_dist = hit.get("distance", dist)
        hit_path = (hit.get("rigidBody") or hit.get("collider") or "").lower()
        is_wall = "_wall" in hit_path or "door" in hit_path
        if is_wall and hit_dist < dist - 0.5:
            return True, (f"interior wall on straight line at {hit_dist:.1f}m "
                          f"(target {dist:.1f}m) — start/target may be in "
                          f"different partitions")
        return False, f"first hit is furniture, not a partition wall"

    # ── Target selection ──
    def has(cat): return cat in objs

    if room == "living":
        l1_cat = "Sofa" if has("Sofa") else "CoffeeTable" if has("CoffeeTable") else "TVStand"
    else:
        l1_cat = "TableDining" if has("TableDining") else "Chair"

    # L2 target: the TALLEST object whose semantic class is "bookshelf"
    # (SimpleBookcase / LargeShelf / CellShelf / ...). Tallest = most visually
    # obvious, instruction-matching shelf (e.g. case01's 2.1 m LargeShelf).
    bookshelf_cats = [c for c in objs if semantic_class_of(c + "Factory") == "bookshelf"]
    l2_cat = max(bookshelf_cats, key=lambda c: _height(objs[c])) if bookshelf_cats else None

    desc_map = {"Sofa": "the sofa", "CoffeeTable": "the coffee table",
                "TVStand": "the TV stand", "TableDining": "the dining table",
                "Chair": "a chair", "SimpleBookcase": "the bookshelf",
                "LargeShelf": "the bookshelf", "CellShelf": "the bookshelf",
                "TriangleShelf": "the bookshelf"}

    def build_task(level, cat, want_visible):
        if not cat or not has(cat):
            report_lines.append(f"- {scene_id}-{level}: SKIP — no suitable target")
            return
        p = objs[cat]
        tcen, tsize = p["center"], p["size"]
        target_xy = tcen[:2]
        factory = cat + "Factory"
        tid = f"{scene_id}-{level}"
        pos, yaw, ok = pick_start(target_xy, want_visible)
        if not ok:
            report_lines.append(f"- {tid}: ❌ FAILED — no on-floor start "
                                f">={MIN_TARGET_DIST}m from {cat} found")
            log(f"[GEN] {tid}: FAILED — no valid on-floor start")
            return
        radius = 3.0 if cat in ("Sofa", "TableDining") else 2.0
        cam = cam_from_pose(pos[0], pos[1], yaw)

        # Visibility statement (geometry only).
        cov = frustum_coverage(cam, yaw, tcen, tsize)
        if want_visible:
            vis_detail = f"frustum coverage {cov*100:.0f}% of bbox"
        else:
            vis_detail = "faces 180 deg away — geometrically hidden"

        task = {
            "id": tid, "level": level, "scene_dir": scene_basename,
            "instruction": f"Go to {desc_map.get(cat, 'the ' + cat.lower())}.",
            "agent_start": pos, "agent_yaw": yaw,
            "phases": [{"name": f"nav_{cat.lower()}", "target_object": factory,
                        "radius": radius, "action": "STOP",
                        "desc": desc_map.get(cat, cat.lower()), "place_at": None}],
            "_visibility": {"class": "visible" if want_visible else "hidden",
                            "detail": vis_detail},
            "_target_size": [round(v, 2) for v in tsize],
        }
        generated_tasks.append(task)
        report_lines.append(f"- {tid}: target={cat} {tuple(round(v,2) for v in tsize)}, "
                            f"start={pos} yaw={yaw} — {vis_detail}")
        log(f"[GEN] {tid}: target={cat}, {vis_detail}")

        # ── Raycast checks — INFORMATIONAL ONLY, never block generation ──
        flag_reasons = []
        # (a) Occlusion — L1 only: target may be visually blocked.
        if want_visible:
            n_clear, n_total = occlusion_check(cam, tcen, tsize)
            if n_clear == 0:
                flag_reasons.append(
                    f"occlusion: 0/{n_total} sightlines clear — target may be "
                    f"occluded despite {cov*100:.0f}% frustum coverage")
        # (b) Connectivity — all tasks: start/target maybe in different partitions.
        conn_blocked, conn_detail = connectivity_blocked(pos, target_xy)
        if conn_blocked:
            flag_reasons.append(f"connectivity: {conn_detail}")
        if flag_reasons:
            flagged.append({
                "id": tid, "scene": scene_basename, "level": level, "target": cat,
                "target_factory": factory, "agent_start": pos, "agent_yaw": yaw,
                "reasons": flag_reasons,
            })
            log(f"[GEN] {tid}: FLAGGED for review — {'; '.join(flag_reasons)}")

    log(f"[GEN] L1 target={l1_cat}, L2 target={l2_cat}")
    build_task("L1", l1_cat, want_visible=True)
    build_task("L2", l2_cat, want_visible=False)

    # ── Write task JSON (merge per-scene) ──
    bench = {"benchmark": "4DSynth-Nav", "version": "3.0-fullgen", "max_steps": 150,
             "generator": "full_task_gen.py", "tasks": generated_tasks}
    if os.path.exists(OUT_JSON):
        existing = json.load(open(OUT_JSON))
        kept = [t for t in existing.get("tasks", []) if not t["id"].startswith(scene_id + "-")]
        bench["tasks"] = kept + generated_tasks
    json.dump(bench, open(OUT_JSON, "w"), indent=2)
    log(f"[GEN] Wrote {len(generated_tasks)} task(s) for {scene_id} -> {OUT_JSON}")

    # ── Report ──
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(REPORT_DIR, f"task_gen_report_{scene_id}.md"), "w") as f:
        f.write(f"# Task generation report — {scene_basename}\n\n")
        f.write(f"Generated: {ts}\n\n")
        f.write(f"Generator: full_task_gen.py v3 (FOV-frustum heuristic; "
                f"raycast = informational only)\n\n")
        f.write("## Per-task\n\n" + "\n".join(report_lines) + "\n")
        if flagged:
            f.write(f"\n## ⚠ Flagged for manual review: {len(flagged)}\n\n")
            for fl in flagged:
                f.write(f"- {fl['id']}: {'; '.join(fl['reasons'])}\n")

    # ── Flagged-for-review folder (metadata only) ──
    # The bird-view image is NOT rendered here. bench_runner already renders a
    # bird frame for every task it runs; when it runs a task listed in this
    # metadata, it copies that first frame into FLAG_DIR itself. No extra
    # rendering pass, no duplicated camera code.
    if flagged:
        os.makedirs(FLAG_DIR, exist_ok=True)
        meta_path = os.path.join(FLAG_DIR, f"flagged_{scene_id}.json")
        json.dump({"scene": scene_basename, "flagged": flagged}, open(meta_path, "w"), indent=2)
        log(f"[GEN] {len(flagged)} task(s) flagged -> {meta_path} (for manual inspection)")

    sim_app.close()

except Exception as e:
    traceback.print_exc()
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(os.path.join(REPORT_DIR, f"task_gen_report_{scene_id}.md"), "w") as f:
            f.write(f"# Task generation report — {scene_basename}\n\nFATAL ERROR:\n\n")
            f.write("```\n" + traceback.format_exc() + "\n```\n")
    except Exception:
        pass
    sys.exit(1)
