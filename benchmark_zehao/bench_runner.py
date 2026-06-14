"""4DSynth-Nav benchmark runner. Runs ONE task inside Isaac Sim.
Usage: TASK_ID=01-L1 /isaac-sim/python.sh bench_runner.py
   or: TASK_JSON=/path/to/single_task.json /isaac-sim/python.sh bench_runner.py
"""
import sys, os, json, math, base64, glob, time, traceback, re
import urllib.request, datetime as _dt

# Make all created files/dirs world-writable so host user can access
# Docker results (runner executes as root inside container).
os.umask(0o000)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bench_helpers import (sample_human_motion, wrap_angle_deg, check_frame_quality,
                           make_nav_system_prompt, make_multistep_system_prompt,
                           discover_scene_files, find_prim_by_factory,
                           find_all_prims_by_factory, get_prim_world_center,
                           resolve_target, compute_metrics)
from semantic_classes import semantic_class_of

# ── Config ──
def _auto_detect_model_name(vllm_url):
    try:
        from urllib.parse import urlparse
        parsed = urlparse(vllm_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}/v1/models"
        req = urllib.request.Request(base_url)
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            return data["data"][0]["id"]
    except Exception as e:
        print(f"Warning: Could not auto-detect model from {vllm_url}: {e}")
        return "Qwen/Qwen3-VL-30B-A3B-Thinking-FP8"

VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8300/v1/chat/completions")
MODEL_NAME = os.environ.get("MODEL_NAME") or _auto_detect_model_name(VLLM_URL)
VLM_API_KEY = os.environ.get("VLM_API_KEY", "")  # set for external APIs (OpenRouter, etc.)
TASKS_JSON = os.environ.get("TASKS_JSON", os.path.join(SCRIPT_DIR, "benchmark_tasks.json"))
RESULTS_BASE = os.path.join(SCRIPT_DIR, "results")

STEP_DIST = 0.25; TURN_ANG = 15.0; TILT_ANG = 5.0
PITCH_MIN = -30; PITCH_MAX = 10; PITCH_INIT = -10
EYE_H = 1.58; MESH_YAW_OFF = 90.0; RUNNER_TIME_PER_STEP = 0.5
DONE_CONFIRM = 2
# Hard cap on consecutive PAUSE actions. After this many in a row the next
# prompt appends a directive asking the VLM to choose a different action.
PAUSE_HARDCAP = 3
# Episode ends at whichever limit is hit FIRST: 150 physical steps OR 50 VLM
# calls. With multi-action planning the two are decoupled; capping VLM calls
# keeps evaluation fast and bounds the planning/reasoning budget.
# Step cap (single-action mode: 1 VLM call per step, so this is also the VLM
# budget). Set via env to override per-run.
MAX_STEPS = int(os.environ.get("MAX_STEPS", "150"))
MAX_VLM_CALLS = int(os.environ.get("MAX_VLM_CALLS", "50"))
# Bird-view smooth video toggle. False (default, prototype speed): no bird
# _smooth folder, no bird filler frames — the bird video is built from the
# per-step decision frames in vlm_nav_frames_bird/. True: full bird _smooth
# folder with decision + filler frames (paper-quality, slower).
ENABLE_BIRD_SMOOTH = os.environ.get("ENABLE_BIRD_SMOOTH", "0") == "1"
# Render resolution for FPV + bird render products. PathTracing cost scales
# ~linearly with pixel count, so 960x540 renders ~4x faster than 1920x1080.
# Lowered for prototype iteration speed; set RENDER_W/RENDER_H env to override.
RENDER_W = int(os.environ.get("RENDER_W", "540"))
RENDER_H = int(os.environ.get("RENDER_H", "360"))
# Master filler-frame switch. Default 0 (off, prototype speed): NO filler
# frames are rendered at all — every step renders only its single decision
# frame (the image the VLM sees). sim_t still advances by RUNNER_TIME_PER_STEP
# per step regardless, so runner world-time, dynamic-collision prediction and
# the decision frames are byte-for-byte identical to a filler-on run; only the
# *_smooth videos lose their in-between frames (runner leaps between decision
# frames instead of walking smoothly). Set to 1 for paper-quality continuous
# *_smooth video — then VISIBILITY_GATE (below) decides which steps get filler.
RENDER_FILLER = os.environ.get("RENDER_FILLER", "0") == "1"
# Visibility-gated filler rendering. ONLY consulted when RENDER_FILLER=1.
# Default 0 (off): every step renders filler frames regardless of runner FOV.
# This eliminates stale-USD-state visual artifacts seen after long off-screen
# sequences (see case004-L2 investigation). Set to 1 to re-enable the ~30-50%
# speedup that skips filler on off-screen steps — only do this if visual
# continuity isn't needed and you've confirmed the artifacts don't recur.
VISIBILITY_GATE = os.environ.get("VISIBILITY_GATE", "0") == "1"
# Diagnostic: log every MOVE_FORWARD sweep result (hit/dist/path per height) to
# investigate collider registration / wall clipping. Off by default = zero impact
# on normal eval runs.
SWEEP_DEBUG = os.environ.get("SWEEP_DEBUG", "0") == "1"
# Number of temporal context frames fed to the VLM per decision (oldest..current).
# Default 3 = baseline (step-2, step-1, current). Set N_FRAMES=1 for single
# current-frame-only mode (no temporal history). "up to 3 images" wording in
# the system prompt stays valid since "up to" includes 1.
N_FRAMES = max(1, int(os.environ.get("N_FRAMES", "3")))

# ── Load task config ──
task_id = os.environ.get("TASK_ID", "")
task_json_path = os.environ.get("TASK_JSON", "")

if task_json_path and os.path.exists(task_json_path):
    task = json.load(open(task_json_path))
else:
    bench = json.load(open(TASKS_JSON))
    tasks_map = {t["id"]: t for t in bench["tasks"]}
    if task_id not in tasks_map:
        print(f"ERROR: TASK_ID={task_id!r} not found. Available: {list(tasks_map.keys())}")
        sys.exit(1)
    task = tasks_map[task_id]
    task["max_steps"] = MAX_STEPS

tid = task["id"]; level = task["level"]
scene_dir = os.path.join(SCRIPT_DIR, task["scene_dir"])
phases = task["phases"]; max_steps = task.get("max_steps", MAX_STEPS)
# Normalize legacy "STOP" phase-completion action to "DONE" so the rest of
# the code only has to recognize one name. The benchmark JSON still uses
# "STOP" — we leave the file untouched and rewrite in memory.
for _p in phases:
    if _p.get("action") == "STOP":
        _p["action"] = "DONE"
is_multi = len(phases) > 1 or phases[0]["action"] != "DONE"
agent_start_xy = task.get("agent_start")
agent_start_yaw = task.get("agent_yaw")
auto_spawn = agent_start_xy is None  # will be resolved after target positions are known
spawn_facing = task.get("spawn_facing", "face")

RUNNER_MESH_GROUND_Z = 0.6773  # bbox-calibrated Z for runner mesh ground contact
DANCER_MESH_GROUND_Z = 0.8961  # bbox-calibrated Z for dancer mesh

# ── Run dir: results/L1/01-L1_20260514_183000/ ──
ts = _dt.datetime.now().strftime('%Y%m%d_%H%M%S')
batch_name = os.environ.get("BATCH_NAME", "")
if batch_name:
    RESULTS_BASE = os.path.join(RESULTS_BASE, batch_name)
RUN_DIR = os.path.join(RESULTS_BASE, level, f"{tid}_{ts}")
os.makedirs(RUN_DIR, exist_ok=True)
LOG = os.path.join(RUN_DIR, "run.log")

def log(msg):
    with open(LOG, "a") as f: f.write(msg + "\n")
    print(msg)

# ── Collision-ignore whitelist (PRECISE, not substring) ──
# A PhysX sweep/raycast hit on one of these is NOT a navigation obstacle:
#   - structural floor meshes (the sweep sphere bottom dips below floor-top and
#     PhysX reports a dist=0 overlap that is not a real wall),
#   - soft floor-level textiles that drape and should never block navigation.
# IMPORTANT: this MUST be exact/token matching, NOT substring `in`. The old
# substring whitelist ("floor","ground","rug","blanket","towel","mat") matched
# FloorLampFactory (contains "floor") and MattressFactory (contains "mat"),
# making a floor lamp and a mattress "walkable" — the agent then walked straight
# through the lamp (and, via the dist=0 closest-hit occluding the wall behind it,
# straight through the wall too). See docs/bw_samples_analysis/ANALYSIS.md.
WALKABLE_FACTORIES = frozenset({
    "rugfactory", "blanketfactory", "towelfactory",
    "comforterfactory", "boxcomforterfactory",
})
def is_walkable_hit(hit_path):
    """True if a PhysX hit path is a non-obstacle (floor / soft textile).
    Matches on the prim's basename, not arbitrary substrings."""
    if not hit_path:
        return False
    seg = hit_path.rstrip("/").split("/")[-1].lower()
    # structural floor: '<room>_<i>_<j>_floor' (NOT _wall/_exterior/_ceiling/skirtingboard)
    if seg.endswith("_floor") or seg in ("floor", "ground"):
        return True
    # factory: pull the '<name>factory' token from e.g. obj_123_rugfactory_spawn_asset_0
    m = re.search(r'([a-z]+factory)', seg)
    if m and m.group(1) in WALKABLE_FACTORIES:
        return True
    return False


# ── Room-boundary helpers ──────────────────────────────────────────────────
# Many compiled scenes are NOT closed boxes: the room footprint has openings /
# missing walls onto an unlit "void" outside. The collision sweep correctly
# returns NO hit there (no wall geometry to hit), so an agent that walks toward
# the opening passes the boundary unobstructed and ends up in black space (FPV
# all-black, a false failure). Nothing constrains the agent to stay in the room.
# Fix: compute each room's floor polygon from the USD floor meshes and treat a
# MOVE_FORWARD that would leave EVERY room polygon as blocked (a soft boundary),
# exactly like hitting a wall. Self-contained (mirrors validate_all_spawns'
# find_floor_polygon / point_in_polygon_xy so the runner needs no extra import).
_ROOM_TOKENS = ("living_room", "living-room", "dining_room", "dining-room",
                "bedroom", "bathroom", "kitchen", "hallway")
ROOM_BOUNDARY = os.environ.get("ROOM_BOUNDARY", "1") == "1"
# Inward inset (m): the agent's CENTROID must stay at least this far INSIDE the
# floor polygon. Set to the capsule radius + a small clearance so the collision
# capsule (r=AGENT_RADIUS=0.40) never reaches a "void wall" (missing-geometry
# floor-hull edge), and the FPV camera (at the centroid) never peeks through the
# opening into the unlit exterior -> black frames. 0.40 + 0.05 = 0.45m.
# (Was previously an OUTWARD margin of 0.5m, which let the centroid leave the
#  polygon by up to 0.5m and the camera stare into the void -> the case069 bug.)
ROOM_BOUNDARY_INSET = float(os.environ.get("ROOM_BOUNDARY_INSET", "0.45"))


def _convex_hull_xy(pts):
    """Andrew's monotone-chain convex hull of [x,y] points."""
    pts = sorted(set((round(p[0], 4), round(p[1], 4)) for p in pts))
    if len(pts) < 3:
        return [list(p) for p in pts]
    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in (lower[:-1] + upper[:-1])]


def _point_in_polygon_xy(px, py, poly):
    """Ray-casting point-in-polygon (matches validate_all_spawns)."""
    if len(poly) < 3:
        return True
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = float(poly[i][0]), float(poly[i][1])
        xj, yj = float(poly[j][0]), float(poly[j][1])
        if (yi > py) != (yj > py):
            xc = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < xc:
                inside = not inside
        j = i
    return inside


def compute_room_polygons(stage):
    """Return a list of per-room floor convex-hull polygons (world XY).
    Empty list if no floor meshes found (caller should then disable the gate)."""
    from pxr import UsdGeom, Gf
    rooms = {}
    for prim in stage.Traverse():
        nl = prim.GetName().lower()
        pl = str(prim.GetPath()).lower()
        if "floor" not in nl or not any(t in pl for t in _ROOM_TOKENS):
            continue
        if not prim.IsActive():
            continue
        room_key = None
        for part in prim.GetPath().pathString.split("/"):
            p = part.lower()
            if "floor" in p and any(t in p for t in _ROOM_TOKENS):
                room_key = p.replace("_floor", "")
                break
        if not room_key:
            continue
        mesh = UsdGeom.Mesh(prim)
        pa = mesh.GetPointsAttr() if mesh else None
        if not pa or not pa.HasValue():
            continue
        pts = pa.Get()
        if not pts:
            continue
        wxf = UsdGeom.XformCache().GetLocalToWorldTransform(prim)
        bucket = rooms.setdefault(room_key, [])
        for p in pts:
            wp = wxf.Transform(Gf.Vec3d(float(p[0]), float(p[1]), float(p[2])))
            bucket.append([float(wp[0]), float(wp[1])])
    polys = []
    for xy in rooms.values():
        if len(xy) >= 3:
            h = _convex_hull_xy(xy)
            if len(h) >= 3:
                polys.append(h)
    return polys


def _dist_to_poly_edge(px, py, poly):
    """Min distance from (px,py) to any edge of poly (XY). inf if degenerate."""
    n = len(poly)
    best = float("inf")
    for i in range(n):
        ax_, ay_ = poly[i]
        bx_, by_ = poly[(i + 1) % n]
        dx, dy = bx_ - ax_, by_ - ay_
        seg2 = dx*dx + dy*dy
        if seg2 <= 1e-9:
            continue
        t = max(0.0, min(1.0, ((px-ax_)*dx + (py-ay_)*dy) / seg2))
        cx, cy = ax_ + t*dx, ay_ + t*dy
        d = ((px-cx)**2 + (py-cy)**2) ** 0.5
        if d < best:
            best = d
    return best


def inside_any_room(px, py, polys, inset=0.0):
    """True if (px,py) is safely inside some room polygon.

    `inset` > 0 shrinks the walkable region INWARD: the point must be inside a
    polygon AND at least `inset` metres from its nearest edge. This keeps the
    agent's collision capsule (and FPV camera, both centred on the point) clear
    of "void wall" floor-hull edges where geometry is missing, preventing the
    camera from peeking into the unlit exterior (black frames). With inset=0 it
    is a plain point-in-polygon test.
    """
    if not polys:
        return True  # no polygons -> gate disabled, never block
    for poly in polys:
        if _point_in_polygon_xy(px, py, poly):
            if inset <= 0:
                return True
            # Inside this polygon: require clearance from its boundary.
            if _dist_to_poly_edge(px, py, poly) >= inset:
                return True
            # Inside but too close to the edge -> treat as outside (blocked).
    return False


def debug_spawn_settle(query_if, ax, ay, sim_app, carb, steps=120):
    """Diagnostic (SPAWN_DEBUG=1, default OFF): trace dynamic furniture settling
    onto the spawn. Steps the timeline and probes overlap at the spawn over a
    z-scan, revealing WHEN/WHERE a falling collider (e.g. a mattress) reaches the
    agent — the case11_bedroom_lift embed bug, where spawn-check passes pre-settle
    but a dynamic rigid body drops onto the spawn within a few physics steps.
    No-op on normal runs."""
    if os.environ.get("SPAWN_DEBUG") != "1":
        return
    try:
        from pxr import UsdGeom, Gf, UsdPhysics, UsdGeom as _ug
        import omni.usd, omni.timeline as _tl
        stage = omni.usd.get_context().get_stage()
        zlist = [0.1, 0.3, 0.5, 0.7, 0.9, 1.0, 1.3, 1.6]

        # Track FULL pose (translation + rotation) + velocities of dynamic
        # furniture. Reading only translation misses TUMBLING (centroid fixed,
        # orientation changing). Velocity reveals whether the body is moving and
        # whether it starts with a non-zero initial velocity (penetration kick).
        track = [p for p in stage.Traverse()
                 if any(t in p.GetName().lower() for t in ("mattress", "pillow"))
                 and p.HasAPI(UsdPhysics.RigidBodyAPI)]
        bbc = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])

        def _pose(prim):
            m = UsdGeom.XformCache().GetLocalToWorldTransform(prim)
            t = m.ExtractTranslation()
            r = m.ExtractRotation()             # axis-angle
            ax_, ang = r.GetAxis(), r.GetAngle()
            # velocities (USD physics attrs, set by sim)
            rb = UsdPhysics.RigidBodyAPI(prim)
            lv = rb.GetVelocityAttr().Get() if rb.GetVelocityAttr() else None
            av = rb.GetAngularVelocityAttr().Get() if rb.GetAngularVelocityAttr() else None
            return (float(t[0]), float(t[1]), float(t[2]),
                    (float(ax_[0]), float(ax_[1]), float(ax_[2])), float(ang), lv, av)

        def _probe(tag):
            cells = "".join(
                "X" if query_if.overlap_sphere_any(0.40, carb.Float3(ax, ay, sz))
                else "." for sz in zlist)
            h = query_if.sweep_sphere_closest(0.40, carb.Float3(ax, ay, 0.5),
                                              carb.Float3(1, 0, 0), 0.05)
            nm = ((h.get("rigidBody") or h.get("collider") or "").split("/")[-1][:34]
                  if h["hit"] else "")
            log(f"[SPAWN_DEBUG] {tag:14s} z-overlap[{cells}] sweep@0.5={nm}")
            for p in track:
                x, y, z, axis, ang, lv, av = _pose(p)
                bb = bbc.ComputeWorldBound(p).ComputeAlignedRange()
                bs = (f"bbox z[{bb.GetMin()[2]:.2f},{bb.GetMax()[2]:.2f}]"
                      if not bb.IsEmpty() else "bbox=EMPTY")
                log(f"[SPAWN_DEBUG]   {p.GetName()[:30]:30s} "
                    f"pos=({x:.2f},{y:.2f},{z:.2f}) rot={ang:.1f}deg@({axis[0]:.1f},{axis[1]:.1f},{axis[2]:.1f}) "
                    f"v={lv} av={av} {bs}")

        tli = _tl.get_timeline_interface()
        _probe("t=0(prephys)")
        tli.play()
        for k in range(1, steps + 1):
            sim_app.update()
            if k in (1, 2, 3, 5, 10, 20, 60, 120):
                _probe(f"after_{k}_steps")
        tli.stop()
        _probe("after_stop")
    except Exception as e:
        log(f"[SPAWN_DEBUG] error: {e}")


log(f"[BENCH] Task={tid} Level={level} Scene={task['scene_dir']}")
log(f"[BENCH] Instruction: {task['instruction']}")
log(f"[BENCH] Model: {MODEL_NAME}")
log(f"[BENCH] Phases: {len(phases)}, MaxSteps={max_steps}")

# ── VLM query ──
ALL_ACTIONS = ["MOVE_FORWARD","TURN_LEFT","TURN_RIGHT","DONE","PAUSE","PICK_UP","PUT_DOWN","TURN_ON","TILT_UP","TILT_DOWN"]
# STOP is accepted as a back-compat alias for DONE (older prompts / task JSON
# / VLM drift). It is rewritten to DONE before any downstream code sees it.
_PARSE_ALIASES = {"STOP": "DONE"}
ACTION_RE = re.compile(r"ACTION:\s*(" + "|".join(ALL_ACTIONS + list(_PARSE_ALIASES)) + r")", re.IGNORECASE)
# Multi-action plan: "PLAN: MOVE_FORWARD, MOVE_FORWARD, TURN_LEFT, ..."
PLAN_RE = re.compile(r"PLAN:\s*(.+)", re.IGNORECASE)
PLAN_LEN = 5  # max actions the VLM may queue per call

def strip_thinking(text):
    """Strip <think>...</think> reasoning from thinking-model outputs.
    Handles both '<think>content</think>answer' and 'content</think>answer'
    (Qwen3-Thinking omits the opening tag). Returns the answer portion only.
    If no </think> tag is found, returns text unchanged (non-thinking model)."""
    idx = text.find("</think>")
    if idx >= 0:
        return text[idx + len("</think>"):].strip()
    return text

def query_vlm(img_paths, prompt, system_prompt, step=0):
    """Query the VLM with one or more images. Uses _vlm_request for
    retry + single-image fallback (defined after this function)."""
    try:
        raw_text = _vlm_request(img_paths, prompt, system_prompt, step)
        text = strip_thinking(raw_text)
        m = ACTION_RE.search(text.upper())
        if m:
            a = m.group(1).upper()
            return _PARSE_ALIASES.get(a, a), False
        best, bi = "MOVE_FORWARD", -1
        for a in ALL_ACTIONS + list(_PARSE_ALIASES):
            i = text.upper().rfind(a)
            if i > bi: bi, best = i, a
        return _PARSE_ALIASES.get(best, best), True
    except Exception as e:
        log(f"[VLM] Error: {e}"); return "MOVE_FORWARD", True


MAX_VLM_RETRIES = int(os.environ.get("MAX_VLM_RETRIES", "8"))  # retry budget for 429s

def _vlm_request(img_paths, prompt, system_prompt, step=0):
    """Low-level VLM HTTP request with retry + single-image fallback.
    Returns raw response text or raises on exhausted retries."""
    if isinstance(img_paths, str):
        img_paths = [img_paths]

    img_content = []
    for ip in img_paths:
        with open(ip, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        img_content.append({"type":"image_url","image_url":{"url":f"data:image/png;base64,{b64}"}})

    headers = {"Content-Type":"application/json"}
    if VLM_API_KEY:
        headers["Authorization"] = f"Bearer {VLM_API_KEY}"

    for attempt in range(MAX_VLM_RETRIES):
        payload = {"model": MODEL_NAME, "max_tokens": 4096, "temperature": 0.6,
                   "messages": [{"role":"system","content":system_prompt},
                                {"role":"user","content":
                                    img_content +
                                    [{"type":"text","text":prompt}]}]}
        req = urllib.request.Request(VLLM_URL, json.dumps(payload).encode(),
                                    headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                content = data["choices"][0]["message"]["content"]
                if not content:
                    log(f"[VLM] Empty/null content in 200 response (attempt {attempt+1}/{MAX_VLM_RETRIES}), retrying...")
                    import time as _t; _t.sleep(3)
                    continue
                raw_text = content.strip()
            resp_log = os.path.join(RUN_DIR, "vlm_responses.jsonl")
            with open(resp_log, "a") as f:
                f.write(json.dumps({"step":step,"response":raw_text}) + "\n")
            return raw_text
        except urllib.error.HTTPError as e:
            body = ""
            try: body = e.read().decode()[:300]
            except: pass
            if e.code == 429:
                wait = min(5 * (attempt + 1), 30)
                log(f"[VLM] 429 rate-limited (attempt {attempt+1}/{MAX_VLM_RETRIES}), "
                    f"retrying in {wait}s...")
                import time as _t; _t.sleep(wait)
                continue
            elif e.code == 400 and "at most 1 image" in body and len(img_content) > 1:
                log(f"[VLM] Model supports 1 image only, falling back to single frame")
                img_content = [img_content[-1]]
                continue
            else:
                raise
    raise RuntimeError(f"VLM retries exhausted after {MAX_VLM_RETRIES} attempts")

def query_vlm_plan(img_paths, prompt, system_prompt, step=0):
    """Query the VLM for a SEQUENCE of up to PLAN_LEN actions. Returns
    (actions_list, fallback_bool). Supports 3-frame temporal context
    with automatic single-image fallback and 429 retry."""
    try:
        raw_text = _vlm_request(img_paths, prompt, system_prompt, step)
        text = strip_thinking(raw_text)
        up = text.upper()
        for alias, canonical in _PARSE_ALIASES.items():
            up = up.replace(alias, canonical)
        m = PLAN_RE.search(up)
        if m:
            tokens = re.split(r"[,\s]+", m.group(1).strip())
            plan = [_PARSE_ALIASES.get(t, t) for t in tokens if t in ALL_ACTIONS or t in _PARSE_ALIASES]
            if plan:
                return plan[:PLAN_LEN], False
        am = ACTION_RE.search(up)
        if am:
            a = am.group(1).upper()
            return [_PARSE_ALIASES.get(a, a)], False
        found = sorted(((up.find(a), a) for a in ALL_ACTIONS if up.find(a) >= 0))
        if found:
            return [a for _, a in found][:PLAN_LEN], True
        return ["MOVE_FORWARD"], True
    except Exception as e:
        log(f"[VLM] Error: {e}"); return ["MOVE_FORWARD"], True

# ── Main ──
try:
    from isaacsim import SimulationApp
    sim_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})
    import omni.usd, omni.replicator.core as rep
    from omni.isaac.core.utils.stage import open_stage, is_stage_loading
    from pxr import Gf, Usd, UsdGeom, UsdLux

    # ── Load scene ──
    sf = discover_scene_files(scene_dir)
    assert sf["stage"], f"No stage in {scene_dir}"
    spec = json.load(open(sf["spec"]))
    humans_spec = spec.get("humans", [])
    active_humans = spec.get("active_humans", [])
    anim_fps = float(spec.get("stage",{}).get("time_codes_per_second", 10.0))

    log(f"[BENCH] Loading stage: {sf['stage']}")
    open_stage(sf["stage"])
    while is_stage_loading(): sim_app.update()
    stage = omni.usd.get_context().get_stage()
    log("[BENCH] Stage loaded")

    # ── Hide ceiling SURFACE for bird's-eye view ──
    # IMPORTANT: Only hide the actual ceiling surface mesh, NOT CeilingLightFactory
    # prims which are light fixtures that illuminate the room interior.
    for p in stage.Traverse():
        pname = p.GetName().lower()
        ppath = str(p.GetPath()).lower()
        # Only hide actual ceiling/roof surface geometry, skip light fixtures
        if ("ceiling" in pname or "roof" in pname) and "light" not in pname and "lamp" not in pname:
            try: UsdGeom.Imageable(p).MakeInvisible()
            except: pass

    # ── Strip DomeLight sky textures to prevent colored sky bleed ──
    # The DomeLight at /World/Env/env_light may have an HDR sky texture
    # that bleeds red/blue/green through the ceiling void in bird-eye view.
    # We strip only the texture (the sky image); the DomeLight's original
    # color and intensity are preserved to keep scene-authentic illumination.
    # (Do NOT force color=white — that changes material appearance, e.g.
    # case03 door turns from black to red under uniform white lighting.)
    for p in stage.Traverse():
        if p.GetTypeName() == "DomeLight":
            pp = p.GetPath().pathString
            if pp.startswith("/World/Env/"):
                dl = UsdLux.DomeLight(p)
                tex_attr = dl.GetTextureFileAttr()
                if tex_attr and tex_attr.Get():
                    tex_attr.Clear()
                    log(f"[BENCH] Cleared sky texture on DomeLight {pp} (preserving original color)")

    # ── Get runner scale from spec ──
    runner_scale = [0.53, 0.53, 0.53]
    runner_root_off = [0, 0, 0.53]
    runner1_spec = None
    runner1_binding = {}
    for ah in active_humans:
        if "run" in ah.get("name",""):
            runner1_binding = ah.get("animation_binding",{})
            runner_scale = runner1_binding.get("scale_xyz", runner_scale)
            runner_root_off = runner1_binding.get("root_offset_m", runner_root_off)
            break
    for h in humans_spec:
        if "run" in h.get("name",""):
            runner1_spec = h; break
    # Collect ALL runner specs for multi-runner scenes
    all_runner_specs = [h for h in humans_spec if "run" in h.get("name","")]

    GROUND_Z = RUNNER_MESH_GROUND_Z  # Use bbox-calibrated constant

    # ── Resolve target positions for each phase ──
    resolved_targets = []
    resolved_half_extents = []  # XY half-extent per phase for edge-distance
    pickup_prim_path = None
    target_prim_paths = set()
    target_classes = set()
    for ph in phases:
        tobj = ph["target_object"]
        target_classes.add(tobj)
        if tobj.startswith("__human_"):
            # Use human initial position
            idx = int(tobj.replace("__human_","").replace("__",""))
            if idx < len(humans_spec):
                pos = humans_spec[idx].get("placement_location_m",[0,0,0])
                resolved_targets.append([pos[0], pos[1]])
            else:
                resolved_targets.append([0, 0])
            resolved_half_extents.append(0.0)  # humans have no bbox
            log(f"[BENCH] Phase '{ph['name']}' -> human[{idx}] at {resolved_targets[-1]}")
        elif tobj == "door":
            # Find door prim
            dp = find_prim_by_factory(stage, "door")
            if dp:
                target_prim_paths.add(dp)
                c = get_prim_world_center(stage, dp)
                resolved_targets.append(c[:2] if c else [6, 11])
            else:
                resolved_targets.append([6, 11])
            resolved_half_extents.append(0.0)  # door half-extent not critical
            log(f"[BENCH] Phase '{ph['name']}' -> door at {resolved_targets[-1]}")
        else:
            # Resolve to the REAL active geometry via the shared resolver (single
            # source of truth shared with the validator + generator). NEVER fall back
            # to a placeholder coordinate — a target that cannot be resolved is a
            # broken task, so fail loud (caught by the top-level FATAL handler).
            res = resolve_target(stage, ph)
            if res is None:
                log(f"[BENCH] ERROR: unresolvable target for phase '{ph['name']}' "
                    f"(target_object={tobj}, target_prim={ph.get('target_prim','')}) "
                    f"— no ACTIVE prim with a valid bbox. Aborting (fail-loud).")
                raise RuntimeError(
                    f"unresolvable target: phase={ph['name']} object={tobj} "
                    f"prim={ph.get('target_prim','')}")
            pp = res["prim_path"]
            target_prim_paths.add(pp)
            resolved_targets.append(res["center"][:2])
            # For pickups on furniture, success is measured to the support's EDGE: use the
            # larger of the object half-extent and the baked reach_half_extent (support).
            he = max(res["half_extent_xy"], float(ph.get("reach_half_extent", 0.0)))
            resolved_half_extents.append(he)
            log(f"[BENCH] Phase '{ph['name']}' -> {tobj} prim={pp} "
                f"center={res['center'][:2]} half_ext={he:.2f}m")
            # Track pickup prims
            if ph["action"] == "PICK_UP" and not pickup_prim_path:
                pickup_prim_path = pp

    # ── Auto-spawn: compute agent start from scene geometry ──
    # Strategy: collect all prop centers as "room interior" samples, then
    # pick a point 3-6m from the first target that is still within the
    # bounding box of the scene interior (padded inward by 1m).
    # Actual ground-hit verification happens later via spawn_nudge.
    if auto_spawn and resolved_targets:
        import math, random as _rng
        _rng.seed(hash(tid) & 0xFFFFFFFF)  # deterministic per task
        tgt0 = resolved_targets[0]

        # Collect all known interior points (prop centers + resolved targets)
        interior_pts = list(resolved_targets)
        # Also add prop centers from the stage traverse we already did
        for p in stage.Traverse():
            if p.GetTypeName() in ("Mesh", "Xform") and "/World/Env/" in p.GetPath().pathString:
                try:
                    imageable = UsdGeom.Imageable(p)
                    bound = imageable.ComputeWorldBound(Usd.TimeCode.Default(), "default")
                    box = bound.GetBox()
                    cn = box.GetMidpoint()
                    if abs(cn[0]) < 100 and abs(cn[1]) < 100:  # sanity
                        interior_pts.append([cn[0], cn[1]])
                except:
                    pass

        if len(interior_pts) >= 2:
            xs = [p[0] for p in interior_pts]
            ys = [p[1] for p in interior_pts]
            # Scene bounding box with 1m inward padding
            x_min, x_max = min(xs) + 1.0, max(xs) - 1.0
            y_min, y_max = min(ys) + 1.0, max(ys) - 1.0
            if x_min >= x_max: x_min, x_max = min(xs), max(xs)
            if y_min >= y_max: y_min, y_max = min(ys), max(ys)
            scene_cx = (x_min + x_max) / 2
            scene_cy = (y_min + y_max) / 2
        else:
            x_min, x_max = tgt0[0] - 6, tgt0[0] + 6
            y_min, y_max = tgt0[1] - 6, tgt0[1] + 6
            scene_cx, scene_cy = tgt0[0], tgt0[1]

        # Try up to 20 random spawn candidates within scene bounds
        best_spawn = None
        for attempt in range(20):
            spawn_dist = 3.0 + _rng.random() * 4.0  # 3-7m from target
            angle = _rng.uniform(0, 2 * math.pi)
            sx = tgt0[0] + spawn_dist * math.cos(angle)
            sy = tgt0[1] + spawn_dist * math.sin(angle)
            # Clamp to scene bounds
            sx = max(x_min, min(x_max, sx))
            sy = max(y_min, min(y_max, sy))
            # Check distance from target after clamping
            actual_dist = math.sqrt((sx - tgt0[0])**2 + (sy - tgt0[1])**2)
            if actual_dist >= 2.0:  # at least 2m from target
                best_spawn = (sx, sy, actual_dist)
                break
        
        if not best_spawn:
            # Fallback: spawn at scene centroid (guaranteed to be in room)
            sx, sy = scene_cx, scene_cy
            actual_dist = math.sqrt((sx - tgt0[0])**2 + (sy - tgt0[1])**2)
            best_spawn = (sx, sy, actual_dist)

        sx, sy, actual_dist = best_spawn
        face_yaw = math.degrees(math.atan2(tgt0[1] - sy, tgt0[0] - sx))
        if spawn_facing == 'back':
            agent_start_yaw = face_yaw + 180
        else:
            agent_start_yaw = face_yaw
        agent_start_xy = [sx, sy]
        log(f"[BENCH] Auto-spawn: ({sx:.2f},{sy:.2f}) yaw={agent_start_yaw:.0f} "
            f"(dist={actual_dist:.1f}m from target, facing={spawn_facing}, "
            f"scene_bounds=[{x_min:.1f},{x_max:.1f}]x[{y_min:.1f},{y_max:.1f}])")

    # ── Place objects that need repositioning ──
    pickup_prim = None
    for i, ph in enumerate(phases):
        if ph.get("place_at"):
            pa = ph["place_at"]
            tobj = ph["target_object"]
            pp = find_prim_by_factory(stage, tobj)
            if pp:
                prim = stage.GetPrimAtPath(pp)
                if prim and prim.IsValid():
                    xf = UsdGeom.Xformable(prim)
                    try: xf.ClearXformOpOrder()
                    except: pass
                    attr = prim.GetAttribute("xformOp:translate")
                    if attr.IsValid():
                        if attr.GetTypeName().type == Gf.Vec3f:
                            attr.Set(Gf.Vec3f(pa[0], pa[1], pa[2]))
                        else:
                            attr.Set(Gf.Vec3d(pa[0], pa[1], pa[2]))
                        xf.SetXformOpOrder([UsdGeom.XformOp(attr)])
                    else:
                        xf.AddTranslateOp().Set(Gf.Vec3d(pa[0], pa[1], pa[2]))
                    resolved_targets[i] = [pa[0], pa[1]]
                    if ph["action"] == "PICK_UP":
                        pickup_prim = prim; pickup_prim_path = pp
                    log(f"[BENCH] Placed {tobj} at {pa}")

    # ── Guarantee Target Uniqueness (by SEMANTIC CLASS) ──
    # 0527_worldenv_invisible: extended to /World/Env/ in addition to
    # /World/InteractiveProps. Scene-built furniture under /World/Env/ was
    # previously missed, allowing duplicate bookshelves etc. to remain visible
    # and confuse VLM navigation instructions.
    #
    # An instruction like "go to the bookshelf" is ambiguous if the scene has
    # several shelf-like factories (SimpleBookcase, CellShelf, LargeShelf, ...).
    # We deactivate every prop in the SAME SEMANTIC CLASS as a target that is
    # not itself a target prim — so exactly one instance of that class remains.
    # Semantic class (not raw factory name) is the disambiguation unit; see
    # semantic_classes.py. This scales: new scenes just need their factories
    # mapped there, no per-asset manual inspection.
    target_semantic = {semantic_class_of(tc) for tc in target_classes}
    log(f"[BENCH] Target semantic classes: {target_semantic}")
    log(f"[BENCH] Target prim paths: {target_prim_paths}")

    # Prefer a generation-baked deactivation list (decided once against real geometry:
    # same-semantic-class non-targets + resting clutter, never a pickup's support). When
    # present, the runtime just executes it and skips the legacy runtime dedup/cascade.
    baked_deact = task.get("deactivate_prims")
    if baked_deact is not None:
        applied = 0
        for dp in baked_deact:
            prim = stage.GetPrimAtPath(dp)
            if prim and prim.IsValid():
                prim.SetActive(False); applied += 1
        log(f"[BENCH] Applied baked deactivate_prims ({applied}/{len(baked_deact)}); "
            f"skipping runtime dedup")

    # First pass: identify which prims to deactivate (legacy; skipped when baked list used)
    prims_to_deactivate = []
    for container_path in ([] if baked_deact is not None else ["/World/InteractiveProps", "/World/Env"]):
        container = stage.GetPrimAtPath(container_path)
        if not container or not container.IsValid():
            log(f"[BENCH] Container {container_path} NOT FOUND or invalid — skipping")
            continue
        children = container.GetChildren()
        log(f"[BENCH] Scanning {container_path}: {len(children)} children")
        for child in children:
            c_name = child.GetName()
            c_path = child.GetPath().pathString
            child_semantic = semantic_class_of(c_name)
            if child_semantic in target_semantic and c_path not in target_prim_paths:
                prims_to_deactivate.append(child)

    # Second pass: for each deactivated prim, also deactivate objects resting on it.
    # When a shelf is deactivated, objects on it lose support and fall to ground,
    # creating impassable ground-level collision obstacles (the "falling lamp" bug).
    cascade_deactivated = []
    deactivate_paths = {p.GetPath().pathString for p in prims_to_deactivate}
    for deact_prim in prims_to_deactivate:
        try:
            deact_img = UsdGeom.Imageable(deact_prim)
            deact_bound = deact_img.ComputeWorldBound(Usd.TimeCode.Default(), "default")
            deact_box = deact_bound.GetBox()
            dmin = deact_box.GetMin()
            dmax = deact_box.GetMax()
            # Skip tiny prims (not furniture)
            if (dmax[0]-dmin[0]) < 0.3 or (dmax[1]-dmin[1]) < 0.3:
                continue
            # Find all OTHER prims whose center is within the deactivated prim's
            # XY footprint and whose Z is above the prim's bottom (resting on it)
            for container_path in ["/World/InteractiveProps", "/World/Env"]:
                container = stage.GetPrimAtPath(container_path)
                if not container or not container.IsValid():
                    continue
                for child in container.GetChildren():
                    cp = child.GetPath().pathString
                    if cp in deactivate_paths or cp in target_prim_paths:
                        continue  # already deactivating or is a target
                    if cp in [c.GetPath().pathString for c in cascade_deactivated]:
                        continue  # already cascade-marked
                    try:
                        ci = UsdGeom.Imageable(child)
                        cb = ci.ComputeWorldBound(Usd.TimeCode.Default(), "default")
                        cbox = cb.GetBox()
                        cc = cbox.GetMidpoint()
                        # XY center within deactivated prim's footprint
                        xy_inside = (dmin[0] <= cc[0] <= dmax[0] and
                                     dmin[1] <= cc[1] <= dmax[1])
                        # Z above deactivated prim's bottom, within its height + 1m
                        z_on_top = (dmin[2] <= cc[2] <= dmax[2] + 1.0)
                        if xy_inside and z_on_top:
                            cascade_deactivated.append(child)
                    except:
                        pass
        except:
            pass

    # Deactivate everything
    for p in prims_to_deactivate:
        p.SetActive(False)
        log(f"[BENCH] Deactivated same-semantic-class ({semantic_class_of(p.GetName())}) "
            f"non-target: {p.GetPath().pathString}")
    for p in cascade_deactivated:
        p.SetActive(False)
        log(f"[BENCH] Cascade-deactivated (resting on deactivated furniture): "
            f"{p.GetPath().pathString}")

    # ── Instance agent ──
    human_usd = sf["human_usds"][0] if sf["human_usds"] else None
    agent_prim = stage.DefinePrim("/World/Humans/agent_runner")
    if human_usd:
        agent_prim.GetReferences().AddReference(human_usd)
    sim_app.update()

    # ── Setup dancer (if present) — scale to match runner, bbox-calibrated Z ──
    for dname in ["obj_2_dance_anim_2", "obj_2__dance__anim_2"]:
        dp = stage.GetPrimAtPath(f"/World/Humans/{dname}")
        if dp and dp.IsValid():
            dxf = UsdGeom.Xformable(dp)
            try: dxf.ClearXformOpOrder()
            except: pass
            d_pos, d_rot = [2.34, 2.13, 1.18], [0, 0, 132.7]
            for ah in active_humans:
                if "dance" in ah.get("name",""):
                    db = ah.get("animation_binding",{})
                    d_pos = db.get("placement_location_m", d_pos)
                    d_rot = db.get("rotation_deg_xyz", d_rot)
                    break
            dxf.AddTranslateOp().Set(Gf.Vec3d(d_pos[0], d_pos[1], DANCER_MESH_GROUND_Z))
            dyr = math.radians(d_rot[2])
            dxf.AddOrientOp().Set(Gf.Quatf(math.cos(dyr/2), 0, 0, math.sin(dyr/2)))
            dxf.AddScaleOp().Set(Gf.Vec3d(*runner_scale))
            log(f"[BENCH] Dancer: scale={runner_scale}, Z={DANCER_MESH_GROUND_Z:.4f}")
            break

    # ── Scene geometry bounds (used by bird-eye camera placement) ──
    all_c = [o["center"][:2] for o in spec.get("scene_objects", [{}]) if isinstance(o.get("center"), list)]

    # ── Tame built-in scene lights ──
    # Some scenes (e.g. case02) ship with PointLampFactory lights at
    # absurd intensities (~500M).  Cap them to a sane maximum so they
    # don't blow out the whole image under PathTracing.
    SCENE_LIGHT_CAP = 100000.0          # max intensity for any built-in light
    for p in stage.Traverse():
        ptype = p.GetTypeName()
        if "Light" not in ptype:
            continue
        int_attr = p.GetAttribute("inputs:intensity")
        if int_attr and int_attr.IsValid():
            orig = int_attr.Get()
            if orig is not None and orig > SCENE_LIGHT_CAP:
                int_attr.Set(SCENE_LIGHT_CAP)
                log(f"[BENCH] Capped scene light {p.GetPath()} from {orig:.0f} → {SCENE_LIGHT_CAP:.0f}")

    # ── Fill lights — supplement built-in CeilingLightFactory lights ──
    if all_c:
        x_min, x_max = min(c[0] for c in all_c), max(c[0] for c in all_c)
        y_min, y_max = min(c[1] for c in all_c), max(c[1] for c in all_c)
        cx, cy = (x_min + x_max)/2, (y_min + y_max)/2
        dx, dy = max(2, (x_max - x_min)/4), max(2, (y_max - y_min)/4)
        light_positions = [
            (cx, cy, 2.3), (cx-dx, cy-dy, 2.3), (cx+dx, cy-dy, 2.3),
            (cx-dx, cy+dy, 2.3), (cx+dx, cy+dy, 2.3)
        ]
    else:
        lx, ly = agent_start_xy[0], agent_start_xy[1]
        light_positions = [(lx, ly, 2.3), (lx-2, ly, 2.3), (lx+2, ly, 2.3), (lx, ly-2, 2.3), (lx, ly+2, 2.3)]

    for i, lp in enumerate(light_positions):
        lt = UsdLux.SphereLight.Define(stage, f"/World/Lights/BenchLight_{i}")
        lt.CreateIntensityAttr().Set(80000.0)
        lt.CreateRadiusAttr().Set(0.3)
        xf = UsdGeom.Xformable(lt); xf.ClearXformOpOrder()
        xf.AddTranslateOp().Set(Gf.Vec3d(*lp))

    log("[BENCH] Added 5 fill lights at intensity=80000.0")

    # (Ceiling already hidden above — no duplicate needed)

    # ── Warm up + Rendering ──
    for _ in range(100): sim_app.update()
    import omni.kit.commands
    omni.kit.commands.execute("ChangeSetting", path="/rtx/rendermode", value="PathTracing")
    omni.kit.commands.execute("ChangeSetting", path="/rtx/pathtracing/spp", value=16)

    # ── Camera helpers ──
    def cam_quat(yaw_deg, pitch_deg=0.0):
        yr, pr = math.radians(yaw_deg), math.radians(pitch_deg)
        eye = Gf.Vec3d(0,0,0)
        tgt = Gf.Vec3d(math.cos(yr)*math.cos(pr), math.sin(yr)*math.cos(pr), math.sin(pr))
        mat = Gf.Matrix4d().SetLookAt(eye, tgt, Gf.Vec3d(0,0,1))
        qd = mat.GetInverse().ExtractRotation().GetQuat()
        return Gf.Quatf(qd.GetReal(), *qd.GetImaginary())

    def cam_lookat(pos, target):
        mat = Gf.Matrix4d().SetLookAt(pos, target, Gf.Vec3d(0,0,1))
        qd = mat.GetInverse().ExtractRotation().GetQuat()
        return Gf.Quatf(qd.GetReal(), *qd.GetImaginary())

    # ── FPV camera ──
    # Decision folders: exactly ONE frame per agent step — the image the VLM
    #   sees (rgb_NNNN.png numbered by step).
    # Smooth folders: EVERY rendered frame, including filler frames captured
    #   while the VLM is thinking, so runner motion is continuous (no leaps).
    # The BasicWriter writes into scratch dirs; render_and_capture() moves each
    # frame out, so the writer's auto-incrementing names never leak into the
    # decision/smooth folders.
    fpv_dir = os.path.join(RUN_DIR, "vlm_nav_frames_fpv")
    bird_dir = os.path.join(RUN_DIR, "vlm_nav_frames_bird")
    fpv_smooth_dir = os.path.join(RUN_DIR, "vlm_nav_frames_fpv_smooth")
    bird_smooth_dir = os.path.join(RUN_DIR, "vlm_nav_frames_bird_smooth")
    fpv_scratch = os.path.join(RUN_DIR, "_scratch_fpv")
    bird_scratch = os.path.join(RUN_DIR, "_scratch_bird")
    import shutil
    _dirs = [fpv_dir, bird_dir, fpv_smooth_dir, fpv_scratch, bird_scratch]
    if ENABLE_BIRD_SMOOTH:           # bird _smooth folder only when enabled
        _dirs.append(bird_smooth_dir)
    for d in _dirs:
        if os.path.exists(d): shutil.rmtree(d)
        os.makedirs(d, exist_ok=True)

    fpv_cam = UsdGeom.Camera.Define(stage, "/World/NavCamera")
    fpv_cam.CreateFocalLengthAttr().Set(17.0)
    fpv_cam.CreateHorizontalApertureAttr().Set(34.0)
    fpv_cam.CreateClippingRangeAttr().Set(Gf.Vec2f(0.01, 10000.0))
    rp_fpv = rep.create.render_product("/World/NavCamera", (RENDER_W, RENDER_H))
    wr_fpv = rep.WriterRegistry.get("BasicWriter")
    wr_fpv.initialize(output_dir=fpv_scratch, rgb=True); wr_fpv.attach([rp_fpv])

    # ── Bird's-eye camera — elevated top-down view of entire room ──
    bird_cam = UsdGeom.Camera.Define(stage, "/World/BirdCamera")
    bird_cam.CreateFocalLengthAttr().Set(12.0)  # Wider angle for top-down
    bird_cam.CreateHorizontalApertureAttr().Set(34.0)
    bird_cam.CreateClippingRangeAttr().Set(Gf.Vec2f(3.5, 10000.0))
    bxf = UsdGeom.Xformable(bird_cam); bxf.ClearXformOpOrder()
    bt = bxf.AddTranslateOp(); bo = bxf.AddOrientOp()
    
    if all_c:
        x_min, x_max = min(c[0] for c in all_c), max(c[0] for c in all_c)
        y_min, y_max = min(c[1] for c in all_c), max(c[1] for c in all_c)
        cx, cy = (x_min + x_max)/2, (y_min + y_max)/2
        bird_pos = Gf.Vec3d(cx, cy, 6.0) # High up in the center
        bird_tgt = Gf.Vec3d(cx+0.01, cy, 0.0) # Look mostly straight down
    else:
        bird_pos = Gf.Vec3d(agent_start_xy[0], agent_start_xy[1], 6.0)
        bird_tgt = Gf.Vec3d(agent_start_xy[0]+0.01, agent_start_xy[1], 0.0)
        
    bt.Set(bird_pos); bo.Set(cam_lookat(bird_pos, bird_tgt))
    rp_bird = rep.create.render_product("/World/BirdCamera", (RENDER_W, RENDER_H))
    wr_bird = rep.WriterRegistry.get("BasicWriter")
    wr_bird.initialize(output_dir=bird_scratch, rgb=True); wr_bird.attach([rp_bird])

    # ── Setup agent + runner xform ops ──
    agent_xf = UsdGeom.Xformable(agent_prim)
    try: agent_xf.ClearXformOpOrder()
    except: pass
    a_trans = agent_xf.AddTranslateOp()
    a_orient = agent_xf.AddOrientOp()

    # Hide agent mesh geometry to prevent blocking FPV camera
    for p in agent_prim.GetChildren():
        if p.GetName() == "SkelRoot":
            for child in p.GetChildren():
                if "Mesh" in child.GetName():
                    UsdGeom.Imageable(child).MakeInvisible()
            break
    a_scale = agent_xf.AddScaleOp()
    a_scale.Set(Gf.Vec3d(*runner_scale))

    # Runner 1 (obstacle)
    r1_ops = {}
    r1_prim = None
    for name_candidate in ["obj_1_run_anim_1", "obj_1__run__anim_1"]:
        r1_prim = stage.GetPrimAtPath(f"/World/Humans/{name_candidate}")
        if r1_prim and r1_prim.IsValid(): break
    if r1_prim and r1_prim.IsValid():
        rxf = UsdGeom.Xformable(r1_prim)
        try: rxf.ClearXformOpOrder()
        except: pass
        r1_ops = {"t": rxf.AddTranslateOp(), "o": rxf.AddOrientOp(), "s": rxf.AddScaleOp()}
        r1_ops["s"].Set(Gf.Vec3d(*runner_scale))
        log(f"[BENCH] Runner1: scale={runner_scale}, GROUND_Z={GROUND_Z:.4f}")

    # Runner 2 (if multi-runner scene)
    r2_ops = {}; runner2_spec = None; r2_prim = None
    if len(all_runner_specs) > 1:
        runner2_spec = all_runner_specs[1]
        for n2 in ["obj_2_run_anim_2", "obj_2__run__anim_2"]:
            r2p = stage.GetPrimAtPath(f"/World/Humans/{n2}")
            if r2p and r2p.IsValid():
                r2_prim = r2p
                r2xf = UsdGeom.Xformable(r2p)
                try: r2xf.ClearXformOpOrder()
                except: pass
                r2_ops = {"t":r2xf.AddTranslateOp(),"o":r2xf.AddOrientOp(),"s":r2xf.AddScaleOp()}
                r2_ops["s"].Set(Gf.Vec3d(*runner_scale))
                log(f"[BENCH] Runner2 found")
                break

    nav_cam = stage.GetPrimAtPath("/World/NavCamera")
    timeline = omni.timeline.get_timeline_interface()
    # IMPORTANT: keep the timeline PAUSED. The runner mesh has a baked
    # xformOp:translate animation (600 keyframes) driven by the timeline.
    # A PLAYING timeline free-runs ~rt_subframes anim-frames during each
    # orchestrator.step() render, so the captured frame reflects a much later
    # timecode than the one we set — that was the obstacle "leap". With the
    # timeline stopped, set_current_time() holds exactly and each render
    # captures precisely the requested timecode.
    timeline.stop()
    anim_start = stage.GetStartTimeCode(); anim_end = stage.GetEndTimeCode()
    anim_dur = (anim_end - anim_start) / max(1.0, anim_fps)

    # ── System prompt ──
    if is_multi:
        sys_prompt = make_multistep_system_prompt(task["instruction"])
    else:
        sys_prompt = make_nav_system_prompt(phases[0]["desc"])

    # ── Smooth-frame capture helpers ──
    # Strict per-step sim-time semantics: every executed step (VLM-call or
    # queued, on-screen or off-screen) advances sim_t by exactly
    # RUNNER_TIME_PER_STEP. On-screen steps render FILLER_FRAMES_PER_STEP
    # filler frames that each correspond to one baked-animation atomic
    # keyframe (FILLER_FPS == anim_fps == 10, so 1 filler frame == 0.1s).
    # Off-screen steps skip the filler renders and advance sim_t directly.
    # The two paths produce IDENTICAL sim_t advancement (0.5s/step), so the
    # runner's world-time is strictly proportional to the agent's step count
    # — independent of VLM wall-clock latency. Fully reproducible.
    FILLER_FPS = 10.0  # match anim fps to avoid trajectory undersampling at corners
    FILLER_SUBFRAMES = 4   # filler frames are video-only -> cheap PathTracing
    DECISION_SUBFRAMES = 16  # decision frames (VLM sees these) stay full quality
    FILLER_FRAMES_PER_STEP = int(RUNNER_TIME_PER_STEP * FILLER_FPS)  # = 5
    smooth_counter = [0]  # global running index for *_smooth folders
    # ── Timing probes ── accumulate wall-clock seconds by category so the log
    # can show where each episode actually spends its time (render vs VLM).
    timing = {"render_decision": 0.0, "render_filler": 0.0, "vlm": 0.0,
              "visibility_check": 0.0, "n_decision": 0, "n_filler": 0,
              "n_vlm": 0, "n_runner_visible": 0, "n_runner_hidden": 0}

    # Collision/event bookkeeping. Collisions are process-quality metrics:
    # they block the attempted move, but final success is still goal completion.
    collision_events = []
    collision_counts = {
        "static_obstacle": 0,
        "dynamic_runner_predicted": 0,
        "agent_pushed_events": 0,
        "agent_pushed_frames": 0,
    }

    def _xy(v):
        return [round(float(v[0]), 3), round(float(v[1]), 3)]

    def record_collision_event(ev):
        ev = dict(ev)
        if "sim_t" in ev:
            ev["sim_t"] = round(float(ev["sim_t"]), 3)
        collision_events.append(ev)

    # XY circle approximation for agent vs runner. SAFE_RADIUS is the minimum
    # center-to-center distance: if a runner's baked pose would land closer
    # than this, the agent is pushed outward (push_agent_if_overlap) — Zehao's
    # runner trajectories have no collision check, so we enforce mutual
    # exclusion here. DYNAMIC_* is the forward-path prediction used by
    # MOVE_FORWARD to refuse moves that would step into a runner's future XY.
    AGENT_RADIUS = 0.40
    RUNNER_RADIUS = 0.35
    SAFE_RADIUS = AGENT_RADIUS + RUNNER_RADIUS
    DYNAMIC_COLLISION_MARGIN = 0.10
    DYNAMIC_COLLISION_RADIUS = SAFE_RADIUS + DYNAMIC_COLLISION_MARGIN
    DYNAMIC_CHECK_SAMPLES = 5
    # First frame of an ongoing contact is recorded as an "event"; subsequent
    # frames advance "frames" but not "events" until contact breaks.
    agent_push_active = {"runner1": False, "runner2": False}
    # ── Runner freeze state (corner deadlock resolution) ──
    # When a runner can't be pushed away from the agent without clipping
    # through a wall (corner deadlock), the runner freezes at its last valid
    # position until its baked trajectory naturally exits the conflict zone.
    runner_frozen = {"runner1": False, "runner2": False}
    runner_frozen_pos = {"runner1": None, "runner2": None}

    def baked_runner_xy_at(t):
        """Effective XY of each runner at sim-time t. Returns frozen position
        when a runner is in deadlock freeze, otherwise the baked trajectory."""
        if runner1_spec:
            rp1 = (runner_frozen_pos["runner1"] if runner_frozen["runner1"]
                   else sample_human_motion(runner1_spec, t, anim_fps)[0])
        else:
            rp1 = None
        if runner2_spec:
            rp2 = (runner_frozen_pos["runner2"] if runner_frozen["runner2"]
                   else sample_human_motion(runner2_spec, t, anim_fps)[0])
        else:
            rp2 = None
        return rp1, rp2

    def push_agent_if_overlap(ax, ay, t, step=None, frame_kind="decision"):
        """Resolve agent-runner overlap with wall-aware push + deadlock freeze.

        1. Normal: push agent radially away from runner.
        2. Wall blocks push: wall-slide (push perpendicular along wall).
        3. Corner deadlock (both blocked): freeze runner at last valid pos,
           don't move agent. Runner stays frozen until baked trajectory
           naturally exits SAFE_RADIUS.

        Returns new (ax, ay) and a list of overlap event names."""
        events = []
        for name, spec in (("runner1", runner1_spec), ("runner2", runner2_spec)):
            if not spec:
                if agent_push_active.get(name): agent_push_active[name] = False
                continue

            # Always read baked position to check for unfreeze
            baked_pos = sample_human_motion(spec, t, anim_fps)[0]
            baked_d = math.hypot(ax - baked_pos[0], ay - baked_pos[1])

            # Use effective position (frozen or baked)
            if runner_frozen[name]:
                rp = runner_frozen_pos[name]
                # Unfreeze check: baked trajectory left the conflict zone
                if baked_d >= SAFE_RADIUS:
                    runner_frozen[name] = False
                    rp = baked_pos  # resume baked trajectory
                    runner_frozen_pos[name] = list(baked_pos)
                    log(f"[BENCH] {name} unfrozen — baked trajectory exited conflict zone")
                # else: still frozen — use frozen_pos as rp and fall through
                # to overlap check below (handles agent being pushed toward
                # frozen runner by another runner)
            else:
                rp = baked_pos

            dxr = ax - rp[0]; dyr = ay - rp[1]
            d = math.hypot(dxr, dyr)
            if d < SAFE_RADIUS:
                if d < 1e-4:
                    nx, ny = 1.0, 0.0
                else:
                    nx, ny = dxr / d, dyr / d
                overlap = SAFE_RADIUS - d

                # ── Wall-slide push resolution ──
                def _sweep_clear(ox, oy, dx, dy, dist):
                    """Return max safe travel distance along (dx,dy).
                    Buffer of 0.15m keeps agent center ≥0.55m from walls,
                    so camera (0.1m forward offset) stays ≥0.45m clear —
                    well beyond the 0.3m near-clip distance. This prevents
                    visual clipping through thin geometry (window frames)
                    when runner push moves the agent toward walls."""
                    try:
                        # EYE_H added: cap push travel at the camera height too,
                        # so a runner push never drives the camera into a wall.
                        for sz in [0.5, 1.0, EYE_H]:
                            h = query_if.sweep_sphere_closest(
                                0.40, carb.Float3(ox, oy, sz),
                                carb.Float3(dx, dy, 0), dist + 0.15)
                            if h["hit"]:
                                wp = (h.get("rigidBody") or
                                      h.get("collider") or "")
                                if is_walkable_hit(wp):
                                    continue
                                return max(float(h.get("distance", 0)) - 0.15, 0.0)
                    except Exception:
                        pass
                    return dist

                # Step 1: push primary direction
                primary = min(overlap, _sweep_clear(ax, ay, nx, ny, overlap))
                remaining = overlap - primary

                # Step 2: wall-slide perpendicular
                slide = 0.0
                if remaining > 0.01:
                    perp1x, perp1y = -ny, nx
                    perp2x, perp2y = ny, -nx
                    dot1 = perp1x * dxr + perp1y * dyr
                    if dot1 >= 0:
                        px, py = perp1x, perp1y
                    else:
                        px, py = perp2x, perp2y
                    slide = min(remaining,
                                _sweep_clear(ax + nx * primary,
                                             ay + ny * primary,
                                             px, py, remaining))
                    remaining -= slide

                # Step 3: deadlock — freeze runner instead of accepting overlap
                if remaining > 0.01:
                    # Can't fully resolve — freeze runner, don't move agent
                    runner_frozen[name] = True
                    if runner_frozen_pos[name] is None:
                        # First freeze: use baked pos clamped to SAFE_RADIUS
                        runner_frozen_pos[name] = list(rp)
                    log(f"[BENCH] {name} FROZEN — corner deadlock "
                        f"(remaining={remaining:.3f}m)")
                    continue  # don't push agent at all

                # Apply resolved push
                ax_before, ay_before = ax, ay
                cand_x = ax + nx * primary
                cand_y = ay + ny * primary
                if slide > 0:
                    cand_x += px * slide
                    cand_y += py * slide

                # ── Soft room-boundary clamp on runner push ──
                # _sweep_clear only stops pushes into REAL wall geometry. At a
                # "void wall" (missing-wall opening, no geometry) it returns clear,
                # so a runner could shove the agent through the soft boundary into
                # the unlit void (case069: residual black frames from runner push,
                # not self-driven MOVE_FORWARD). Mirror the movement-gate inset:
                # if the pushed destination leaves the room footprint, freeze the
                # runner instead of pushing the agent out of bounds.
                if room_polys and not inside_any_room(cand_x, cand_y, room_polys,
                                                      ROOM_BOUNDARY_INSET):
                    runner_frozen[name] = True
                    if runner_frozen_pos[name] is None:
                        runner_frozen_pos[name] = list(rp)
                    log(f"[BENCH] {name} FROZEN — push would cross room boundary "
                        f"(dest=({cand_x:.2f},{cand_y:.2f}))")
                    continue  # don't push agent past the soft boundary

                ax, ay = cand_x, cand_y

                # Update last valid runner position (for future freeze)
                runner_frozen_pos[name] = list(rp)

                was_active = agent_push_active.get(name, False)
                if frame_kind != "prime":
                    collision_counts["agent_pushed_frames"] += 1
                    if not was_active:
                        collision_counts["agent_pushed_events"] += 1
                        record_collision_event({
                            "step": step,
                            "type": "agent_pushed_by_runner",
                            "runner": name,
                            "frame_kind": frame_kind,
                            "sim_t": t,
                            "overlap_m": round(overlap, 3),
                            "agent_xy_before": _xy([ax_before, ay_before]),
                            "agent_xy_after": _xy([ax, ay]),
                            "runner_xy": _xy(rp),
                        })
                agent_push_active[name] = True
                events.append(name)
            else:
                if agent_push_active.get(name): agent_push_active[name] = False
                # Update last valid position when not overlapping
                runner_frozen_pos[name] = list(rp)
        return ax, ay, events

    def pose_runners_at(t):
        """Set runners/dancer to their baked pose at sim-time t.
        When a runner is frozen (corner deadlock), its position stays at the
        frozen position while rotation still follows the baked data."""
        if runner1_spec and r1_ops:
            rp1, rr = sample_human_motion(runner1_spec, t, anim_fps)
            if runner_frozen["runner1"] and runner_frozen_pos["runner1"]:
                pos = runner_frozen_pos["runner1"]
            else:
                pos = rp1
            r1_ops["t"].Set(Gf.Vec3d(pos[0], pos[1], GROUND_Z))
            ryr = math.radians(rr[2])
            r1_ops["o"].Set(Gf.Quatf(math.cos(ryr/2), 0, 0, math.sin(ryr/2)))
        if runner2_spec and r2_ops:
            rp2, rr2 = sample_human_motion(runner2_spec, t, anim_fps)
            if runner_frozen["runner2"] and runner_frozen_pos["runner2"]:
                pos2 = runner_frozen_pos["runner2"]
            else:
                pos2 = rp2
            r2_ops["t"].Set(Gf.Vec3d(pos2[0], pos2[1], GROUND_Z))
            ry2 = math.radians(rr2[2])
            r2_ops["o"].Set(Gf.Quatf(math.cos(ry2/2), 0, 0, math.sin(ry2/2)))
        at_ = anim_start/anim_fps + (t % anim_dur) if anim_dur > 0 else t
        # Timeline is stopped; set the timecode and commit it with one update so
        # USD evaluates the runner's baked animation samples before rendering.
        timeline.set_current_time(at_)
        sim_app.update()

    def predict_dynamic_runner_block(ax0, ay0, dx, dy, t0):
        """Return collision detail if this MOVE_FORWARD would intersect a
        runner's predicted XY disk over the action interval."""
        specs = [("runner1", runner1_spec), ("runner2", runner2_spec)]
        best = None
        for i in range(DYNAMIC_CHECK_SAMPLES):
            alpha = i / max(1, DYNAMIC_CHECK_SAMPLES - 1)
            agent_xy = [ax0 + STEP_DIST * dx * alpha, ay0 + STEP_DIST * dy * alpha]
            tt = t0 + RUNNER_TIME_PER_STEP * alpha
            for runner_name, spec in specs:
                if not spec:
                    continue
                rp, _ = sample_human_motion(spec, tt, anim_fps)
                d = math.hypot(agent_xy[0] - rp[0], agent_xy[1] - rp[1])
                if d < DYNAMIC_COLLISION_RADIUS:
                    detail = {
                        "runner": runner_name,
                        "sample_alpha": round(alpha, 3),
                        "sim_t": tt,
                        "agent_xy": _xy(agent_xy),
                        "runner_xy": _xy(rp),
                        "distance_m": round(d, 3),
                        "threshold_m": round(DYNAMIC_COLLISION_RADIUS, 3),
                    }
                    if best is None or d < best["distance_m"]:
                        best = detail
        return best

    # ── Obstacle-runner visibility (FOV-frustum, pure geometry) ──
    # Filler frames only matter if a moving runner is on screen — that is the
    # only thing they smooth. If no runner is in the agent's FPV frustum this
    # step, the filler renders are wasted. runner_visible() answers that with
    # a few trig ops (no raycast, no occlusion — per design, FOV coverage is
    # taken as "visible"). FPV horizontal FOV is 90° (focal 17 / aperture 34).
    FPV_HALF_FOV = 45.0  # degrees, half of the 90° horizontal FOV

    def runner_visible(cam_x, cam_y, cam_yaw, t):
        """True if any obstacle runner is inside the FPV horizontal FOV cone
        at sim-time t. Pure geometry — cost is negligible vs a render."""
        specs = [s for s in (runner1_spec, runner2_spec) if s]
        for spec in specs:
            rp, _ = sample_human_motion(spec, t, anim_fps)
            dx, dy = rp[0] - cam_x, rp[1] - cam_y
            yaw = math.radians(cam_yaw)
            fwd = dx * math.cos(yaw) + dy * math.sin(yaw)
            side = -dx * math.sin(yaw) + dy * math.cos(yaw)
            if fwd <= 0.05:
                continue  # behind the camera
            if math.degrees(math.atan2(abs(side), fwd)) <= FPV_HALF_FOV:
                return True
        return False

    def _wait_scratch(scratch, prev_n):
        t0 = time.time()
        while time.time() - t0 < 5.0:
            ff = sorted(glob.glob(os.path.join(scratch, "rgb_*.png")))
            if len(ff) > prev_n:
                return ff[-1]
            time.sleep(0.05)
        return None

    def _drain_scratch():
        """Delete any pending PNGs in the writer scratch dirs."""
        for d in [fpv_scratch, bird_scratch]:
            for p in glob.glob(os.path.join(d, "rgb_*.png")):
                try: os.remove(p)
                except: pass

    def prime_render():
        """Flush the render pipeline ONCE at loop start so the first real
        render_frame() already reflects the current pose.

        Only the very first captured frame was ever wrong: it carried stale
        state from the warmup phase (100 free-running updates with a playing
        timeline). Frames 1..N were always continuous on a single render. So we
        drain the pipeline thoroughly here, ONCE, instead of paying a double
        render on every frame."""
        for _ in range(4):
            _drain_scratch()
            rep.orchestrator.step(rt_subframes=DECISION_SUBFRAMES)
            _wait_scratch(fpv_scratch, 0)
        _drain_scratch()

    def render_frame(filler=False):
        """Render one frame at the current pose.

        FPV PNG always goes into the fpv *_smooth folder (decision + filler,
        contiguous). The bird PNG is returned as a scratch path for the caller
        to file per-step into vlm_nav_frames_bird/.

        Bird _smooth (ENABLE_BIRD_SMOOTH):
          - False (default): no bird _smooth folder; filler bird frames are
            discarded; the bird video is built from per-step decision frames.
          - True: filler bird frames are also kept in the bird _smooth folder
            for a paper-quality continuous bird video.

        filler=True: video-only in-between frame, low PathTracing subframes.
        filler=False: full-quality decision frame the VLM sees."""
        subframes = FILLER_SUBFRAMES if filler else DECISION_SUBFRAMES
        _drain_scratch()
        rep.orchestrator.step(rt_subframes=subframes)
        fpv_raw = _wait_scratch(fpv_scratch, 0)
        bird_raw = _wait_scratch(bird_scratch, 0)
        if not fpv_raw:
            return None, None
        idx = smooth_counter[0]; smooth_counter[0] += 1
        fpv_out = os.path.join(fpv_smooth_dir, f"rgb_{idx:04d}.png")
        shutil.move(fpv_raw, fpv_out)
        if filler:
            if ENABLE_BIRD_SMOOTH and bird_raw:
                shutil.move(bird_raw, os.path.join(bird_smooth_dir, f"rgb_{idx:04d}.png"))
            elif bird_raw:
                try: os.remove(bird_raw)
                except: pass
            return fpv_out, None
        # Decision frame: also keep it in bird _smooth if enabled.
        if ENABLE_BIRD_SMOOTH and bird_raw:
            shutil.copy(bird_raw, os.path.join(bird_smooth_dir, f"rgb_{idx:04d}.png"))
        # Return the bird scratch path for the caller to file per-step.
        return fpv_out, bird_raw

    # ── Nav loop ──
    ax, ay, ayaw = agent_start_xy[0], agent_start_xy[1], agent_start_yaw
    apitch = PITCH_INIT; sim_t = 0.0
    cur_phase = 0; inventory = []; action_fb = ""
    nav_hist = []; lamp_on = False
    vlm_calls = 0   # episode counter (decoupled from step count in multi-action mode)
    plan_queue = []; plan_history = []   # multi-action planning state
    cur_planned = []; cur_executed = []  # current plan tracking

    # Room-boundary gate: compute floor polygons once. If extraction fails (no
    # floor meshes) or the spawn somehow lands outside them, disable the gate so
    # we never trap a legitimately-placed agent. The spawn was already validated
    # in-room, so a spawn falling outside the polygons means our extraction is
    # wrong, not the spawn — fail open.
    room_polys = []
    if ROOM_BOUNDARY:
        try:
            room_polys = compute_room_polygons(stage)
            # Spawn validity uses inset=0 (plain in-room test): a spawn placed
            # legitimately close to a floor edge must NOT disable the gate. The
            # 0.45m capsule inset is only applied to MOVEMENT below.
            if room_polys and not inside_any_room(ax, ay, room_polys, 0.0):
                log(f"[BENCH] room-boundary gate DISABLED: spawn ({ax:.2f},{ay:.2f}) "
                    f"outside extracted polygons ({len(room_polys)} rooms) — failing open")
                room_polys = []
            elif room_polys:
                log(f"[BENCH] room-boundary gate ON: {len(room_polys)} room polygon(s), "
                    f"inset={ROOM_BOUNDARY_INSET}m (capsule r=0.40 + 0.05 clearance)")
            else:
                log("[BENCH] room-boundary gate OFF: no floor polygons extracted")
        except Exception as e:
            log(f"[BENCH] room-boundary gate OFF (extract failed): {e}")
            room_polys = []

    log(f"[BENCH] Starting nav loop: start=({ax},{ay}) yaw={ayaw}")

    # ── Spawn nudge: ensure agent doesn't overlap static obstacles ──
    # Uses PhysX sweep to detect immediate overlaps and spiral-searches
    # for the nearest clear position. Saves adjustment details for review.
    import omni.physx, carb
    sim_app.update()
    query_if = omni.physx.get_physx_scene_query_interface()

    _8DIRS = [(1,0),(-1,0),(0,1),(0,-1),
              (0.707,0.707),(-0.707,0.707),(0.707,-0.707),(-0.707,-0.707)]

    def _check_spawn_clear(cx, cy):
        """Return (is_clear, worst_hit_path, worst_dist) for a candidate spawn."""
        # EYE_H added: also reject spawns where the camera height is embedded in
        # geometry (e.g. spawn facing a wall corner -> overexposed/black frame 0).
        for sz in [0.5, 1.0, EYE_H]:
            for dx, dy in _8DIRS:
                h = query_if.sweep_sphere_closest(
                    0.40, carb.Float3(cx, cy, sz),
                    carb.Float3(dx, dy, 0), 0.05)
                if h["hit"]:
                    wp = (h.get("rigidBody") or h.get("collider") or "")
                    if is_walkable_hit(wp):
                        continue
                    d = float(h.get("distance", 0))
                    if d < 0.01:
                        return False, wp.split("/")[-1][:60], d
        return True, "", -1

    spawn_adjustment = None
    is_clear, hit_name, hit_dist = _check_spawn_clear(ax, ay)
    if not is_clear:
        log(f"[BENCH] ⚠ Spawn overlap detected at ({ax:.2f},{ay:.2f}): "
            f"hit={hit_name} dist={hit_dist:.3f}m — searching for clear position")
        original_ax, original_ay = ax, ay
        found = False
        # Spiral search: 0.25m steps, up to 2m radius
        for radius_step in range(1, 9):  # 0.25m to 2.0m
            r = radius_step * 0.25
            n_points = max(8, int(2 * math.pi * r / 0.25))
            for i in range(n_points):
                angle = 2 * math.pi * i / n_points
                cx = original_ax + r * math.cos(angle)
                cy = original_ay + r * math.sin(angle)
                ok, _, _ = _check_spawn_clear(cx, cy)
                if ok:
                    nudge_dist = math.hypot(cx - original_ax, cy - original_ay)
                    ax, ay = cx, cy
                    spawn_adjustment = {
                        "task_id": tid,
                        "original_start": [round(original_ax, 3), round(original_ay, 3)],
                        "adjusted_start": [round(ax, 3), round(ay, 3)],
                        "nudge_distance_m": round(nudge_dist, 3),
                        "reason": f"overlap with {hit_name} at dist={hit_dist:.3f}m",
                        "search_radius_m": round(r, 2),
                    }
                    log(f"[BENCH] ⚠ SPAWN AUTO-ADJUST: ({original_ax:.2f},{original_ay:.2f}) "
                        f"→ ({ax:.2f},{ay:.2f}) nudge={nudge_dist:.2f}m "
                        f"reason=overlap_{hit_name}")
                    found = True
                    break
            if found:
                break
        if not found:
            log(f"[BENCH] ❌ SPAWN CRITICALLY BAD — no clear position within 2m "
                f"of ({original_ax:.2f},{original_ay:.2f})")
            spawn_adjustment = {
                "task_id": tid,
                "original_start": [round(original_ax, 3), round(original_ay, 3)],
                "adjusted_start": [round(ax, 3), round(ay, 3)],
                "nudge_distance_m": 0,
                "reason": f"FAILED: no clear position within 2m, hit={hit_name}",
                "search_radius_m": 2.0,
            }
    else:
        log(f"[BENCH] Spawn clear at ({ax:.2f},{ay:.2f})")

    # Diagnostic only (SPAWN_DEBUG=1): trace dynamic furniture settling onto spawn.
    debug_spawn_settle(query_if, ax, ay, sim_app, carb)

    # Save spawn adjustment info for archival
    if spawn_adjustment:
        adj_path = os.path.join(RUN_DIR, "spawn_adjustment.json")
        with open(adj_path, "w") as f:
            json.dump(spawn_adjustment, f, indent=2)
        log(f"[BENCH] Spawn adjustment saved to {adj_path}")

    # Prime the render pipeline: pose agent + camera + runners at the step-0
    # state and flush throwaway renders. The first orchestrator.step()
    # otherwise captures the stale warmup-phase pose, which made step 0's
    # frame leap. Priming once here lets every render_frame() be a single
    # render with no per-frame double cost.
    # If a runner happens to spawn overlapping the agent's start, resolve it
    # before priming so the very first frame is overlap-free. Use frame_kind
    # "prime" to skip event bookkeeping (initial-condition fix, not contact).
    ax, ay, _ = push_agent_if_overlap(ax, ay, sim_t, step=0, frame_kind="prime")
    a_trans.Set(Gf.Vec3d(ax, ay, GROUND_Z))
    myaw0 = math.radians(ayaw + MESH_YAW_OFF)
    a_orient.Set(Gf.Quatf(math.cos(myaw0/2), 0, 0, math.sin(myaw0/2)))
    if nav_cam and nav_cam.IsValid():
        cxf0 = UsdGeom.Xformable(nav_cam)
        try: cxf0.ClearXformOpOrder()
        except: pass
        cam_x0 = ax + 0.01 * math.cos(math.radians(ayaw))
        cam_y0 = ay + 0.01 * math.sin(math.radians(ayaw))
        cxf0.AddTranslateOp().Set(Gf.Vec3d(cam_x0, cam_y0, EYE_H))
        cxf0.AddOrientOp().Set(cam_quat(ayaw, apitch))
    pose_runners_at(sim_t)
    prime_render()

    for step in range(max_steps):
        tgt = resolved_targets[cur_phase]
        tgt_radius = phases[cur_phase]["radius"]
        tgt_half = resolved_half_extents[cur_phase] if cur_phase < len(resolved_half_extents) else 0.0
        center_dist = math.sqrt((ax-tgt[0])**2 + (ay-tgt[1])**2)
        dist = max(0.0, center_dist - tgt_half)  # edge-based distance

        # Resolve any agent-runner overlap BEFORE writing the agent + camera
        # xforms so the decision frame the VLM sees has no penetration.
        ax, ay, _ = push_agent_if_overlap(ax, ay, sim_t, step=step, frame_kind="decision")

        # Update agent pose
        a_trans.Set(Gf.Vec3d(ax, ay, GROUND_Z))
        myaw = math.radians(ayaw + MESH_YAW_OFF)
        a_orient.Set(Gf.Quatf(math.cos(myaw/2), 0, 0, math.sin(myaw/2)))

        # Update camera — minimal forward offset (0.01m) to stay inside the
        # agent's collision capsule (r=0.40m). The old 0.1m offset pushed the
        # camera to the capsule edge, causing it to enter runner head mesh
        # during close encounters (e.g. deadlock freeze at SAFE_RADIUS).
        if nav_cam and nav_cam.IsValid():
            cxf = UsdGeom.Xformable(nav_cam)
            try: cxf.ClearXformOpOrder()
            except: pass
            cam_x = ax + 0.01 * math.cos(math.radians(ayaw))
            cam_y = ay + 0.01 * math.sin(math.radians(ayaw))
            # DO NOT ADD GROUND_Z: EYE_H is absolute height from floor.
            # Adding GROUND_Z pushed the camera into the ceiling (2.25m).
            cxf.AddTranslateOp().Set(Gf.Vec3d(cam_x, cam_y, EYE_H))
            cxf.AddOrientOp().Set(cam_quat(ayaw, apitch))

        # Animate runners to the start-of-step sim_t, then render this step's
        # frame. It lands in the *_smooth folders as a normal frame.
        pose_runners_at(sim_t)
        _t_render = time.time()
        fpv_smooth_path, bird_raw = render_frame()
        timing["render_decision"] += time.time() - _t_render
        timing["n_decision"] += 1
        if not fpv_smooth_path:
            log(f"[BENCH] Step {step}: frame timeout"); break
        # File this step's frame into the per-step decision folders (rgb_NNNN
        # contiguous by step). FPV decision frame is also already in fpv_smooth.
        frame_path = os.path.join(fpv_dir, f"rgb_{step:04d}.png")
        shutil.copy(fpv_smooth_path, frame_path)
        if bird_raw:
            shutil.move(bird_raw, os.path.join(bird_dir, f"rgb_{step:04d}.png"))
        # Generate thumbnails for quick preview
        try:
            from PIL import Image
            for d in [fpv_dir, bird_dir]:
                df = sorted(glob.glob(os.path.join(d, "rgb_*.png")))
                if df:
                    tp = df[-1].replace(".png","_thumb.jpg")
                    with Image.open(df[-1]) as im:
                        if im.mode in ('RGBA','P'): im = im.convert('RGB')
                        im.thumbnail((480,270)); im.save(tp,"JPEG",quality=80)
        except: pass

        # ── Flagged-task review snapshot ──
        # full_task_gen may flag a task (raycast occlusion / connectivity) as
        # informational. We do NOT render anything extra for it: on step 0 we
        # just copy this already-rendered bird+fpv frame into the flag folder
        # so the user can eyeball whatever the raycast was unsure about.
        if step == 0:
            try:
                flag_dir = os.path.join(SCRIPT_DIR, "review_flagged")
                meta = os.path.join(flag_dir, f"flagged_{tid.split('-')[0]}.json")
                if os.path.exists(meta):
                    fl = json.load(open(meta))
                    rec = next((r for r in fl.get("flagged", []) if r["id"] == tid), None)
                    if rec:
                        snap = os.path.join(flag_dir, tid)
                        os.makedirs(snap, exist_ok=True)
                        shutil.copy(frame_path, os.path.join(snap, "fpv_step0.png"))
                        bf = sorted(glob.glob(os.path.join(bird_dir, "rgb_*.png")))
                        if bf:
                            shutil.copy(bf[-1], os.path.join(snap, "bird_step0.png"))
                        json.dump(rec, open(os.path.join(snap, "flag_meta.json"), "w"), indent=2)
                        log(f"[BENCH] Flagged task — review snapshot saved to {snap}")
            except Exception as e:
                log(f"[BENCH] flag snapshot skipped: {e}")

        ph = phases[cur_phase]
        log(f"[BENCH] Step {step}: ({ax:.2f},{ay:.2f}) yaw={ayaw:.0f} dist={dist:.2f} "
            f"phase={cur_phase+1}/{len(phases)}")

        # ── Multi-action planning: query VLM only when plan_queue is empty ──
        queried_this_step = False
        if not plan_queue:
            # Check VLM call budget
            if vlm_calls >= MAX_VLM_CALLS:
                log(f"[BENCH] VLM call budget exhausted ({MAX_VLM_CALLS} calls)")
                break

            # Build prompt
            if is_multi:
                inv_s = ','.join(inventory) if inventory else 'empty'
                lamp_s = " Lamp: ON." if lamp_on else ""
                prompt = (f"Current objective: go to {ph['desc']} and use {ph['action']}. "
                          f"Carrying: [{inv_s}].{lamp_s} Progress: step {cur_phase+1}/{len(phases)}. "
                          f"Plan your next actions.")
            else:
                prompt = f"Navigate to {ph['desc']}. Plan your next actions."
            if action_fb:
                prompt += f" ⚠ PREVIOUS ACTION FAILED: {action_fb}"

            # Plan history feedback
            if plan_history:
                hlines = []
                for ph_rec in plan_history[-5:]:
                    pl = ', '.join(ph_rec['planned'])
                    ex = ', '.join(ph_rec['executed'])
                    hlines.append(f"  planned [{pl}] -> executed [{ex}] -> {ph_rec['outcome']}")
                prompt += ("\nYour recent plans (most recent last):\n" + "\n".join(hlines)
                           + "\nIf a plan was BLOCKED, the route ahead is obstructed — "
                             "choose a clearly different direction now.")
                # Stuck detection: 3+ consecutive plans that moved nowhere
                stuck = sum(1 for r in plan_history[-3:]
                            if all(a in ("MOVE_FORWARD",) for a in r["planned"][:1])
                            and "BLOCKED" in r["outcome"])
                if stuck >= 3:
                    prompt += ("\n⚠ WARNING: 3+ plans in a row were blocked immediately. "
                               "You MUST turn substantially (queue several TURN_LEFT or "
                               "TURN_RIGHT) before moving forward again.")

            fq = check_frame_quality(frame_path)
            if fq.get('guidance'): prompt += fq['guidance']

            # Temporal context: up to N_FRAMES frames (oldest..current).
            # N_FRAMES=1 -> current decision frame only (no history).
            vlm_frames = []
            for prev_step in range(step - (N_FRAMES - 1), step):
                if prev_step >= 0:
                    prev_path = os.path.join(fpv_dir, f"rgb_{prev_step:04d}.png")
                    if os.path.exists(prev_path):
                        vlm_frames.append(prev_path)
            vlm_frames.append(frame_path)

            # Query VLM concurrently; render filler frames while it thinks.
            # Strict per-step: sim_t advances by exactly RUNNER_TIME_PER_STEP
            # regardless of VLM wall-clock. On-screen → render FILLER_FRAMES_PER_STEP
            # filler frames (each = 1 anim atomic keyframe). Off-screen → skip
            # filler renders. Either way sim_t lands at sim_t + RUNNER_TIME_PER_STEP.
            import threading
            vlm_result = {}
            def _vlm_worker():
                vlm_result["out"] = query_vlm_plan(vlm_frames, prompt, sys_prompt, step)
            _t_vlm = time.time()
            vlm_thread = threading.Thread(target=_vlm_worker)
            vlm_thread.start()

            # Visibility gate: scan the upcoming step's sim_t interval
            # [sim_t, sim_t + RUNNER_TIME_PER_STEP]. Narrow scan is enough now
            # that sim_t advances deterministically per step. Only needed to
            # decide filler rendering, so it is skipped when RENDER_FILLER=0.
            runner_on_screen = False
            if RENDER_FILLER:
                VIS_SCAN_POINTS = 5
                _t_vis = time.time()
                runner_on_screen = any(
                    runner_visible(ax, ay, ayaw,
                                   sim_t + RUNNER_TIME_PER_STEP * k / (VIS_SCAN_POINTS - 1))
                    for k in range(VIS_SCAN_POINTS))
                timing["visibility_check"] += time.time() - _t_vis
                timing["n_runner_visible" if runner_on_screen else "n_runner_hidden"] += 1

            # Filler frames are video-only and gated by the RENDER_FILLER master
            # switch. When RENDER_FILLER=0 (default) NO filler is rendered — the
            # block below is skipped entirely and sim_t still advances, so the
            # runner/collision state is unchanged. When RENDER_FILLER=1,
            # VISIBILITY_GATE decides: off → filler every step regardless of FOV
            # (eliminates stale-USD artifacts on long off-screen sequences);
            # on → filler only when a runner is actually on screen. Stats still
            # reflect actual visibility either way.
            render_filler_this_step = RENDER_FILLER and (runner_on_screen or not VISIBILITY_GATE)
            if render_filler_this_step:
                # Render fixed FILLER_FRAMES_PER_STEP frames spanning the step.
                # Each filler frame corresponds to one baked anim atomic keyframe.
                for i in range(FILLER_FRAMES_PER_STEP):
                    filler_t = sim_t + RUNNER_TIME_PER_STEP * (i + 1) / FILLER_FRAMES_PER_STEP
                    ax, ay, _ = push_agent_if_overlap(ax, ay, filler_t, step=step, frame_kind="filler")
                    a_trans.Set(Gf.Vec3d(ax, ay, GROUND_Z))
                    if nav_cam and nav_cam.IsValid():
                        cxf_f = UsdGeom.Xformable(nav_cam)
                        try: cxf_f.ClearXformOpOrder()
                        except: pass
                        cam_x = ax + 0.01 * math.cos(math.radians(ayaw))
                        cam_y = ay + 0.01 * math.sin(math.radians(ayaw))
                        cxf_f.AddTranslateOp().Set(Gf.Vec3d(cam_x, cam_y, EYE_H))
                        cxf_f.AddOrientOp().Set(cam_quat(ayaw, apitch))
                    pose_runners_at(filler_t)
                    _t_fr = time.time()
                    render_frame(filler=True)
                    timing["render_filler"] += time.time() - _t_fr
                    timing["n_filler"] += 1
            sim_t += RUNNER_TIME_PER_STEP
            # Wait for VLM to finish (it may take longer than the 5 filler renders).
            vlm_thread.join()
            timing["vlm"] += time.time() - _t_vlm
            timing["n_vlm"] += 1
            plan, fallback = vlm_result.get("out", (["MOVE_FORWARD"], True))
            plan_queue = list(plan)
            cur_planned = list(plan)
            cur_executed = []
            vlm_calls += 1
            if not RENDER_FILLER:
                _vis_tag = "filler disabled"
            elif runner_on_screen:
                _vis_tag = "runner visible, filler rendered"
            elif not VISIBILITY_GATE:
                _vis_tag = "runner off-screen, filler forced"
            else:
                _vis_tag = "runner off-screen, no filler"
            log(f"[BENCH] Step {step}: VLM call #{vlm_calls} -> plan={plan_queue} "
                f"({_vis_tag})")
            action_fb = ""
            queried_this_step = True

        # Pop the next action from the queue
        action = plan_queue.pop(0)
        cur_executed.append(action)
        if not queried_this_step:
            log(f"[BENCH] Step {step}: action={action} (queued, {len(plan_queue)} remaining)")

        # DONE confirm — only when NOT already within radius. If the agent is
        # already in range, accept DONE immediately: a confirm re-query where the
        # model wavers (returns a non-DONE action) used to silently discard a
        # valid arrival, depressing SR. The confirm is a guard against premature
        # DONE while still far, not a second gate on a legitimate arrival.
        if action == "DONE" and not (ph["action"] == "DONE" and dist < tgt_radius):
            for cr in range(1, DONE_CONFIRM):
                ca, _ = query_vlm(frame_path, "You chose DONE. Is the target within arm's reach? Confirm.", sys_prompt, step)
                if ca != "DONE": action = ca; plan_queue = []; break

        pre_x, pre_y = ax, ay
        pre_yaw, pre_pitch = ayaw, apitch
        pre_phase = cur_phase
        pre_fb = action_fb
        nav_hist.append({"step":step,"x":round(ax,3),"y":round(ay,3),"yaw":round(ayaw,1),
                         "dist_to_target":round(dist,3),"action":action,
                         "moved":False,"blocked":False,
                         "blocked_reason":None,"blocked_detail":None})

        # ── Execute action ──
        # DONE means "I've arrived / this phase is complete". It completes any
        # arrival phase (a two_nav DONE-phase OR a pick_place PUT_DOWN-phase) when
        # in range — reaching the destination is the goal; whether the model then
        # literally says DONE vs PUT_DOWN shouldn't gate a nav benchmark. (The
        # PUT_DOWN block below handles the carrying case and pops inventory.)
        if action == "DONE":
            if ph["action"] in ("DONE", "PUT_DOWN") and dist < tgt_radius:
                cur_phase += 1
                log(f"[BENCH] DONE success phase {cur_phase}/{len(phases)} dist={dist:.2f}")
                if cur_phase >= len(phases):
                    log(f"[BENCH] ALL PHASES DONE — SUCCESS at step {step}")
                    break
                tgt = resolved_targets[cur_phase]
            else:
                action_fb = f"DONE rejected: still need to {ph['desc']}."
                log(f"[BENCH] DONE rejected dist={dist:.2f}")

        elif action == "PAUSE":
            # Stand still for one step. No XY change, no yaw change.
            # sim_t still advances via the filler/no-filler path above, so
            # runners keep moving. The "not moved 3+ steps" warning will fire
            # if PAUSE is chosen repeatedly without progress.
            pass

        elif action == "PICK_UP":
            if ph["action"] == "PICK_UP" and dist < tgt_radius:
                inventory.append("object")
                if pickup_prim and pickup_prim.IsValid():
                    pickup_prim.SetActive(False)  # remove visual + physics collider
                    log(f"[BENCH] Deactivated picked-up prim: {pickup_prim.GetPath()}")
                cur_phase += 1
                log(f"[BENCH] PICK_UP success, advancing to phase {cur_phase+1}")
                if cur_phase < len(phases):
                    tgt = resolved_targets[cur_phase]
            else:
                action_fb = "PICK_UP failed: too far."
                log(f"[BENCH] PICK_UP failed dist={dist:.2f}")

        elif action == "PUT_DOWN":
            # PUT_DOWN completes an arrival phase ONLY while actually carrying
            # something (inventory non-empty). "bring it to X" makes a carrying
            # agent emit PUT_DOWN, so it's accepted for a DONE- or PUT_DOWN-phase.
            # But a two_nav phase (empty-handed — never picked anything up) must
            # NOT be completed by a stray PUT_DOWN; it only accepts DONE.
            # NOTE: `inventory` is the right gate here, not an explicit "is this a
            # pick task?" check. inventory becomes non-empty ONLY via a successful
            # PICK_UP (a pick_place phase1), so `and inventory` already implies
            # "this is a pick task AND we've actually grabbed the object" — strictly
            # stronger than a task-type check (it also rejects a pick task that
            # PUT_DOWNs before ever picking up). Don't add a task-type condition.
            if ph["action"] in ("PUT_DOWN", "DONE") and dist < tgt_radius and inventory:
                inventory.pop()
                cur_phase += 1
                log(f"[BENCH] PUT_DOWN success, advancing to phase {cur_phase+1}")
                if cur_phase < len(phases):
                    tgt = resolved_targets[cur_phase]
                elif cur_phase >= len(phases):
                    log(f"[BENCH] ALL PHASES DONE — SUCCESS at step {step}"); break
            else:
                action_fb = "PUT_DOWN failed."

        elif action == "TURN_ON":
            if ph["action"] == "TURN_ON" and dist < tgt_radius:
                lamp_on = True
                # Create visible light
                ll = UsdLux.SphereLight.Define(stage, "/World/Lights/TaskLamp")
                ll.CreateIntensityAttr().Set(150000.0); ll.CreateRadiusAttr().Set(0.15)
                ll.CreateColorAttr().Set(Gf.Vec3f(1.0, 0.92, 0.7))
                lxf = UsdGeom.Xformable(ll); lxf.ClearXformOpOrder()
                lxf.AddTranslateOp().Set(Gf.Vec3d(tgt[0], tgt[1], 1.2))
                cur_phase += 1
                log(f"[BENCH] TURN_ON success")
                if cur_phase < len(phases):
                    tgt = resolved_targets[cur_phase]
            else:
                action_fb = "TURN_ON failed: too far."

        elif action == "MOVE_FORWARD":
            import omni.physx, carb
            sim_app.update()
            query_if = omni.physx.get_physx_scene_query_interface()
            dx = math.cos(math.radians(ayaw)); dy = math.sin(math.radians(ayaw))
            blocked = False
            hit_info = None
            dynamic_hit = predict_dynamic_runner_block(ax, ay, dx, dy, sim_t)
            if dynamic_hit:
                blocked = True
                collision_counts["dynamic_runner_predicted"] += 1
                nav_hist[-1]["blocked"] = True
                nav_hist[-1]["blocked_reason"] = "dynamic_runner"
                nav_hist[-1]["blocked_detail"] = (
                    f"{dynamic_hit['runner']} dist={dynamic_hit['distance_m']:.3f}m "
                    f"threshold={dynamic_hit['threshold_m']:.3f}m"
                )
                action_fb = "MOVE_FORWARD blocked by a moving person. Try turning or choosing another route."
                record_collision_event({
                    "step": step,
                    "type": "dynamic_runner",
                    "action": action,
                    "sim_t": dynamic_hit["sim_t"],
                    "agent_xy": dynamic_hit["agent_xy"],
                    "runner": dynamic_hit["runner"],
                    "runner_xy": dynamic_hit["runner_xy"],
                    "distance_m": dynamic_hit["distance_m"],
                    "threshold_m": dynamic_hit["threshold_m"],
                    "result": "move_blocked",
                })
                log(f"[BENCH] Step {step}: DYNAMIC COLLISION blocked "
                    f"{dynamic_hit['runner']} dist={dynamic_hit['distance_m']:.3f}m")
            # z=EYE_H added so the FPV camera height is collision-checked:
            # the body sweep at 0.5/1.0 (sphere top ~1.4m) leaves a 1.4-1.58m
            # blind spot, so chest-high / overhanging geometry (wall cabinets,
            # uneven walls) lets the camera (z=1.58) clip through -> black/white
            # FPV frames with NO 'blocked' feedback to the agent. Checking EYE_H
            # both prevents the clip and surfaces the obstacle as a real block.
            for sz in [0.5, 1.0, EYE_H]:
                if blocked:
                    break
                hit = query_if.sweep_sphere_closest(0.40, carb.Float3(ax,ay,sz),
                                                     carb.Float3(dx,dy,0), STEP_DIST)
                if SWEEP_DEBUG:
                    _hp = (hit.get("rigidBody") or hit.get("collider") or "")
                    log(f"[SWEEP] step={step} pos=({ax:.3f},{ay:.3f}) dir=({dx:+.2f},{dy:+.2f}) "
                        f"z={sz} hit={hit['hit']} dist={hit.get('distance',-1):.4f} "
                        f"path={_hp.split('/')[-1][:32]}")
                if not hit["hit"]:
                    continue
                hit_path = (hit.get("rigidBody") or hit.get("collider") or "")
                # Skip non-obstacle hits (floor meshes + soft draped textiles).
                # Precise basename match — see is_walkable_hit (NOT substring,
                # which used to let FloorLamp/Mattress pass as "walkable").
                if is_walkable_hit(hit_path):
                    continue
                hit_info = (sz, hit_path.lower(), hit.get("distance", -1))
                blocked = True; break
            # Soft room boundary: the collision sweep can't stop a move toward a
            # missing-wall opening (no geometry to hit), so the agent would walk
            # into the unlit void outside the room. Block such a move as if it hit
            # a void wall, keeping the agent's capsule (and FPV camera) inside the
            # room footprint with ROOM_BOUNDARY_INSET clearance — so the camera
            # never peeks through the opening into the black exterior.
            if not blocked and room_polys:
                nx_, ny_ = ax + STEP_DIST * dx, ay + STEP_DIST * dy
                if not inside_any_room(nx_, ny_, room_polys, ROOM_BOUNDARY_INSET):
                    blocked = True
                    # sentinel dist=-1 (not 0): this is a soft-boundary refusal,
                    # NOT a measured physics overlap, so don't fire the dist==0
                    # "possible embedding" CLIP warning below.
                    hit_info = (EYE_H, "room_boundary", -1.0)
            if not blocked:
                ax += STEP_DIST * dx; ay += STEP_DIST * dy
            elif hit_info:
                collision_counts["static_obstacle"] += 1
                nav_hist[-1]["blocked"] = True
                nav_hist[-1]["blocked_reason"] = "static_obstacle"
                nav_hist[-1]["blocked_detail"] = (
                    f"z={hit_info[0]} dist={hit_info[2]:.3f}m "
                    f"hit={hit_info[1].split('/')[-1][:60]}"
                )
                action_fb = "MOVE_FORWARD blocked by an obstacle. Try turning or choosing another route."
                # dist==0 means the agent sphere already OVERLAPS this collider
                # (it is partially inside it) — a clip/embedding warning sign worth
                # surfacing for later debugging (see ANALYSIS.md wall-clip case).
                # Require a real MEASURED overlap (0 <= dist <= eps); the soft
                # room-boundary refusal uses dist=-1 sentinel and must NOT trip it.
                if 0.0 <= hit_info[2] <= 1e-6:
                    log(f"[CLIP?] step={step} agent=({ax:.2f},{ay:.2f}) overlaps "
                        f"{hit_info[1].split('/')[-1][:48]} (dist=0) — possible embedding")
                record_collision_event({
                    "step": step,
                    "type": "static_obstacle",
                    "action": action,
                    "sim_t": sim_t,
                    "agent_xy": _xy([ax, ay]),
                    "hit_z": hit_info[0],
                    "hit_path": hit_info[1],
                    "distance_m": round(float(hit_info[2]), 3),
                    "result": "move_blocked",
                })
                log(f"[BENCH] Step {step}: COLLISION at z={hit_info[0]} "
                    f"dist={hit_info[2]:.3f}m hit={hit_info[1].split('/')[-1][:60]}")
            elif blocked:
                nav_hist[-1]["blocked"] = True

        elif action == "TURN_LEFT": ayaw += TURN_ANG
        elif action == "TURN_RIGHT": ayaw -= TURN_ANG
        elif action == "TILT_UP": apitch = min(apitch + TILT_ANG, PITCH_MAX)
        elif action == "TILT_DOWN": apitch = max(apitch - TILT_ANG, PITCH_MIN)

        ayaw = wrap_angle_deg(ayaw)
        did_move = abs(ax-pre_x) > 0.001 or abs(ay-pre_y) > 0.001

        # ── Smooth Camera Transition (filler) ──
        # Camera transition interpolation: set to 0 to skip runtime PathTracing
        # of intermediate frames (~1.5s/frame savings). Use ffmpeg minterpolate
        # in post-processing for smooth video instead (pure CPU, zero render cost).
        # Gated by the RENDER_FILLER master switch — these are filler frames too,
        # so they never render when filler is disabled (default).
        CAM_TRANSITION_FRAMES = 0
        if RENDER_FILLER and CAM_TRANSITION_FRAMES > 0 and (
                did_move or abs(ayaw - pre_yaw) > 0.1 or abs(apitch - pre_pitch) > 0.1):
            def short_angle(a0, a1):
                da = (a1 - a0) % 360
                return 2 * da % 360 - da
            dyaw = short_angle(pre_yaw, ayaw)
            dpitch = apitch - pre_pitch
            dx_m = ax - pre_x
            dy_m = ay - pre_y
            
            for t in range(1, CAM_TRANSITION_FRAMES + 1):
                frac = t / CAM_TRANSITION_FRAMES
                ix = pre_x + dx_m * frac
                iy = pre_y + dy_m * frac
                iyaw = pre_yaw + dyaw * frac
                ipitch = pre_pitch + dpitch * frac
                
                sim_t += (1.0 / FILLER_FPS)
                a_trans.Set(Gf.Vec3d(ix, iy, GROUND_Z))
                if nav_cam and nav_cam.IsValid():
                    cxf_f = UsdGeom.Xformable(nav_cam)
                    try: cxf_f.ClearXformOpOrder()
                    except: pass
                    cam_x = ix + 0.01 * math.cos(math.radians(iyaw))
                    cam_y = iy + 0.01 * math.sin(math.radians(iyaw))
                    cxf_f.AddTranslateOp().Set(Gf.Vec3d(cam_x, cam_y, EYE_H))
                    cxf_f.AddOrientOp().Set(cam_quat(iyaw, ipitch))
                pose_runners_at(sim_t)
                _t_fr = time.time()
                render_frame(filler=True)
                timing["render_filler"] += time.time() - _t_fr
                timing["n_filler"] += 1

        nav_hist[-1]["moved"] = did_move
        # Advance sim_t for queued steps (plan items 2..N executed without a
        # new VLM call). When RENDER_FILLER=1 this mirrors the VLM-call step's
        # visibility-gated filler so the runner's baked walk-cycle stays
        # continuous across queued steps. When RENDER_FILLER=0 (default) no
        # filler is rendered and the visibility probe is skipped — sim_t still
        # advances by RUNNER_TIME_PER_STEP so queued steps stay reproducible.
        if not queried_this_step:
            if RENDER_FILLER:
                VIS_SCAN_POINTS = 5
                _t_vis = time.time()
                queued_on_screen = any(
                    runner_visible(ax, ay, ayaw,
                                   sim_t + RUNNER_TIME_PER_STEP * k / (VIS_SCAN_POINTS - 1))
                    for k in range(VIS_SCAN_POINTS))
                timing["visibility_check"] += time.time() - _t_vis
                timing["n_runner_visible" if queued_on_screen else "n_runner_hidden"] += 1

                if queued_on_screen or not VISIBILITY_GATE:
                    for i in range(FILLER_FRAMES_PER_STEP):
                        filler_t = sim_t + RUNNER_TIME_PER_STEP * (i + 1) / FILLER_FRAMES_PER_STEP
                        ax, ay, _ = push_agent_if_overlap(ax, ay, filler_t, step=step, frame_kind="filler")
                        a_trans.Set(Gf.Vec3d(ax, ay, GROUND_Z))
                        if nav_cam and nav_cam.IsValid():
                            cxf_f = UsdGeom.Xformable(nav_cam)
                            try: cxf_f.ClearXformOpOrder()
                            except: pass
                            cam_x = ax + 0.01 * math.cos(math.radians(ayaw))
                            cam_y = ay + 0.01 * math.sin(math.radians(ayaw))
                            cxf_f.AddTranslateOp().Set(Gf.Vec3d(cam_x, cam_y, EYE_H))
                            cxf_f.AddOrientOp().Set(cam_quat(ayaw, apitch))
                        pose_runners_at(filler_t)
                        _t_fr = time.time()
                        render_frame(filler=True)
                        timing["render_filler"] += time.time() - _t_fr
                        timing["n_filler"] += 1
            sim_t += RUNNER_TIME_PER_STEP

        # ── Decide whether the current plan ends here ──
        collided = nav_hist[-1]["blocked"]
        phase_changed = cur_phase != pre_phase
        action_failed = bool(action_fb) and not pre_fb
        plan_interrupted = collided or phase_changed or action_failed
        plan_ended = plan_interrupted or not plan_queue

        if plan_ended and cur_planned:
            if collided:
                outcome = f"BLOCKED at action {len(cur_executed)} ({action})"
            elif phase_changed:
                outcome = f"sub-task completed ({action})"
            elif action_failed:
                outcome = f"action failed ({action_fb})"
            else:
                outcome = "plan completed, no obstruction"
            plan_history.append({"planned": list(cur_planned),
                                  "executed": list(cur_executed),
                                  "outcome": outcome})
            if plan_interrupted and plan_queue:
                # Arrival-rescue: a collision on an earlier action (e.g.
                # MOVE_FORWARD) used to abort the WHOLE queue, discarding a
                # trailing DONE/PUT_DOWN even when the agent is ALREADY within
                # the goal radius. A blocked "nudge forward" must not mask the
                # fact that we have arrived. So if the dropped tail contains a
                # completion action and the current phase is a DONE/PUT_DOWN
                # arrival phase already satisfied, complete it instead.
                # A trailing DONE completes any arrival phase when in range; a
                # trailing PUT_DOWN only completes while carrying (inventory) —
                # an empty-handed two_nav phase is never rescued by a stray
                # PUT_DOWN (mirrors the PUT_DOWN execute-block rule above).
                tail_has_done = "DONE" in plan_queue
                tail_has_putdown = "PUT_DOWN" in plan_queue
                rescuable = tail_has_done or (tail_has_putdown and inventory)
                if (collided and rescuable
                        and ph["action"] in ("DONE", "PUT_DOWN")
                        and dist < tgt_radius):
                    if tail_has_putdown and inventory:
                        inventory.pop()
                    cur_phase += 1
                    log(f"[BENCH] arrival-rescue: completing phase {cur_phase}/{len(phases)} "
                        f"from dropped tail DONE/PUT_DOWN dist={dist:.2f}")
                    plan_queue = []
                    cur_planned = []; cur_executed = []
                    if cur_phase >= len(phases):
                        log(f"[BENCH] ALL PHASES DONE — SUCCESS at step {step}")
                        break
                    tgt = resolved_targets[cur_phase]
                    continue
                log(f"[BENCH] Step {step}: aborting queued plan ({outcome}), "
                    f"dropped {len(plan_queue)} action(s) -> re-query next step")
                plan_queue = []
            cur_planned = []; cur_executed = []
    else:
        log(f"[BENCH] TIMEOUT after {max_steps} steps, dist={dist:.2f}")

    # ── Save results ──
    metrics = compute_metrics(nav_hist, task, cur_phase, len(phases))
    # Planning efficiency: how many VLM calls the episode used. With
    # multi-action planning one call yields up to PLAN_LEN actions, so
    # vlm_calls << steps_used. This is a cost/latency axis, separate from the
    # physical-step metrics (which stay comparable to single-action runs).
    metrics["vlm_calls"] = vlm_calls
    metrics["actions_per_call"] = round(metrics["steps_used"] / max(1, vlm_calls), 2)
    metrics["static_collision_count"] = collision_counts["static_obstacle"]
    metrics["dynamic_runner_collision_count"] = collision_counts["dynamic_runner_predicted"]
    metrics["agent_pushed_events"] = collision_counts["agent_pushed_events"]
    metrics["agent_pushed_frames"] = collision_counts["agent_pushed_frames"]
    # Total collisions = static blocks + predicted-dynamic blocks + push contacts.
    metrics["collision_count"] = (
        metrics["static_collision_count"] +
        metrics["dynamic_runner_collision_count"] +
        metrics["agent_pushed_events"]
    )

    # ── Render-quality guard: detect black/blank FPV frames ──
    # Black/blank FPV frames mean the agent was effectively blind (camera clipped
    # into geometry, walked into an unlit area, or the RTX renderer faulted and
    # emitted empty frames). Such an episode is a FALSE failure that silently
    # depresses SR, so flag it here instead of letting the bad frames count as a
    # real run. We never *fix* the frames — we only measure and mark.
    #   fpv_black_frac : fraction of FPV decision frames with mean luminance < dark
    #   bird_black_frac, render_black_sync : if bird darkens together with fpv the
    #     renderer itself faulted (vs. only-fpv = camera clip / dark area).
    #   render_invalid : fpv_black_frac >= RENDER_INVALID_FRAC -> exclude from SR.
    try:
        from PIL import Image
        DARK_LUM = float(os.environ.get("DARK_LUM", "8.0"))
        INVALID_FRAC = float(os.environ.get("RENDER_INVALID_FRAC", "0.20"))

        def _black_set(d):
            blk, tot = set(), 0
            for p in sorted(glob.glob(os.path.join(d, "rgb_*.png"))):
                m = re.match(r"rgb_(\d+)\.png$", os.path.basename(p))
                if not m:
                    continue
                tot += 1
                try:
                    import numpy as _np
                    if _np.asarray(Image.open(p).convert("L"), dtype=_np.float32).mean() < DARK_LUM:
                        blk.add(int(m.group(1)))
                except Exception:
                    pass
            return blk, tot

        fpv_blk, fpv_tot = _black_set(fpv_dir)
        bird_blk, bird_tot = _black_set(bird_dir)
        fpv_frac = (len(fpv_blk) / fpv_tot) if fpv_tot else 0.0
        sync = (len(fpv_blk & bird_blk) / len(fpv_blk)) if fpv_blk else 0.0
        metrics["render_quality"] = {
            "fpv_frames": fpv_tot,
            "fpv_black_frac": round(fpv_frac, 3),
            "bird_black_frac": round((len(bird_blk) / bird_tot) if bird_tot else 0.0, 3),
            "render_black_sync": round(sync, 3),
            "first_black_step": (min(fpv_blk) if fpv_blk else -1),
            "render_invalid": fpv_frac >= INVALID_FRAC,
            "dark_lum_thresh": DARK_LUM,
        }
        if fpv_frac >= INVALID_FRAC:
            kind = "RENDERER-FAULT" if sync >= 0.7 else "CAMERA/DARK"
            log(f"[BENCH] RENDER_INVALID: {fpv_frac:.0%} FPV frames black "
                f"(sync={sync:.2f} -> {kind}); episode excluded from SR")
    except Exception as e:
        log(f"[BENCH] render-quality guard skipped: {e}")

    # ── Timing breakdown — where did the episode spend wall-clock time? ──
    # NOTE: the VLM thread and filler renders run CONCURRENTLY, so "vlm" (the
    # join window) overlaps "render_filler". Pure VLM wait ≈ vlm - render_filler.
    rd, rf = timing["render_decision"], timing["render_filler"]
    vt = timing["vlm"]
    nd, nf, nv = timing["n_decision"], timing["n_filler"], max(1, timing["n_vlm"])
    metrics["timing"] = {k: round(v, 1) for k, v in timing.items()}

    collision_summary = {
        "total": metrics["collision_count"],
        "static_obstacle": metrics["static_collision_count"],
        "dynamic_runner_predicted": metrics["dynamic_runner_collision_count"],
        "agent_pushed_events": metrics["agent_pushed_events"],
        "agent_pushed_frames": metrics["agent_pushed_frames"],
    }
    collisions = {
        "summary": collision_summary,
        "config": {
            "agent_radius_m": AGENT_RADIUS,
            "runner_radius_m": RUNNER_RADIUS,
            "safe_radius_m": SAFE_RADIUS,
            "dynamic_collision_margin_m": DYNAMIC_COLLISION_MARGIN,
            "dynamic_collision_radius_m": DYNAMIC_COLLISION_RADIUS,
        },
        "events": collision_events,
    }
    results = {"task": task, "metrics": metrics, "model_name": MODEL_NAME, "nav_history": nav_hist,
               "resolved_targets": resolved_targets,
               "agent_start": agent_start_xy, "agent_yaw": agent_start_yaw,
               "spawn_adjusted": spawn_adjustment is not None,
               "spawn_adjustment": spawn_adjustment,
               "effective_start": [round(nav_hist[0]["x"], 3), round(nav_hist[0]["y"], 3)] if nav_hist else agent_start_xy}
    with open(os.path.join(RUN_DIR, "vlm_nav_history.json"), "w") as f:
        json.dump(results, f, indent=2)
    with open(os.path.join(RUN_DIR, "results.json"), "w") as f:
        json.dump(results, f, indent=2)
    with open(os.path.join(RUN_DIR, "collisions.json"), "w") as f:
        json.dump(collisions, f, indent=2)

    log(f"[BENCH] Results: SR={metrics['task_success_rate']} SP={metrics['subtask_progress']:.0%} "
        f"GD={metrics['goal_distance_m']:.2f}m Steps={metrics['steps_used']} "
        f"VLMcalls={vlm_calls} ({metrics['actions_per_call']} actions/call) "
        f"Collisions={metrics['collision_count']} "
        f"(static={metrics['static_collision_count']}, "
        f"dyn-predicted={metrics['dynamic_runner_collision_count']}, "
        f"pushed={metrics['agent_pushed_events']} events/"
        f"{metrics['agent_pushed_frames']} frames)")

    log(f"[BENCH] TIMING: decision-render {rd:.0f}s ({nd} frames, "
        f"{rd/max(1,nd):.1f}s/frame) | filler-render {rf:.0f}s ({nf} frames, "
        f"{rf/max(1,nf):.1f}s/frame) | vlm-window {vt:.0f}s ({nv} calls, "
        f"{vt/nv:.1f}s/call) | pure-vlm≈{max(0,vt-rf):.0f}s")
    # Visibility-gated filler: how many VLM-call steps had the runner on screen
    # (filler rendered) vs off-screen (filler skipped — render saved). Only
    # meaningful when RENDER_FILLER=1; with filler disabled no probe runs (0/0).
    rv, rh = timing["n_runner_visible"], timing["n_runner_hidden"]
    vc = timing["visibility_check"]
    if RENDER_FILLER:
        log(f"[BENCH] VISIBILITY GATE: runner on-screen {rv} steps / off-screen {rh} "
            f"steps (filler skipped) | visibility-check total {vc*1000:.0f}ms")
    else:
        log("[BENCH] FILLER DISABLED (RENDER_FILLER=0): decision frames only, "
            "no filler rendered; *_smooth holds 1 frame/step.")

    # ── Clean up render scratch dirs ──
    for d in [fpv_scratch, bird_scratch]:
        try: shutil.rmtree(d)
        except: pass

    # ── Generate media (HD + Preview) via gen_media.sh ──
    import subprocess
    gen_media_sh = os.path.join(os.path.dirname(SCRIPT_DIR), "gen_media.sh")
    if os.path.exists(gen_media_sh):
        log("[BENCH] Running gen_media.sh...")
        mr = subprocess.run(["bash", gen_media_sh, RUN_DIR], capture_output=True, text=True, timeout=600)
        log(f"[BENCH] gen_media rc={mr.returncode}")

    # ── 2D Trajectory Map ──
    log("[BENCH] Generating trajectory map...")
    try:
        import matplotlib; matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        fig, ax_plt = plt.subplots(1, 1, figsize=(12, 8))
        ax_plt.set_facecolor('#1a1a2e'); fig.patch.set_facecolor('#0f0f23')
        xs = [h["x"] for h in nav_hist]; ys = [h["y"] for h in nav_hist]
        actions = [h["action"] for h in nav_hist]
        act_colors = {"MOVE_FORWARD":"#00ff88","TURN_LEFT":"#ff6b6b","TURN_RIGHT":"#4ecdc4",
                      "DONE":"#ffd93d","PAUSE":"#a3a3a3","PICK_UP":"#c084fc","PUT_DOWN":"#f472b6",
                      "TURN_ON":"#fbbf24","TILT_UP":"#94a3b8","TILT_DOWN":"#64748b"}
        for i in range(len(xs)-1):
            c = act_colors.get(actions[i],"#888")
            ax_plt.plot([xs[i],xs[i+1]],[ys[i],ys[i+1]],color=c,linewidth=2,alpha=0.8)
            ax_plt.scatter(xs[i],ys[i],c=c,s=30,zorder=5,alpha=0.9)
        ax_plt.scatter(agent_start_xy[0],agent_start_xy[1],c='#ff4444',s=200,marker='*',zorder=10,label='Start',edgecolors='white',linewidths=1)
        for i, rt in enumerate(resolved_targets):
            ax_plt.scatter(rt[0],rt[1],c='#44ff44',s=200,marker='s',zorder=10,
                          label=f'Target {i+1}: {phases[i]["desc"][:20]}',edgecolors='white',linewidths=1)
            sc = mpatches.Circle((rt[0],rt[1]),phases[i]["radius"],linewidth=1.5,
                                edgecolor='#44ff44',facecolor='none',alpha=0.5,linestyle=':')
            ax_plt.add_patch(sc)
        if xs:
            ax_plt.scatter(xs[-1],ys[-1],c='#ffaa00',s=150,marker='D',zorder=10,
                          label=f'End (d={nav_hist[-1]["dist_to_target"]:.1f}m)',edgecolors='white',linewidths=1)
        for i in range(0,len(nav_hist),max(1,len(nav_hist)//15)):
            h = nav_hist[i]; yr = math.radians(h["yaw"])
            dx,dy = 0.4*math.cos(yr), 0.4*math.sin(yr)
            ax_plt.annotate('',xy=(h["x"]+dx,h["y"]+dy),xytext=(h["x"],h["y"]),
                           arrowprops=dict(arrowstyle='->',color='white',lw=1.5))
            ax_plt.text(h["x"]+dx*1.3,h["y"]+dy*1.3,str(h["step"]),fontsize=7,color='white',ha='center')
        lp = [mpatches.Patch(color=c,label=a) for a,c in act_colors.items()]
        ax_plt.legend(handles=lp+ax_plt.get_legend_handles_labels()[0],loc='upper right',
                     fontsize=8,facecolor='#2a2a4a',edgecolor='#444',labelcolor='white')
        ax_plt.set_xlabel('X (m)',color='white'); ax_plt.set_ylabel('Y (m)',color='white')
        ax_plt.set_title(f'{tid} [{level}]: {task["instruction"][:60]}',color='white',fontsize=12,fontweight='bold')
        ax_plt.tick_params(colors='white'); ax_plt.set_aspect('equal'); ax_plt.grid(True,alpha=0.2,color='white')
        for sp in ax_plt.spines.values(): sp.set_color('#444')
        plt.savefig(os.path.join(RUN_DIR,"trajectory_2d.png"),dpi=150,bbox_inches='tight',facecolor=fig.get_facecolor())
        plt.close(); log("[BENCH] Trajectory map saved")
    except Exception as e:
        log(f"[BENCH] Trajectory map error: {e}")

    log(f"[BENCH] Saved to {RUN_DIR}")
    log("[BENCH] All done!")
    sim_app.close()

except Exception as e:
    with open(LOG, "a") as f:
        f.write(f"\n[BENCH] FATAL ERROR:\n{traceback.format_exc()}")
    raise
