"""
VLM Navigation Benchmark: Closed-Loop Navigation with Dynamic Obstacles.

Architecture:
- Agent: Runner 2 (instanced from runner model), controlled by VLM actions
- Obstacle: Runner 1, running on original pre-baked trajectory  
- VLM: Qwen3-VL-30B-A3B via vLLM on localhost:8300
- Action Space: {MOVE_FORWARD, TURN_LEFT, TURN_RIGHT, STOP}
- Step Size: MOVE_FORWARD=0.25m, TURN=30°
- Success: Agent STOPs within 1.0m of target
- Max Steps: 100

Design Decisions:
- VLM queried once per navigation step (not every render frame)
- Runner 1 advances 0.5s per step (continuous motion)
- Each step: render → query VLM → apply action → next step
- GIF generated from all step frames at the end
"""
import sys, traceback, json, os, math, base64, glob
import urllib.request
from typing import Any, Dict, List, Tuple

# ============================================================
# Motion sampling (from Zehao's runtime, zero external deps)
# ============================================================
def lerp(a, b, alpha): return float(a)*(1.0-alpha)+float(b)*alpha
def lerp_angle_deg(a, b, alpha):
    delta = ((float(b)-float(a)+180.0)%360.0)-180.0
    return float(a)+delta*float(alpha)
def wrap_angle_deg(v): return ((float(v)+180.0)%360.0)-180.0

def trajectory_loop_mode(hs):
    mode = str(hs.get("trajectory_loop_mode","") or "").strip().lower()
    if mode in {"ping_pong_180_turn","pingpong_180_turn","ping_pong"}: return "ping_pong_180_turn"
    return "repeat"

def _looped_frame(hs, elapsed_s, fps, sf, ef, fcl):
    fcl = max(1, int(fcl or 1))
    mode = trajectory_loop_mode(hs)
    if mode == "ping_pong_180_turn":
        full = float(max(2, 2*fcl))
        fo = (float(elapsed_s)*float(fps)) % full
        if fo < float(fcl): return float(sf)+fo, False
        return float(ef)-(fo-float(fcl)), True
    return float(sf)+(float(elapsed_s)*float(fps))%float(fcl), False

def sample_human_motion(hs, elapsed_s, fps):
    samples = hs.get("trajectory_keyframes_world", [])
    if not isinstance(samples, list) or not samples:
        rot = list(hs.get("rotation_deg_xyz",[0,0,0]))[:3]
        pos = list(hs.get("placement_location_m",[0,0,0]))[:3]
        while len(rot)<3: rot.append(0.0)
        while len(pos)<3: pos.append(0.0)
        return pos, rot
    ordered = sorted(samples, key=lambda x: int(x.get("frame",1) or 1))
    sf = int(ordered[0].get("frame",1) or 1)
    ef = int(ordered[-1].get("frame",sf) or sf)
    cl = int(hs.get("trajectory_cycle_frame_count", ef-sf+1) or (ef-sf+1))
    loop = bool(hs.get("loop_trajectory",False)) and cl>1 and len(ordered)>=2
    if loop:
        ff, rev = _looped_frame(hs, elapsed_s, fps, sf, ef, cl)
    else:
        ff = min(float(ef), sf+float(elapsed_s)*float(fps))
        rev = False
    if ff <= float(sf): sa,sb,alpha = ordered[0],ordered[0],0.0
    elif ff >= float(ef): sa,sb,alpha = ordered[-1],ordered[-1],0.0
    else:
        sa,sb,alpha = ordered[0],ordered[-1],0.0
        for i in range(len(ordered)-1):
            a,b = ordered[i],ordered[i+1]
            fa = float(a.get("frame",sf) or sf)
            fb = float(b.get("frame",sf) or sf)
            if fa <= ff <= fb:
                sa,sb = a,b
                alpha = 0.0 if abs(fb-fa)<1e-6 else (ff-fa)/(fb-fa)
                break
    ta = list(sa.get("translation_m", hs.get("placement_location_m",[0,0,0])))[:3]
    tb = list(sb.get("translation_m", ta))[:3]
    ra = list(sa.get("rotation_deg_xyz", hs.get("rotation_deg_xyz",[0,0,0])))[:3]
    rb = list(sb.get("rotation_deg_xyz", ra))[:3]
    while len(ta)<3: ta.append(0.0)
    while len(tb)<3: tb.append(0.0)
    while len(ra)<3: ra.append(0.0)
    while len(rb)<3: rb.append(0.0)
    pos = [lerp(ta[i],tb[i],alpha) for i in range(3)]
    rot = [lerp(ra[0],rb[0],alpha), lerp(ra[1],rb[1],alpha), lerp_angle_deg(ra[2],rb[2],alpha)]
    if rev: rot[2] = wrap_angle_deg(rot[2]+180.0)
    return pos, rot

# ============================================================
# VLM API client (using stdlib urllib, no pip deps needed)
# ============================================================
VLLM_URL = "http://localhost:8300/v1/chat/completions"
MODEL_NAME = "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8"

# ============================================================
# Frame quality analysis (pixel-based black/blank detection)
# ============================================================
def check_frame_quality(image_path: str) -> dict:
    """Analyse frame brightness to detect black/overexposed frames.
    Returns dict with 'mean_brightness', 'is_dark', 'is_overexposed', 'guidance'.
    """
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(image_path).convert('L')  # grayscale
        pixels = np.array(img)
        mean_val = float(pixels.mean())
        dark_frac = float((pixels < 15).sum()) / pixels.size
        bright_frac = float((pixels > 240).sum()) / pixels.size

        result = {'mean_brightness': mean_val, 'is_dark': False, 'is_overexposed': False, 'guidance': ''}

        if mean_val < 12 or dark_frac > 0.85:
            result['is_dark'] = True
            result['guidance'] = (
                " VISUAL WARNING: The current camera frame is almost entirely BLACK. "
                "This usually means you are facing a wall or large obstacle at very close range. "
                "Consider turning left or right to find an open path with visible room features. "
                "Moving forward in this state will likely result in a collision."
            )
        elif mean_val > 235 or bright_frac > 0.85:
            result['is_overexposed'] = True
            result['guidance'] = (
                " VISUAL WARNING: The current camera frame is almost entirely WHITE/overexposed. "
                "This usually means you are pressed against a bright wall or obstacle. "
                "Consider turning to find an open area with visible furniture and room features."
            )
        return result
    except Exception:
        return {'mean_brightness': -1, 'is_dark': False, 'is_overexposed': False, 'guidance': ''}

def make_system_prompt(target_desc: str) -> str:
    """Generate a target-aware system prompt."""
    return f"""You are a navigation robot inside a living room. Your goal is to reach the {target_desc}.

You see the room from your first-person camera. There may be a person running across the room - you must avoid colliding with them.

You can ONLY output ONE of these actions:
- MOVE_FORWARD (move 0.25 meters in your current facing direction)
- TURN_LEFT (rotate 15 degrees to the left)  
- TURN_RIGHT (rotate 15 degrees to the right)
- STOP (you believe you have reached the target)

Rules:
- If you see the target directly ahead and close, move toward it.
- If the target is to your left or right, turn toward it first.
- If a person is blocking your path, turn to find an alternate route. Do NOT use STOP for waiting.
- When you are very close to the target (within arm's reach), output STOP.

First, briefly explain your reasoning based on what you see. 
Then, as the VERY LAST line of your response, output ONLY the single chosen action in this exact format:
ACTION: <action_name>"""

def make_multistep_system_prompt(task_instruction):
    """Generate a system prompt for multi-step tasks with PICK_UP/PUT_DOWN."""
    return f"""You are a service robot inside a living room. Your task:
{task_instruction}

You can ONLY output ONE of these actions:
- MOVE_FORWARD (move 0.25 meters in your current facing direction)
- TURN_LEFT (rotate 15 degrees to the left)
- TURN_RIGHT (rotate 15 degrees to the right)
- PICK_UP (pick up an object near you — only works when you are very close to the object)
- PUT_DOWN (put down the object you are carrying — only works near the target location)
- STOP (you have completed the final step of the task)

Rules:
- Think step by step: what sub-task should I do next?
- You must be VERY CLOSE to an object to PICK_UP or PUT_DOWN.
- If you are carrying an object, navigate to where you need to put it down.
- Only use STOP after ALL steps of the task are done.

First, briefly explain your reasoning based on what you see.
Then, as the VERY LAST line of your response, output ONLY the single chosen action in this exact format:
ACTION: <action_name>"""

def query_vlm(image_path: str, out_log: str, collision_alert: bool = False, action_history: list = None, step: int = 0, frame_quality: dict = None, nav_history_records: list = None, task_phase_info: dict = None) -> tuple:
    """Send image to VLM, return (action_string, is_fallback)."""
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")
    
    if task_phase_info:
        ph_desc = task_phase_info['desc']
        ph_action = task_phase_info['action']
        inv_str = task_phase_info.get('inventory', 'empty')
        ph_idx = task_phase_info['phase_idx']
        ph_total = task_phase_info['phase_total']
        prompt = (
            f"You are performing a multi-step task. Current objective: go to {ph_desc} and use {ph_action}. "
            f"Carrying: [{inv_str}]. Progress: step {ph_idx+1}/{ph_total}. "
            f"Based on this first-person view, what action should you take? First explain your reasoning, then output the action."
        )
    else:
        prompt = f"You are navigating an indoor environment to reach the {TARGET_DESC}. Based on this first-person view, what action should you take? First explain your reasoning, then output the action."
    if action_history and nav_history_records:
        recent = nav_history_records[-8:]  # last 8 steps
        history_lines = []
        for h in recent:
            if h.get("blocked", False):
                moved = "BLOCKED by obstacle"
            elif h.get("moved", False):
                moved = "moved"
            else:
                moved = "no movement"
            history_lines.append(f"Step {h['step']}: {h['action']} ({moved}, yaw={h['yaw']:.0f}°)")
        prompt += f" Recent navigation history:\n" + "\n".join(history_lines)
        # Detect if agent has been stuck (no movement for 3+ steps)
        recent_moved = [h.get("moved", True) for h in nav_history_records[-3:]]
        if len(recent_moved) >= 3 and not any(recent_moved):
            prompt += "\n⚠ WARNING: You have NOT moved for the last 3+ steps. You are likely stuck. Try a different direction."
    if collision_alert:
        prompt += " WARNING: Your path is currently blocked by an obstacle. You MUST turn to find a clear path."
    # Inject frame quality guidance (pixel-analysis driven)
    if frame_quality and frame_quality.get('guidance'):
        prompt += frame_quality['guidance']

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                {"type": "text", "text": prompt}
            ]}
        ],
        "max_tokens": 4096,
        "temperature": 0.0,
    }
    
    req = urllib.request.Request(
        VLLM_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            text = result["choices"][0]["message"]["content"].strip()
            
            # Log the full response reasoning to a jsonl file
            resp_log = os.path.join(RUN_DIR, "vlm_responses.jsonl")
            with open(resp_log, "a") as f:
                f.write(json.dumps({"step": step, "image": image_path, "response": text}) + "\n")

            text_upper = text.upper()
            
            # Extract valid action from response
            import re
            match = re.search(r"ACTION:\s*(MOVE_FORWARD|TURN_LEFT|TURN_RIGHT|STOP|PICK_UP|PUT_DOWN)", text_upper)
            if match:
                return match.group(1), False
            else:
                # Fallback: search backwards to find the final chosen action
                best_action = "MOVE_FORWARD"
                best_idx = -1
                for action in ["MOVE_FORWARD", "TURN_LEFT", "TURN_RIGHT", "STOP", "PICK_UP", "PUT_DOWN"]:
                    idx = text_upper.rfind(action)
                    if idx > best_idx:
                        best_idx = idx
                        best_action = action
                
                if best_idx == -1:
                    with open(out_log, "a") as f: f.write(f"[VLM] Unrecognized response: {text[:100]}\n")
            
                return best_action, True
    except Exception as e:
        with open(out_log, "a") as f: f.write(f"[VLM] API error: {e}\n")
        return "MOVE_FORWARD", True  # Fallback on error

def query_vlm_confirm_stop(image_path: str, out_log: str, step: int = 0) -> str:
    """Re-query VLM with a skeptical prompt to confirm a STOP decision."""
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")
    
    confirm_prompt = (
        "You just chose STOP. Look again carefully: "
        "is the target IMMEDIATELY in front of you, large in your view, and within arm's reach? "
        "If it is still distant, continue navigating. "
        "Output your final action on the last line as: ACTION: <action_name>"
    )
    
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                {"type": "text", "text": confirm_prompt}
            ]}
        ],
        "max_tokens": 4096,
        "temperature": 0.0,
    }
    
    req = urllib.request.Request(
        VLLM_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            text = result["choices"][0]["message"]["content"].strip()
            resp_log = os.path.join(RUN_DIR, "vlm_responses.jsonl")
            with open(resp_log, "a") as f:
                f.write(json.dumps({"step": step, "type": "stop_confirm", "image": image_path, "response": text}) + "\n")
            text_upper = text.upper()
            import re
            match = re.search(r"ACTION:\s*(MOVE_FORWARD|TURN_LEFT|TURN_RIGHT|STOP|PICK_UP|PUT_DOWN)", text_upper)
            if match:
                return match.group(1)
            best_action, best_idx = "MOVE_FORWARD", -1
            for a in ["MOVE_FORWARD", "TURN_LEFT", "TURN_RIGHT", "STOP", "PICK_UP", "PUT_DOWN"]:
                idx = text_upper.rfind(a)
                if idx > best_idx:
                    best_idx, best_action = idx, a
            return best_action
    except Exception as e:
        with open(out_log, "a") as f: f.write(f"[VLM] Confirm API error: {e}\n")
        return "MOVE_FORWARD"  # On error, keep navigating

# ============================================================
# Navigation config
# ============================================================
STEP_DISTANCE = 0.25     # meters per MOVE_FORWARD
TURN_ANGLE = 15.0        # degrees per TURN
MAX_STEPS = 250        # timeout
STOP_CONFIRM_ROUNDS = 2  # require 2 consecutive STOP predictions to accept
# BBox-calibrated ground contact Z offsets (measured via check_dancer_bbox.py).
# These are the translate-Z values that place each mesh's feet exactly at floor
# level (Z=0) when scaled to runner_scale (0.5326).  The old root_offset_m
# approach was wrong: root_offset_m ≠ mesh-space feet distance.
RUNNER_MESH_GROUND_Z = 0.6773   # obj_1_run_anim_1.usdc  (agent uses same mesh)
DANCER_MESH_GROUND_Z = 0.8961   # obj_2_dance_anim_2.usdc
AGENT_EYE_HEIGHT = 1.58  # z for camera (eye level)
RUNNER_TIME_PER_STEP = 0.5  # seconds of runner animation per nav step

# Camera pitch: tilt down to see low furniture (sofa, coffee table).
# Habitat Challenge also tilts camera for realistic robot behavior (Hello Stretch).
CAMERA_PITCH_DEG = -10   # degrees (negative = look down)



# Agent mesh default facing direction → yaw=0 is +X.
# The mesh actually faces -Y, so we rotate +90° to align with +X.
# (was -90° which caused the agent to face backwards)
AGENT_MESH_YAW_OFFSET = +90.0  # degrees

# ============================================================
# Target configurations
# ============================================================
TARGET_CONFIGS = {
    "sofa": {
        "coords": [4.37, 6.43],
        "success_radius": 3.0,  # sofa bbox is ~2.58m; 3m from center = at the near edge
        "desc": "SOFA (the large light-green couch)",
        # Ground truth from scene_inventory.json (bbox_min/max in meters)
        "bbox": [3.08, 5.14, 5.66, 7.72],
    },
    "bookshelf": {
        "coords": [0.34, 8.76],
        "success_radius": 1.5,  # shelf is narrow (~0.27m deep, 1.19m wide)
        "desc": "tall white 4-tier SHELF (the tall one with items on it, NOT the short 2-tier bookcase)",
        # Ground truth from scene_inventory.json
        "bbox": [0.20, 8.16, 0.47, 9.36],
    },
    "book_return": {
        "task_type": "multi_step",
        "coords": [8.0, 6.0],  # initial target = book on floor (phase 0)
        "success_radius": 1.0,
        "desc": "book return task",
        "instruction": "Pick up the book from the floor, put it on the tall 4-tier bookshelf, then go to the black door.",
        "phases": [
            {"name": "pick_up_book", "target": [8.0, 6.0], "radius": 1.0,
             "action": "PICK_UP", "desc": "the book on the floor"},
            {"name": "put_on_shelf", "target": [0.34, 8.76], "radius": 1.2,
             "action": "PUT_DOWN", "desc": "the tall 4-tier bookshelf"},
            {"name": "go_to_door", "target": [6.0, 11.7], "radius": 1.5,
             "action": "STOP", "desc": "the black door"},
        ],
        # Book prim to relocate to floor
        "book_prim": "/World/Env/BookStackFactory_3931954__spawn_asset_7414082_",
        "book_floor_pos": [8.0, 6.0, 0.15],
    },
}

# === SELECT TARGET (override via env: NAV_TARGET=bookshelf) ===
TARGET_NAME = os.environ.get("NAV_TARGET", "sofa")

_cfg = TARGET_CONFIGS[TARGET_NAME]
IS_MULTI_STEP = _cfg.get("task_type") == "multi_step"
TARGET = _cfg["coords"]
SUCCESS_RADIUS = _cfg["success_radius"]
TARGET_DESC = _cfg["desc"]

if IS_MULTI_STEP:
    SYSTEM_PROMPT = make_multistep_system_prompt(_cfg["instruction"])
else:
    SYSTEM_PROMPT = make_system_prompt(TARGET_DESC)

# Agent start: on the rug, facing roughly toward sofa
AGENT_START_X = 12.0
AGENT_START_Y = 4.0
AGENT_START_YAW = 160.0  # degrees, roughly facing the sofa (upper-left)

# === Per-run output directory ===
import datetime as _dt
RUN_DIR = os.path.join("/home/qi/hc/Puppeteer/zehao_task/runs",
                       f"{TARGET_NAME}_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}")
os.makedirs(RUN_DIR, exist_ok=True)

def get_camera_lookat(pos, target):
    from pxr import Gf
    mat = Gf.Matrix4d().SetLookAt(pos, target, Gf.Vec3d(0, 0, 1))
    qd = mat.GetInverse().ExtractRotation().GetQuat()
    return Gf.Quatf(qd.GetReal(), *qd.GetImaginary())

def get_camera_quat_from_yaw(yaw_deg, pitch_deg=0.0):
    """Compute camera orientation quaternion from yaw (horizontal) and pitch (vertical tilt).
    
    Args:
        yaw_deg: horizontal rotation (0° = +X, 90° = +Y)
        pitch_deg: vertical tilt (negative = look down, positive = look up)
    """
    from pxr import Gf
    import math
    yaw_rad = math.radians(yaw_deg)
    pitch_rad = math.radians(pitch_deg)
    
    eye = Gf.Vec3d(0.0, 0.0, 0.0)
    # Tilt the look-at target downward by pitch angle
    target = Gf.Vec3d(
        math.cos(yaw_rad) * math.cos(pitch_rad),
        math.sin(yaw_rad) * math.cos(pitch_rad),
        math.sin(pitch_rad))
    up = Gf.Vec3d(0.0, 0.0, 1.0)
    
    mat = Gf.Matrix4d().SetLookAt(eye, target, up)
    qd = mat.GetInverse().ExtractRotation().GetQuat()
    return Gf.Quatf(qd.GetReal(), *qd.GetImaginary())

# ============================================================
# Main
# ============================================================
out_log = os.path.join(RUN_DIR, "vlm_nav.log")
with open(out_log, "w") as f: f.write(f"[NAV] Starting VLM navigation benchmark (target={TARGET_NAME})...\n")
with open(out_log, "a") as f: f.write(f"[NAV] Run dir: {RUN_DIR}\n")

try:
    from isaacsim import SimulationApp
    with open(out_log, "a") as f: f.write("[NAV] Loading SimulationApp...\n")
    simulation_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})
    
    import omni.usd
    import omni.replicator.core as rep
    from omni.isaac.core.utils.stage import open_stage, is_stage_loading
    from pxr import Gf, UsdGeom, UsdLux
    
    scene_dir = "/home/qi/hc/Puppeteer/zehao_new_folder/phy_env/case11_multi_surface_turn_right_full_physics_scene"
    usd_path = os.path.join(scene_dir, "compiled_stages", "case11_multi_surface_turn_right_full_physics.compiled.usda")
    spec_path = os.path.join(scene_dir, "compiled_specs", "case11_multi_surface_turn_right_full_physics.compiled.spec.json")
    human_usd = os.path.join(scene_dir, "assets", "humans", "obj_1_run_anim_1.usdc")
    
    spec = json.load(open(spec_path))
    humans_spec = spec.get("humans", [])
    active_humans = spec.get("active_humans", [])
    anim_fps = float(spec.get("stage", {}).get("time_codes_per_second", 10.0) or 10.0)
    
    with open(out_log, "a") as f: f.write(f"[NAV] Loading stage...\n")
    open_stage(usd_path)
    while is_stage_loading():
        simulation_app.update()
    stage = omni.usd.get_context().get_stage()
    with open(out_log, "a") as f: f.write("[NAV] Stage loaded!\n")
    
    # Hide ceiling for bird's-eye view
    ceiling_prim = stage.GetPrimAtPath("/World/Env/living_room_0_0_ceiling")
    if ceiling_prim.IsValid():
        UsdGeom.Imageable(ceiling_prim).MakeInvisible()
    
    # Remove the second sofa (right side, near start) to avoid VLM ambiguity
    # SetActive(False) removes it entirely: no visuals, no collision, no physics
    sofa2_prim = stage.GetPrimAtPath("/World/Env/SofaFactory_1079026__spawn_asset_4034957_")
    if sofa2_prim.IsValid():
        sofa2_prim.SetActive(False)
        with open(out_log, "a") as f: f.write("[NAV] Removed Sofa 2 (right side) from scene\n")
    
    # Instance agent runner (Do NOT use 'Xform' or it overrides 'SkelRoot' and won't render)
    agent_prim = stage.DefinePrim("/World/Humans/agent_runner")
    agent_prim.GetReferences().AddReference(human_usd)
    simulation_app.update()
    
    # Lighting — soft SphereLights inside the room
    # (DomeLight doesn't work: it's blocked by walls/ceiling in enclosed rooms)
    for i, lp in enumerate([(5,6,2.3),(10,6,2.3),(15,6,2.3),(5,2,2.3),(10,10,2.3)]):
        light_prim = UsdLux.SphereLight.Define(stage, f"/World/Lights/SphereLight_{i}")
        light_prim.CreateIntensityAttr().Set(80000.0)
        light_prim.CreateRadiusAttr().Set(0.3)  # 30cm radius: soft, uniform illumination
        xf = UsdGeom.Xformable(light_prim)
        xf.ClearXformOpOrder()
        xf.AddTranslateOp().Set(Gf.Vec3d(*lp))
    
    # Warm up
    for _ in range(100):
        simulation_app.update()
        
    import omni.kit.commands
    omni.kit.commands.execute("ChangeSetting", path="/rtx/rendermode", value="PathTracing")
    omni.kit.commands.execute("ChangeSetting", path="/rtx/pathtracing/spp", value=16)
    
    out_dir_fpv = os.path.join(RUN_DIR, "vlm_nav_frames_fpv")
    out_dir_bird = os.path.join(RUN_DIR, "vlm_nav_frames_bird")
    
    for d in [out_dir_fpv, out_dir_bird]:
        import shutil
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d, exist_ok=True)
    
    # === Setup FPV camera (VLM input) ===
    # Use pure USD Camera instead of rep.create.camera to prevent Replicator OmniGraph
    # from overriding our manual per-step pose updates.
    nav_cam_path = "/World/NavCamera"
    cam_fpv_prim = UsdGeom.Camera.Define(stage, nav_cam_path)
    
    # Set to a standard 90-degree wide Field of View (FOV)
    cam_fpv_prim.CreateFocalLengthAttr().Set(17.0)
    cam_fpv_prim.CreateHorizontalApertureAttr().Set(34.0)
    
    rp_fpv = rep.create.render_product(str(nav_cam_path), (1920, 1080))
    writer_fpv = rep.WriterRegistry.get("BasicWriter")
    writer_fpv.initialize(output_dir=out_dir_fpv, rgb=True)
    writer_fpv.attach([rp_fpv])

    # Bird's-eye camera — 3/4 angle from agent-start corner, sees entire room
    # Room bounds: X=[0,18] Y=[0.13,11.87] Z=[0.13,2.71]
    cam_bird_path = "/World/BirdCamera"
    cam_bird_prim = UsdGeom.Camera.Define(stage, cam_bird_path)
    cam_bird_prim.CreateFocalLengthAttr().Set(18.0)   # moderate wide-angle (less distortion)
    cam_bird_prim.CreateHorizontalApertureAttr().Set(34.0)
    
    bird_xf = UsdGeom.Xformable(cam_bird_prim)
    bird_xf.ClearXformOpOrder()
    b_trans = bird_xf.AddTranslateOp()
    b_orient = bird_xf.AddOrientOp()
    # Position near agent-start corner, below ceiling; look toward sofa/room center
    bird_pos = Gf.Vec3d(15.0, 2.0, 2.4)
    bird_target = Gf.Vec3d(5.0, 7.0, 0.3)
    b_trans.Set(bird_pos)
    b_orient.Set(get_camera_lookat(bird_pos, bird_target))

    rp_bird = rep.create.render_product(str(cam_bird_path), (1920, 1080))
    writer_bird = rep.WriterRegistry.get("BasicWriter")
    writer_bird.initialize(output_dir=out_dir_bird, rgb=True)
    writer_bird.attach([rp_bird])
    
    # === Read correct animation bindings from active_humans ===
    runner1_binding = {}
    runner1_human_spec = None
    for ah in active_humans:
        if "run" in ah.get("name", ""):
            runner1_binding = ah.get("animation_binding", {})
            runner1_human_spec = ah
            break
    # Also find top-level runner spec for trajectory keyframes
    runner1_spec = None
    for h in humans_spec:
        if "run" in h.get("name", ""):
            runner1_spec = h
            break
    
    # Use runner's animation_binding scale (0.53) for consistent sizing across all humans
    runner_scale = runner1_binding.get("scale_xyz", [0.53, 0.53, 0.53])
    runner_root_offset = runner1_binding.get("root_offset_m", [0, 0, 0.53])
    
    # === Scale Dancer to match runner height ===
    dancer_prim = stage.GetPrimAtPath("/World/Humans/obj_2_dance_anim_2")
    if dancer_prim and dancer_prim.IsValid():
        d_xf = UsdGeom.Xformable(dancer_prim)
        try: d_xf.ClearXformOpOrder()
        except: pass
        # Read dancer's original position from spec so we can re-apply after clearing xform
        dancer_spec = None
        for h in humans_spec:
            if "dance" in h.get("name", ""):
                dancer_spec = h
                break
        dancer_binding = {}
        for ah in active_humans:
            if "dance" in ah.get("name", ""):
                dancer_binding = ah.get("animation_binding", {})
                break
        d_pos = dancer_binding.get("placement_location_m", [2.34, 2.13, 1.18])
        d_rot = dancer_binding.get("rotation_deg_xyz", [0, 0, 132.7])
        d_root_off = dancer_binding.get("root_offset_m", [0, 0, 1.04])
        d_orig_scale = dancer_binding.get("scale_xyz", [1.0, 1.0, 1.0])
        d_trans = d_xf.AddTranslateOp()
        d_orient = d_xf.AddOrientOp()
        d_scale = d_xf.AddScaleOp()
        # Use bbox-calibrated ground contact Z (empirically measured)
        dancer_z = DANCER_MESH_GROUND_Z
        d_trans.Set(Gf.Vec3d(d_pos[0], d_pos[1], dancer_z))
        d_yaw_rad = math.radians(d_rot[2])
        d_orient.Set(Gf.Quatf(math.cos(d_yaw_rad/2), 0, 0, math.sin(d_yaw_rad/2)))
        d_scale.Set(Gf.Vec3d(runner_scale[0], runner_scale[1], runner_scale[2]))
        with open(out_log, "a") as f: f.write(f"[NAV] Dancer: scale={runner_scale}, Z={dancer_z:.4f} (bbox-calibrated)\n")
    
    # === Setup book_return task: relocate book to floor ===
    book_prim_for_task = None
    if IS_MULTI_STEP and _cfg.get("book_prim"):
        book_path = _cfg["book_prim"]
        book_prim_for_task = stage.GetPrimAtPath(book_path)
        if book_prim_for_task and book_prim_for_task.IsValid():
            bk_xf = UsdGeom.Xformable(book_prim_for_task)
            try: bk_xf.ClearXformOpOrder()
            except: pass
            bk_trans = bk_xf.AddTranslateOp()
            bk_pos = _cfg["book_floor_pos"]
            bk_trans.Set(Gf.Vec3d(bk_pos[0], bk_pos[1], bk_pos[2]))
            with open(out_log, "a") as f: f.write(f"[NAV] Book relocated to floor: {bk_pos}\n")
        else:
            with open(out_log, "a") as f: f.write(f"[NAV] WARNING: Book prim not found: {book_path}\n")
    
    # === Setup Runner 1 (obstacle) — override scale + animate manually ===
    runner1_prim = None
    runner1_xf_ops = {}  # will hold translate/orient/scale ops
    prim_name = "obj_1_run_anim_1"
    if runner1_human_spec:
        pn = runner1_human_spec.get('target_human_name', prim_name)
        prim_name = pn.replace('__', '_')
    
    runner1_path = f"/World/Humans/{prim_name}"
    runner1_prim = stage.GetPrimAtPath(runner1_path)
    if not runner1_prim.IsValid():
        runner1_path = "/World/Humans/obj_1_run_anim_1"
        runner1_prim = stage.GetPrimAtPath(runner1_path)
    
    if runner1_prim and runner1_prim.IsValid():
        r1_xf = UsdGeom.Xformable(runner1_prim)
        try: r1_xf.ClearXformOpOrder()
        except: pass
        r1_trans = r1_xf.AddTranslateOp()
        r1_orient = r1_xf.AddOrientOp()
        r1_scale = r1_xf.AddScaleOp()
        r1_scale.Set(Gf.Vec3d(runner_scale[0], runner_scale[1], runner_scale[2]))
        runner1_xf_ops = {"trans": r1_trans, "orient": r1_orient, "scale": r1_scale}
        with open(out_log, "a") as f: f.write(f"[NAV] Runner1 scale set to {runner_scale}, root_offset={runner_root_offset}\n")
    
    # === Setup Agent (Runner 2) ===
    agent_xf = UsdGeom.Xformable(agent_prim)
    try: agent_xf.ClearXformOpOrder()
    except: pass
    agent_trans = agent_xf.AddTranslateOp()
    agent_orient = agent_xf.AddOrientOp()
    agent_scale = agent_xf.AddScaleOp()
    
    # Apply the same scale as runner (both use same human_usd mesh)
    agent_scale.Set(Gf.Vec3d(runner_scale[0], runner_scale[1], runner_scale[2]))
    
    # Find camera prim for per-step repositioning
    nav_cam_prim = None
    for p in stage.Traverse():
        if "NavCamera" in str(p.GetPath()):
            nav_cam_prim = p
            break
    
    timeline = omni.timeline.get_timeline_interface()
    timeline.play()
    
    # Compute animation loop range from the USD stage time codes
    anim_start_tc = stage.GetStartTimeCode()   # typically 1.0
    anim_end_tc   = stage.GetEndTimeCode()      # typically 39.0
    anim_duration_s = (anim_end_tc - anim_start_tc) / max(1.0, anim_fps)
    
    with open(out_log, "a") as f: f.write("[NAV] Setup complete. Starting navigation loop...\n")
    
    # === Agent state ===
    agent_x = AGENT_START_X
    agent_y = AGENT_START_Y
    agent_yaw = AGENT_START_YAW  # degrees
    sim_time = 0.0
    collision_occurred = False
    
    # === Navigation log ===
    nav_history = []
    collision_occurred = False
    
    # === Multi-step task state ===
    task_phases = _cfg.get("phases", None)
    current_phase_idx = 0
    agent_inventory = []  # items the agent is carrying
    if task_phases:
        active_target = task_phases[0]["target"]
        active_radius = task_phases[0]["radius"]
        with open(out_log, "a") as f:
            f.write(f"[NAV] Multi-step task: {len(task_phases)} phases\n")
            for i, ph in enumerate(task_phases):
                f.write(f"[NAV]   Phase {i}: {ph['name']} -> {ph['desc']} (r={ph['radius']}m, action={ph['action']})\n")
    else:
        active_target = TARGET
        active_radius = SUCCESS_RADIUS
    
    for step in range(MAX_STEPS):
        # 1. Update agent position in scene (bbox-calibrated ground contact Z)
        agent_trans.Set(Gf.Vec3d(agent_x, agent_y, RUNNER_MESH_GROUND_Z))
        # Apply mesh yaw offset so the body faces the movement direction
        mesh_yaw = agent_yaw + AGENT_MESH_YAW_OFFSET
        mesh_yaw_rad = math.radians(mesh_yaw)
        agent_orient.Set(Gf.Quatf(math.cos(mesh_yaw_rad/2), 0, 0, math.sin(mesh_yaw_rad/2)))
        
        # 2. Update camera to agent's eye position + facing direction
        if nav_cam_prim and nav_cam_prim.IsValid():
            cam_xf = UsdGeom.Xformable(nav_cam_prim)
            try: cam_xf.ClearXformOpOrder()
            except: pass
            cam_trans = cam_xf.AddTranslateOp()
            cam_orient = cam_xf.AddOrientOp()
            
            cam_trans.Set(Gf.Vec3d(agent_x, agent_y, AGENT_EYE_HEIGHT))
            cam_orient.Set(get_camera_quat_from_yaw(agent_yaw, CAMERA_PITCH_DEG))
        
        # 3. Animate Runner 1 (obstacle) — position from trajectory keyframes
        if runner1_spec and runner1_xf_ops:
            r1_pos, r1_rot = sample_human_motion(runner1_spec, sim_time, anim_fps)
            r1_z = runner_root_offset[2] if runner_root_offset else 0.0
            runner1_xf_ops["trans"].Set(Gf.Vec3d(r1_pos[0], r1_pos[1], r1_z))
            r1_yaw_rad = math.radians(r1_rot[2])
            runner1_xf_ops["orient"].Set(Gf.Quatf(math.cos(r1_yaw_rad/2), 0, 0, math.sin(r1_yaw_rad/2)))
        
        # 4. Advance timeline for skeleton animation (loop within animation range)
        anim_time = anim_start_tc / anim_fps + (sim_time % anim_duration_s) if anim_duration_s > 0 else sim_time
        timeline.set_current_time(anim_time)
        
        # 5. Render current frame (PathTracing with 16 accumulated subframes to clear snowflakes)
        rep.orchestrator.step(rt_subframes=16)
        
        # 6. Find the latest rendered frame
        import time
        wait_start = time.time()
        while time.time() - wait_start < 5.0:
            all_frames = sorted(glob.glob(os.path.join(out_dir_fpv, "rgb_*.png")))
            if len(all_frames) >= step + 1:
                frame_path = all_frames[-1]
                
                # Create a lightweight thumbnail for quick VS Code preview
                try:
                    from PIL import Image
                    for d in [out_dir_fpv, out_dir_bird]:
                        dir_frames = sorted(glob.glob(os.path.join(d, "rgb_*.png")))
                        if len(dir_frames) > 0:
                            fpath = dir_frames[-1]
                            thumb_path = fpath.replace(".png", "_thumb.jpg")
                            with Image.open(fpath) as img:
                                if img.mode in ('RGBA', 'P'):
                                    img = img.convert('RGB')
                                img.thumbnail((480, 270))
                                img.save(thumb_path, format="JPEG", quality=80)
                except Exception as e:
                    with open(out_log, "a") as f: f.write(f"[NAV] Thumbnail error: {e}\n")
                
                break
            time.sleep(0.1)
        else:
            with open(out_log, "a") as f: f.write(f"[NAV] ERROR: Timeout waiting for frame {step}\n")
            break
        
        # 7. Query VLM
        dist_to_target = math.sqrt((agent_x - active_target[0])**2 + (agent_y - active_target[1])**2)
        phase_info = ""
        if task_phases:
            ph = task_phases[current_phase_idx]
            phase_info = f" [Phase {current_phase_idx+1}/{len(task_phases)}: {ph['name']}]"
        with open(out_log, "a") as f: 
            f.write(f"[NAV] Step {step}: pos=({agent_x:.2f},{agent_y:.2f}) yaw={agent_yaw:.0f}° dist={dist_to_target:.2f}m{phase_info}\n")
        
        # Check frame quality (pixel analysis for black/overexposed frames)
        fq = check_frame_quality(frame_path)
        if fq.get('is_dark') or fq.get('is_overexposed'):
            fq_label = 'DARK' if fq.get('is_dark') else 'OVEREXPOSED'
            with open(out_log, "a") as f:
                f.write(f"[NAV] Step {step}: Frame quality: {fq_label} (mean={fq['mean_brightness']:.1f})\n")
        
        past_actions = [h["action"] for h in nav_history] if nav_history else None
        # Build task phase info for multi-step prompt
        _tpi = None
        if task_phases:
            ph = task_phases[current_phase_idx]
            inv_str = ', '.join(agent_inventory) if agent_inventory else 'empty'
            _tpi = {"desc": ph["desc"], "action": ph["action"], "inventory": inv_str,
                    "phase_idx": current_phase_idx, "phase_total": len(task_phases)}
        action, is_fallback = query_vlm(frame_path, out_log, collision_alert=collision_occurred, action_history=past_actions, step=step, frame_quality=fq, nav_history_records=nav_history, task_phase_info=_tpi)
        collision_occurred = False # reset after consuming
        
        # --- Multi-step: inject phase context into prompt (via nav_history) ---
        if task_phases and nav_history:
            ph = task_phases[current_phase_idx]
            inv_str = ', '.join(agent_inventory) if agent_inventory else 'empty'
            nav_history[-1]["phase"] = current_phase_idx
            nav_history[-1]["phase_name"] = ph["name"]
            nav_history[-1]["inventory"] = inv_str
        
        # --- STOP confirmation (Sequential Confirmation, 2 rounds) ---
        if action == "STOP":
            for confirm_round in range(1, STOP_CONFIRM_ROUNDS):
                with open(out_log, "a") as f:
                    f.write(f"[NAV] Step {step}: STOP requested, confirming ({confirm_round}/{STOP_CONFIRM_ROUNDS-1})...\n")
                confirm_action = query_vlm_confirm_stop(frame_path, out_log, step=step)
                if confirm_action != "STOP":
                    with open(out_log, "a") as f:
                        f.write(f"[NAV] Step {step}: STOP overridden → {confirm_action}\n")
                    action = confirm_action
                    break
                with open(out_log, "a") as f:
                    f.write(f"[NAV] Step {step}: STOP confirmed\n")
        
        log_action = f"{action} (fallback)" if is_fallback else action
        with open(out_log, "a") as f: f.write(f"[NAV] Step {step}: VLM action = {log_action}\n")
        
        
        # Save pre-action position to compute 'moved' later
        pre_x, pre_y = agent_x, agent_y
        
        nav_history.append({
            "step": step,
            "x": round(agent_x, 3),
            "y": round(agent_y, 3),
            "yaw": round(agent_yaw, 1),
            "dist_to_target": round(dist_to_target, 3),
            "action": action,
            "moved": False,  # will be updated after action execution
        })
        
        # 8. Check STOP / PICK_UP / PUT_DOWN
        if action == "STOP":
            if task_phases:
                # Multi-step: STOP only valid in final phase
                ph = task_phases[current_phase_idx]
                if ph["action"] == "STOP" and dist_to_target < active_radius:
                    with open(out_log, "a") as f:
                        f.write(f"[NAV] STOP at step {step}. Phase {current_phase_idx+1}/{len(task_phases)} complete. dist={dist_to_target:.2f}m. ALL PHASES DONE — SUCCESS!\n")
                    break
                else:
                    with open(out_log, "a") as f:
                        f.write(f"[NAV] Step {step}: STOP rejected — task not complete (phase={current_phase_idx+1}/{len(task_phases)}, dist={dist_to_target:.2f}m)\n")
                    # Don't break — treat as no-op, agent continues
            else:
                success = dist_to_target < active_radius
                with open(out_log, "a") as f:
                    f.write(f"[NAV] STOP at step {step}. dist={dist_to_target:.2f}m. {'SUCCESS!' if success else 'FAIL (too far)'}\n")
                break
        
        elif action == "PICK_UP" and task_phases:
            ph = task_phases[current_phase_idx]
            if ph["action"] == "PICK_UP" and dist_to_target < active_radius:
                # Success: pick up the book
                agent_inventory.append("book")
                if book_prim_for_task and book_prim_for_task.IsValid():
                    UsdGeom.Imageable(book_prim_for_task).MakeInvisible()
                current_phase_idx += 1
                active_target = task_phases[current_phase_idx]["target"]
                active_radius = task_phases[current_phase_idx]["radius"]
                with open(out_log, "a") as f:
                    f.write(f"[NAV] Step {step}: PICK_UP success! dist={dist_to_target:.2f}m. Inventory={agent_inventory}. Advancing to phase {current_phase_idx+1}: {task_phases[current_phase_idx]['name']}\n")
            else:
                reason = f"wrong phase (need {ph['action']})" if ph["action"] != "PICK_UP" else f"too far ({dist_to_target:.2f}m > {active_radius}m)"
                with open(out_log, "a") as f:
                    f.write(f"[NAV] Step {step}: PICK_UP failed — {reason}\n")
        
        elif action == "PUT_DOWN" and task_phases:
            ph = task_phases[current_phase_idx]
            if ph["action"] == "PUT_DOWN" and dist_to_target < active_radius and "book" in agent_inventory:
                # Success: put the book on the shelf
                agent_inventory.remove("book")
                if book_prim_for_task and book_prim_for_task.IsValid():
                    # Move book to shelf position and make visible
                    shelf_target = task_phases[current_phase_idx]["target"]
                    bk_xf = UsdGeom.Xformable(book_prim_for_task)
                    for op in bk_xf.GetOrderedXformOps():
                        if op.GetOpName() == "xformOp:translate":
                            op.Set(Gf.Vec3d(shelf_target[0], shelf_target[1], 0.8))
                    UsdGeom.Imageable(book_prim_for_task).MakeVisible()
                current_phase_idx += 1
                active_target = task_phases[current_phase_idx]["target"]
                active_radius = task_phases[current_phase_idx]["radius"]
                with open(out_log, "a") as f:
                    f.write(f"[NAV] Step {step}: PUT_DOWN success! dist={dist_to_target:.2f}m. Book placed on shelf. Advancing to phase {current_phase_idx+1}: {task_phases[current_phase_idx]['name']}\n")
            else:
                if "book" not in agent_inventory:
                    reason = "nothing to put down"
                elif ph["action"] != "PUT_DOWN":
                    reason = f"wrong phase (need {ph['action']})"
                else:
                    reason = f"too far ({dist_to_target:.2f}m > {active_radius}m)"
                with open(out_log, "a") as f:
                    f.write(f"[NAV] Step {step}: PUT_DOWN failed — {reason}\n")
        
        # 9. Apply movement action
        if action == "MOVE_FORWARD":
            import omni.physx, carb
            # Sync PhysX broadphase with latest xform changes
            simulation_app.update()
            query = omni.physx.get_physx_scene_query_interface()
            dir_x = math.cos(math.radians(agent_yaw))
            dir_y = math.sin(math.radians(agent_yaw))
            
            # Multi-height sphere sweep: waist (0.5m) + chest (1.0m)
            blocked = False
            hit_info = ""
            for sweep_z in [0.5, 1.0]:
                origin = carb.Float3(agent_x, agent_y, sweep_z)
                direction = carb.Float3(dir_x, dir_y, 0.0)
                hit = query.sweep_sphere_closest(0.2, origin, direction, STEP_DISTANCE)
                if hit["hit"]:
                    blocked = True
                    hit_body = hit.get("rigidBody", "?")
                    hit_collider = hit.get("collider", "?")
                    hit_dist = hit.get("distance", -1)
                    hit_info = f"z={sweep_z} body={hit_body} collider={hit_collider} dist={hit_dist:.3f}"
                    break
            
            if not blocked:
                agent_x += STEP_DISTANCE * dir_x
                agent_y += STEP_DISTANCE * dir_y
            else:
                with open(out_log, "a") as f: f.write(f"[NAV] Step {step}: COLLISION [{hit_info}]\n")
                collision_occurred = True
        elif action == "TURN_LEFT":
            agent_yaw += TURN_ANGLE
        elif action == "TURN_RIGHT":
            agent_yaw -= TURN_ANGLE
        
        # Keep yaw in [-180, 180]
        agent_yaw = wrap_angle_deg(agent_yaw)
        
        # Update 'moved' and 'blocked' fields in nav_history
        did_move = (abs(agent_x - pre_x) > 0.001 or abs(agent_y - pre_y) > 0.001)
        nav_history[-1]["moved"] = did_move
        nav_history[-1]["blocked"] = (action == "MOVE_FORWARD" and not did_move)
        
        # 10. Advance simulation time (runner keeps moving)
        sim_time += RUNNER_TIME_PER_STEP
    
    else:
        with open(out_log, "a") as f:
            f.write(f"[NAV] TIMEOUT after {MAX_STEPS} steps. dist={dist_to_target:.2f}m\n")
    
    # === Save navigation log ===
    log_path = os.path.join(RUN_DIR, "vlm_nav_history.json")
    with open(log_path, "w") as f:
        json.dump({
            "target_name": TARGET_NAME,
            "target_desc": TARGET_DESC,
            "target": TARGET,
            "start": [AGENT_START_X, AGENT_START_Y, AGENT_START_YAW],
            "success_radius": SUCCESS_RADIUS,
            "history": nav_history,
        }, f, indent=2)
    with open(out_log, "a") as f: f.write(f"[NAV] History saved: {log_path}\n")
    
    # === Generate media (HD + Preview) via FFmpeg ===
    import subprocess, shutil
    gen_media_script = "/home/qi/hc/Puppeteer/zehao_task/gen_media.sh"
    with open(out_log, "a") as f: f.write("[NAV] Running gen_media.sh for HD + preview media...\n")
    result = subprocess.run(["bash", gen_media_script, RUN_DIR], capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        with open(out_log, "a") as f: f.write("[NAV] Media generation complete.\n")
    else:
        with open(out_log, "a") as f:
            f.write(f"[NAV] Media generation failed (rc={result.returncode}): {(result.stderr or '')[:200]}\n")
    
    # === Generate 2D Trajectory Map ===
    with open(out_log, "a") as f: f.write("[NAV] Generating 2D trajectory map...\n")
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        
        fig, ax = plt.subplots(1, 1, figsize=(12, 8))
        ax.set_facecolor('#1a1a2e')
        fig.patch.set_facecolor('#0f0f23')
        
        # Extract trajectory
        xs = [h["x"] for h in nav_history]
        ys = [h["y"] for h in nav_history]
        actions = [h["action"] for h in nav_history]
        
        # Color-code by action
        action_colors = {
            "MOVE_FORWARD": "#00ff88",
            "TURN_LEFT": "#ff6b6b",
            "TURN_RIGHT": "#4ecdc4",
            "STOP": "#ffd93d",
        }
        
        # Draw trajectory segments
        for i in range(len(xs) - 1):
            color = action_colors.get(actions[i], "#888888")
            ax.plot([xs[i], xs[i+1]], [ys[i], ys[i+1]], color=color, linewidth=2, alpha=0.8)
            # Draw action dots
            ax.scatter(xs[i], ys[i], c=color, s=30, zorder=5, alpha=0.9)
        
        # Mark start and target
        ax.scatter(AGENT_START_X, AGENT_START_Y, c='#ff4444', s=200, marker='*', zorder=10, label='Start', edgecolors='white', linewidths=1)
        ax.scatter(TARGET[0], TARGET[1], c='#44ff44', s=200, marker='s', zorder=10, label=f'Target ({TARGET_NAME.title()})', edgecolors='white', linewidths=1)
        
        # Draw target bounding box outline
        target_bbox = _cfg.get("bbox")
        if target_bbox:
            bx0, by0, bx1, by1 = target_bbox
            rect = mpatches.Rectangle((bx0, by0), bx1 - bx0, by1 - by0,
                                       linewidth=2, edgecolor='#44ff44', facecolor='#44ff44',
                                       alpha=0.18, zorder=3, linestyle='--',
                                       label=f'{TARGET_NAME.title()} bbox')
            ax.add_patch(rect)
        
        # Draw success radius circle
        success_circle = mpatches.Circle((TARGET[0], TARGET[1]), SUCCESS_RADIUS,
                                          linewidth=1.5, edgecolor='#44ff44', facecolor='none',
                                          alpha=0.5, zorder=3, linestyle=':',
                                          label=f'Success r={SUCCESS_RADIUS}m')
        ax.add_patch(success_circle)
        
        # Mark final position
        if xs:
            ax.scatter(xs[-1], ys[-1], c='#ffaa00', s=150, marker='D', zorder=10, label=f'End (d={nav_history[-1]["dist_to_target"]:.1f}m)', edgecolors='white', linewidths=1)
        
        # Draw facing direction arrows at key steps
        for i in range(0, len(nav_history), max(1, len(nav_history)//15)):
            h = nav_history[i]
            yaw_r = math.radians(h["yaw"])
            dx = 0.4 * math.cos(yaw_r)
            dy = 0.4 * math.sin(yaw_r)
            ax.annotate('', xy=(h["x"]+dx, h["y"]+dy), xytext=(h["x"], h["y"]),
                       arrowprops=dict(arrowstyle='->', color='white', lw=1.5))
            ax.text(h["x"]+dx*1.3, h["y"]+dy*1.3, str(h["step"]), fontsize=7, color='white', ha='center', va='center')
        
        # Legend
        legend_patches = [mpatches.Patch(color=c, label=a) for a, c in action_colors.items()]
        ax.legend(handles=legend_patches + ax.get_legend_handles_labels()[0], loc='upper right',
                 fontsize=9, facecolor='#2a2a4a', edgecolor='#444', labelcolor='white')
        
        ax.set_xlabel('X (meters)', color='white', fontsize=12)
        ax.set_ylabel('Y (meters)', color='white', fontsize=12)
        ax.set_title('VLM Navigation Trajectory (Top-Down View)', color='white', fontsize=14, fontweight='bold')
        ax.tick_params(colors='white')
        ax.set_aspect('equal')
        ax.grid(True, alpha=0.2, color='white')
        for spine in ax.spines.values():
            spine.set_color('#444')
        
        traj_path = os.path.join(RUN_DIR, "trajectory_2d.png")
        plt.savefig(traj_path, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
        plt.close()
        with open(out_log, "a") as f: f.write(f"[NAV] 2D trajectory map saved: {traj_path}\n")
    except Exception as e:
        with open(out_log, "a") as f: f.write(f"[NAV] Failed to generate trajectory map: {e}\n")
    
    with open(out_log, "a") as f: f.write("[NAV] All done!\n")
    simulation_app.close()

except Exception as e:
    with open(out_log, "a") as f:
        f.write("\n[NAV] ERROR:\n")
        f.write(traceback.format_exc())
