"""4DSynth-Nav benchmark runner. Runs ONE task inside Isaac Sim.
Usage: TASK_ID=01-L1 /isaac-sim/python.sh bench_runner.py
   or: TASK_JSON=/path/to/single_task.json /isaac-sim/python.sh bench_runner.py
"""
import sys, os, json, math, base64, glob, time, traceback, re
import urllib.request, datetime as _dt

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bench_helpers import (sample_human_motion, wrap_angle_deg, check_frame_quality,
                           make_nav_system_prompt, make_multistep_system_prompt,
                           discover_scene_files, find_prim_by_factory,
                           find_all_prims_by_factory, get_prim_world_center,
                           compute_metrics)
from semantic_classes import semantic_class_of

# ── Config ──
VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8300/v1/chat/completions")
MODEL_NAME = os.environ.get("MODEL_NAME", "Qwen/Qwen3-VL-30B-A3B-Thinking-FP8")
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
MAX_STEPS = int(os.environ.get("MAX_STEPS", "100"))
# Bird-view smooth video toggle. False (default, prototype speed): no bird
# _smooth folder, no bird filler frames — the bird video is built from the
# per-step decision frames in vlm_nav_frames_bird/. True: full bird _smooth
# folder with decision + filler frames (paper-quality, slower).
ENABLE_BIRD_SMOOTH = os.environ.get("ENABLE_BIRD_SMOOTH", "0") == "1"
# Render resolution for FPV + bird render products. PathTracing cost scales
# ~linearly with pixel count, so 960x540 renders ~4x faster than 1920x1080.
# Lowered for prototype iteration speed; set RENDER_W/RENDER_H env to override.
RENDER_W = int(os.environ.get("RENDER_W", "640"))
RENDER_H = int(os.environ.get("RENDER_H", "360"))

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
agent_start_xy = task["agent_start"]; agent_start_yaw = task["agent_yaw"]

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

log(f"[BENCH] Task={tid} Level={level} Scene={task['scene_dir']}")
log(f"[BENCH] Instruction: {task['instruction']}")
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
    """Query the VLM with one or more images (3-frame temporal context).
    img_paths: a single path (str) or list of paths [oldest, ..., newest]."""
    if isinstance(img_paths, str):
        img_paths = [img_paths]
    # Build image content entries
    img_content = []
    for ip in img_paths:
        with open(ip, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        img_content.append({"type":"image_url","image_url":{"url":f"data:image/png;base64,{b64}"}})
    payload = {"model": MODEL_NAME, "max_tokens": 4096, "temperature": 0.6,
               "messages": [{"role":"system","content":system_prompt},
                            {"role":"user","content":
                                img_content +
                                [{"type":"text","text":prompt}]}]}
    req = urllib.request.Request(VLLM_URL, json.dumps(payload).encode(),
                                {"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw_text = json.loads(resp.read())["choices"][0]["message"]["content"].strip()
        # Log raw response (including thinking, for debugging)
        resp_log = os.path.join(RUN_DIR, "vlm_responses.jsonl")
        with open(resp_log, "a") as f:
            f.write(json.dumps({"step":step,"response":raw_text}) + "\n")
        # Strip thinking tags if present (thinking-model compatibility)
        text = strip_thinking(raw_text)
        m = ACTION_RE.search(text.upper())
        if m:
            a = m.group(1).upper()
            return _PARSE_ALIASES.get(a, a), False
        # Fallback: last mentioned action (including aliases — STOP -> DONE)
        best, bi = "MOVE_FORWARD", -1
        for a in ALL_ACTIONS + list(_PARSE_ALIASES):
            i = text.upper().rfind(a)
            if i > bi: bi, best = i, a
        return _PARSE_ALIASES.get(best, best), True
    except Exception as e:
        log(f"[VLM] Error: {e}"); return "MOVE_FORWARD", True


def query_vlm_plan(img_path, prompt, system_prompt, step=0):
    """Query the VLM for a SEQUENCE of up to PLAN_LEN actions. Returns
    (actions_list, fallback_bool). The agent executes the list one action at a
    time until collision / target / list exhausted, then re-queries."""
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {"model": MODEL_NAME, "max_tokens": 4096, "temperature": 0.0,
               "messages": [{"role":"system","content":system_prompt},
                            {"role":"user","content":[
                                {"type":"image_url","image_url":{"url":f"data:image/png;base64,{b64}"}},
                                {"type":"text","text":prompt}]}]}
    req = urllib.request.Request(VLLM_URL, json.dumps(payload).encode(),
                                {"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = json.loads(resp.read())["choices"][0]["message"]["content"].strip()
        resp_log = os.path.join(RUN_DIR, "vlm_responses.jsonl")
        with open(resp_log, "a") as f:
            f.write(json.dumps({"step":step,"response":text}) + "\n")
        up = text.upper()
        m = PLAN_RE.search(up)
        if m:
            tokens = re.split(r"[,\s]+", m.group(1).strip())
            plan = [t for t in tokens if t in ALL_ACTIONS]
            if plan:
                return plan[:PLAN_LEN], False
        # Fallback: single action via the ACTION: format
        am = ACTION_RE.search(up)
        if am:
            return [am.group(1)], False
        # Last resort: collect any action keywords in order of appearance
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

    # ── Hide scene DomeLight to prevent sky bleed in bird's-eye view ──
    # The DomeLight at /World/Env/env_light projects an HDR sky texture
    # onto an infinite sphere. With the ceiling hidden, the bird-eye camera
    # sees through the void and catches the sky dome (which can be red/blue
    # depending on time-of-day). Its illumination contribution (0.25 intensity)
    # is negligible compared to our fill lights (5x 80000). Runner-scoped
    # DomeLights under /World/Humans/ are kept for runner mesh lighting.
    for p in stage.Traverse():
        if p.GetTypeName() == "DomeLight":
            pp = p.GetPath().pathString
            if pp.startswith("/World/Env/"):
                UsdGeom.Imageable(p).MakeInvisible()
                log(f"[BENCH] Hidden DomeLight {pp} (sky bleed prevention)")

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
            pp = find_prim_by_factory(stage, tobj)
            if pp:
                target_prim_paths.add(pp)
                c = get_prim_world_center(stage, pp)
                # Compute XY half-extent for edge-based distance
                he = 0.0
                try:
                    imageable = UsdGeom.Imageable(stage.GetPrimAtPath(pp))
                    bound = imageable.ComputeWorldBound(Usd.TimeCode.Default(), "default")
                    box = bound.GetBox()
                    mn, mx = box.GetMin(), box.GetMax()
                    bw, bd = mx[0]-mn[0], mx[1]-mn[1]
                    if 0 < bw < 100:
                        he = max(bw, bd) / 2.0
                except Exception:
                    pass
                resolved_half_extents.append(he)
                if c:
                    resolved_targets.append(c[:2])
                    log(f"[BENCH] Phase '{ph['name']}' -> {tobj} prim={pp} center={c[:2]} half_ext={he:.2f}m")
                else:
                    resolved_targets.append([5, 5])
                    log(f"[BENCH] WARNING: no bbox for {pp}")
                # Track pickup prims
                if ph["action"] == "PICK_UP" and not pickup_prim_path:
                    pickup_prim_path = pp
            else:
                resolved_targets.append([5, 5])
                resolved_half_extents.append(0.0)
                log(f"[BENCH] WARNING: no prim found for {tobj}")

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
    for container_path in ["/World/InteractiveProps", "/World/Env"]:
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
                child.SetActive(False)
                log(f"[BENCH] Deactivated same-semantic-class ({child_semantic}) "
                    f"non-target in {container_path}: {c_path}")

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
    # FILLER_FPS controls how many in-between frames are rendered per second of
    # sim-time while the VLM is thinking. Higher = smoother runner motion.
    # Lowered 6 -> 3 to roughly halve filler render load (prototype speed).
    FILLER_FPS = 10.0  # match anim fps to avoid trajectory undersampling at corners
    FILLER_SUBFRAMES = 4   # filler frames are video-only -> cheap PathTracing
    DECISION_SUBFRAMES = 16  # decision frames (VLM sees these) stay full quality
    # Render at least this many filler frames per step so the runner visibly
    # progresses even on a fast VLM response.
    MIN_FILLER_FRAMES = 2
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
                        for sz in [0.5, 1.0]:
                            h = query_if.sweep_sphere_closest(
                                0.40, carb.Float3(ox, oy, sz),
                                carb.Float3(dx, dy, 0), dist + 0.15)
                            if h["hit"]:
                                wp = (h.get("rigidBody") or
                                      h.get("collider") or "").lower()
                                if any(w in wp for w in
                                       ("floor","ground","rug","blanket",
                                        "towel","mat")):
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
                ax += nx * primary
                ay += ny * primary
                if slide > 0:
                    ax += px * slide
                    ay += py * slide

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
    vlm_calls = 0   # episode counter (== step count in single-action mode)

    log(f"[BENCH] Starting nav loop: start=({ax},{ay}) yaw={ayaw}")

    # ── Spawn nudge: ensure agent doesn't overlap static obstacles ──
    # Uses PhysX sweep to detect immediate overlaps and spiral-searches
    # for the nearest clear position. Saves adjustment details for review.
    import omni.physx, carb
    sim_app.update()
    query_if = omni.physx.get_physx_scene_query_interface()

    WALKABLE_SPAWN = ("floor", "ground", "rug", "blanket", "towel", "mat")
    _8DIRS = [(1,0),(-1,0),(0,1),(0,-1),
              (0.707,0.707),(-0.707,0.707),(0.707,-0.707),(-0.707,-0.707)]

    def _check_spawn_clear(cx, cy):
        """Return (is_clear, worst_hit_path, worst_dist) for a candidate spawn."""
        for sz in [0.5, 1.0]:
            for dx, dy in _8DIRS:
                h = query_if.sweep_sphere_closest(
                    0.40, carb.Float3(cx, cy, sz),
                    carb.Float3(dx, dy, 0), 0.05)
                if h["hit"]:
                    wp = (h.get("rigidBody") or h.get("collider") or "").lower()
                    if any(w in wp for w in WALKABLE_SPAWN):
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
            # Fix FPV clipping into NPC or own arms when close
            nav_cam_prim = UsdGeom.Camera(nav_cam)
            if nav_cam_prim:
                nav_cam_prim.CreateClippingRangeAttr().Set(Gf.Vec2f(0.3, 10000.0))

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

        # ── Build prompt for this step ──
        # Single-action mode: the VLM picks ONE action from the current frame.
        # (Multi-action planning was tried but caused regressions: the VLM
        # would commit to several MOVE_FORWARDs from a single frame, walking
        # into obstacles it would have noticed on the next frame.)
        if is_multi:
            inv_s = ','.join(inventory) if inventory else 'empty'
            lamp_s = " Lamp: ON." if lamp_on else ""
            prompt = (f"Current objective: go to {ph['desc']} and use {ph['action']}. "
                      f"Carrying: [{inv_s}].{lamp_s} Progress: step {cur_phase+1}/{len(phases)}. "
                      f"What action should you take?")
        else:
            prompt = f"Navigate to {ph['desc']}. What action should you take?"
        if action_fb:
            prompt += f" ⚠ PREVIOUS ACTION FAILED: {action_fb}"

        # ── Recent action history (per-step, no plan-queue layer) ──
        if nav_hist:
            recent = nav_hist[-8:]
            hlines = []
            for h in recent:
                ms = "BLOCKED" if h.get("blocked") else ("moved" if h.get("moved") else "no movement")
                hlines.append(f"Step {h['step']}: {h['action']} ({ms}, yaw={h['yaw']:.0f}°)")
            prompt += " Recent history:\n" + "\n".join(hlines)
            # No-progress warning. PAUSE counts as no-movement, so a PAUSE run
            # will trip this just like any other stuck pattern.
            rm = [h.get("moved", True) for h in nav_hist[-3:]]
            if len(rm) >= 3 and not any(rm):
                prompt += "\n⚠ WARNING: You have NOT moved for 3+ steps. Try a different direction."
            # PAUSE hard cap — neutral wording, no scenario assumption.
            recent_pause = [h["action"] == "PAUSE" for h in nav_hist[-PAUSE_HARDCAP:]]
            if len(recent_pause) >= PAUSE_HARDCAP and all(recent_pause):
                prompt += f"\nYou have paused {PAUSE_HARDCAP} times consecutively. Choose a different action now."

        fq = check_frame_quality(frame_path)
        if fq.get('guidance'): prompt += fq['guidance']

        # ── Build 3-frame temporal context (current + 2 previous) ──
        # Gives the VLM visual history to reason about stuck situations.
        # Fallback: fewer frames for step 0/1.
        vlm_frames = []
        for prev_step in [step - 2, step - 1]:
            if prev_step >= 0:
                prev_path = os.path.join(fpv_dir, f"rgb_{prev_step:04d}.png")
                if os.path.exists(prev_path):
                    vlm_frames.append(prev_path)
        vlm_frames.append(frame_path)  # current frame is always last

        # ── Query VLM concurrently; render filler frames while it thinks ──
        # The agent holds its pose (it has not decided yet) but the runner keeps
        # moving, so the *_smooth video stays leap-free.
        import threading
        vlm_result = {}
        def _vlm_worker():
            vlm_result["out"] = query_vlm(vlm_frames, prompt, sys_prompt, step)
        _t_vlm = time.time()
        vlm_thread = threading.Thread(target=_vlm_worker)
        vlm_thread.start()
        filler_t = sim_t

        # Visibility gate — skip filler renders if the runner is off-screen
        # for the whole step. Scan a 3s window of sim-time (cheap geometry).
        VIS_SCAN_WINDOW = 3.0; VIS_SCAN_POINTS = 16
        _t_vis = time.time()
        runner_on_screen = any(
            runner_visible(ax, ay, ayaw,
                           sim_t + VIS_SCAN_WINDOW * k / (VIS_SCAN_POINTS - 1))
            for k in range(VIS_SCAN_POINTS))
        timing["visibility_check"] += time.time() - _t_vis
        timing["n_runner_visible" if runner_on_screen else "n_runner_hidden"] += 1

        if runner_on_screen:
            filler_n = 0
            while vlm_thread.is_alive() or filler_n < MIN_FILLER_FRAMES:
                filler_t += 1.0 / FILLER_FPS
                # Resolve agent-runner overlap at this filler timestep and
                # follow the (possibly-pushed) agent position with the camera.
                # The agent's facing (ayaw) is held — only XY can shift from
                # being bumped.
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
                filler_n += 1
        else:
            vlm_thread.join()
            filler_t = sim_t + RUNNER_TIME_PER_STEP
        vlm_thread.join()
        timing["vlm"] += time.time() - _t_vlm
        timing["n_vlm"] += 1
        action, fallback = vlm_result.get("out", ("MOVE_FORWARD", True))
        vlm_calls += 1
        sim_t = filler_t
        _vis_tag = "runner visible" if runner_on_screen else "runner off-screen, no filler"
        log(f"[BENCH] Step {step}: action={action} ({_vis_tag})")
        action_fb = ""

        # DONE confirm — verify with a single-action query before accepting
        if action == "DONE":
            for cr in range(1, DONE_CONFIRM):
                ca, _ = query_vlm(frame_path, "You chose DONE. Is the target within arm's reach? Confirm.", sys_prompt, step)
                if ca != "DONE": action = ca; break

        pre_x, pre_y = ax, ay
        pre_phase = cur_phase
        pre_fb = action_fb
        nav_hist.append({"step":step,"x":round(ax,3),"y":round(ay,3),"yaw":round(ayaw,1),
                         "dist_to_target":round(dist,3),"action":action,
                         "moved":False,"blocked":False,
                         "blocked_reason":None,"blocked_detail":None})

        # ── Execute action ──
        if action == "DONE":
            if ph["action"] == "DONE" and dist < tgt_radius:
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
                    UsdGeom.Imageable(pickup_prim).MakeInvisible()
                cur_phase += 1
                log(f"[BENCH] PICK_UP success, advancing to phase {cur_phase+1}")
                if cur_phase < len(phases):
                    tgt = resolved_targets[cur_phase]
            else:
                action_fb = "PICK_UP failed: too far."
                log(f"[BENCH] PICK_UP failed dist={dist:.2f}")

        elif action == "PUT_DOWN":
            if ph["action"] == "PUT_DOWN" and dist < tgt_radius and inventory:
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
            for sz in [0.5, 1.0]:
                if blocked:
                    break
                hit = query_if.sweep_sphere_closest(0.40, carb.Float3(ax,ay,sz),
                                                     carb.Float3(dx,dy,0), STEP_DIST)
                if not hit["hit"]:
                    continue
                hit_path = (hit.get("rigidBody") or hit.get("collider") or "").lower()
                # Ignore thin floor-level objects — the sweep sphere bottom
                # (z=sz-0.40) sits below the floor top, so PhysX reports an
                # overlap at distance 0. These are not real obstacles:
                #   floor/ground  — floor meshes
                #   rug           — RugFactory (flat textile, ~1cm thick)
                #   blanket/towel — draped textiles that may touch the floor
                WALKABLE = ("floor", "ground", "rug", "blanket", "towel", "mat")
                if any(w in hit_path for w in WALKABLE):
                    continue
                hit_info = (sz, hit_path, hit.get("distance", -1))
                blocked = True; break
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
        nav_hist[-1]["moved"] = did_move
        # sim_t was already advanced this step by the filler loop (or by
        # RUNNER_TIME_PER_STEP if the runner was off-screen). Single-action
        # mode queries the VLM every step, so there's no "no-VLM-this-step"
        # path that would leave sim_t un-advanced.
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
    results = {"task": task, "metrics": metrics, "nav_history": nav_hist,
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
    # (filler rendered) vs off-screen (filler skipped — render saved).
    rv, rh = timing["n_runner_visible"], timing["n_runner_hidden"]
    vc = timing["visibility_check"]
    log(f"[BENCH] VISIBILITY GATE: runner on-screen {rv} steps / off-screen {rh} "
        f"steps (filler skipped) | visibility-check total {vc*1000:.0f}ms")

    # ── Clean up render scratch dirs ──
    for d in [fpv_scratch, bird_scratch]:
        try: shutil.rmtree(d)
        except: pass

    # ── Generate media (HD + Preview) via gen_media.sh ──
    import subprocess
    gen_media_sh = "/home/qi/hc/Puppeteer/zehao_task/gen_media.sh"
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
