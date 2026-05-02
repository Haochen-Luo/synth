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

SYSTEM_PROMPT = """You are a navigation robot inside a living room. Your goal is to reach the SOFA.

You see the room from your first-person camera. There may be a person running across the room - you must avoid colliding with them.

You can ONLY output ONE of these actions:
- MOVE_FORWARD (move 0.25 meters in your current facing direction)
- TURN_LEFT (rotate 30 degrees to the left)  
- TURN_RIGHT (rotate 30 degrees to the right)
- STOP (you believe you have reached the sofa)

Rules:
- If you see the sofa directly ahead and close, move toward it.
- If the sofa is to your left or right, turn toward it first.
- If a person is blocking your path, wait (output STOP temporarily) or turn to find an alternate route.
- When you are very close to the sofa (within arm's reach), output STOP.

Output ONLY the action name, nothing else. Do not explain. Example output: MOVE_FORWARD"""

def query_vlm(image_path: str, out_log: str, collision_alert: bool = False, action_history: list = None) -> str:
    """Send image to VLM, return action string."""
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")
    
    prompt = "You are navigating an indoor environment to reach the SOFA. Based on this first-person view, what action should you take? Output only: MOVE_FORWARD, TURN_LEFT, TURN_RIGHT, or STOP."
    if action_history:
        recent = action_history[-5:]  # last 5 actions
        prompt += f" Your recent actions were: {', '.join(recent)}. Avoid repeating unhelpful patterns."
    if collision_alert:
        prompt += " WARNING: Your path is currently blocked by an obstacle. You MUST turn to find a clear path."

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                {"type": "text", "text": prompt}
            ]}
        ],
        "max_tokens": 20,
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
            text = result["choices"][0]["message"]["content"].strip().upper()
            # Extract valid action from response
            for action in ["MOVE_FORWARD", "TURN_LEFT", "TURN_RIGHT", "STOP"]:
                if action in text:
                    return action
            with open(out_log, "a") as f: f.write(f"[VLM] Unrecognized response: {text[:100]}\n")
            return "MOVE_FORWARD"  # Default fallback
    except Exception as e:
        with open(out_log, "a") as f: f.write(f"[VLM] API error: {e}\n")
        return "MOVE_FORWARD"  # Fallback on error

# ============================================================
# Navigation config
# ============================================================
STEP_DISTANCE = 0.25     # meters per MOVE_FORWARD
TURN_ANGLE = 15.0        # degrees per TURN
MAX_STEPS = 250        # timeout
SUCCESS_RADIUS = 0.8     # meters to target for success
AGENT_HEIGHT = 0.0       # Fix: match dancer's floor-level physical height
AGENT_EYE_HEIGHT = 1.58  # z for camera (eye level)
RUNNER_TIME_PER_STEP = 0.5  # seconds of runner animation per nav step

# Target: center of big sofa
TARGET = [4.38, 6.44]
# Agent start: on the rug, facing roughly toward sofa
AGENT_START_X = 12.0
AGENT_START_Y = 4.0
AGENT_START_YAW = 160.0  # degrees, roughly facing the sofa (upper-left)

def get_camera_quat_from_yaw(yaw_deg):
    """Generates a camera orientation looking in the yaw direction, keeping the horizon level."""
    from pxr import Gf
    import math
    yaw_rad = math.radians(yaw_deg)
    
    eye = Gf.Vec3d(0.0, 0.0, 0.0)
    target = Gf.Vec3d(math.cos(yaw_rad), math.sin(yaw_rad), 0.0)
    up = Gf.Vec3d(0.0, 0.0, 1.0)
    
    mat = Gf.Matrix4d().SetLookAt(eye, target, up)
    qd = mat.GetInverse().ExtractRotation().GetQuat()
    return Gf.Quatf(qd.GetReal(), *qd.GetImaginary())

# ============================================================
# Main
# ============================================================
out_log = "/home/qi/hc/Puppeteer/zehao_task/vlm_nav.log"
with open(out_log, "w") as f: f.write("[NAV] Starting VLM navigation benchmark...\n")

try:
    from isaacsim import SimulationApp
    with open(out_log, "a") as f: f.write("[NAV] Loading SimulationApp...\n")
    simulation_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})
    
    import omni.usd
    import omni.replicator.core as rep
    from omni.isaac.core.utils.stage import open_stage, is_stage_loading
    from pxr import Gf, UsdGeom
    
    scene_dir = "/home/qi/hc/Puppeteer/zehao_new_folder/phy_env/case11_multi_surface_turn_right_full_physics_scene"
    usd_path = os.path.join(scene_dir, "compiled_stages", "case11_multi_surface_turn_right_full_physics.compiled.usda")
    spec_path = os.path.join(scene_dir, "compiled_specs", "case11_multi_surface_turn_right_full_physics.compiled.spec.json")
    human_usd = os.path.join(scene_dir, "assets", "humans", "obj_1_run_anim_1.usdc")
    
    spec = json.load(open(spec_path))
    humans_spec = spec.get("humans", [])
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
    
    # Instance agent runner
    agent_prim = stage.DefinePrim("/World/Humans/agent_runner")
    agent_prim.GetReferences().AddReference(human_usd)
    simulation_app.update()
    
    # Lighting
    for lp in [(5,6,2.3),(10,6,2.3),(15,6,2.3),(5,2,2.3),(10,10,2.3)]:
        rep.create.light(light_type="sphere", position=lp, intensity=50000.0)
    
    # Warm up
    for _ in range(100):
        simulation_app.update()
        
    import omni.kit.commands
    omni.kit.commands.execute("ChangeSetting", path="/rtx/rendermode", value="PathTracing")
    omni.kit.commands.execute("ChangeSetting", path="/rtx/pathtracing/spp", value=16)
    
    out_dir_fpv = "/home/qi/hc/Puppeteer/zehao_task/vlm_nav_frames_fpv"
    out_dir_bird = "/home/qi/hc/Puppeteer/zehao_task/vlm_nav_frames_bird"
    out_dir_bird2 = "/home/qi/hc/Puppeteer/zehao_task/vlm_nav_frames_bird2"
    
    for d in [out_dir_fpv, out_dir_bird, out_dir_bird2]:
        import shutil
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d, exist_ok=True)
    
    # === Setup FPV camera (VLM input) ===
    # Use pure USD Camera instead of rep.create.camera to prevent Replicator OmniGraph
    # from overriding our manual per-step pose updates.
    nav_cam_path = "/World/NavCamera"
    cam_fpv_prim = UsdGeom.Camera.Define(stage, nav_cam_path)
    rp_fpv = rep.create.render_product(str(nav_cam_path), (1920, 1080))
    writer_fpv = rep.WriterRegistry.get("BasicWriter")
    writer_fpv.initialize(output_dir=out_dir_fpv, rgb=True)
    writer_fpv.attach([rp_fpv])

    # === Third-Person Camera (rear corner — validated by grid search) ===
    # CRITICAL: Must use rep.modify.pose inside 'with cam:' block.
    # rep.create.camera(look_at=...) silently fails to orient the camera.
    cam_bird = rep.create.camera(position=(13.0, 7.0, 2.7), name="BirdEyeCamera")
    with cam_bird:
        rep.modify.pose(position=(13.0, 7.0, 2.7), look_at=(6.0, 5.0, 0.5))
    rp_bird = rep.create.render_product(cam_bird, (1920, 1080))
    writer_bird = rep.WriterRegistry.get("BasicWriter")
    writer_bird.initialize(output_dir=out_dir_bird, rgb=True)
    writer_bird.attach([rp_bird])

    # === Second Bird Camera (sofa-side corner — opposite angle) ===
    cam_bird2 = rep.create.camera(position=(2.0, 7.0, 2.7), name="BirdEyeCamera2")
    with cam_bird2:
        rep.modify.pose(position=(2.0, 7.0, 2.7), look_at=(8.0, 4.0, 0.5))
    rp_bird2 = rep.create.render_product(cam_bird2, (1920, 1080))
    writer_bird2 = rep.WriterRegistry.get("BasicWriter")
    writer_bird2.initialize(output_dir=out_dir_bird2, rgb=True)
    writer_bird2.attach([rp_bird2])
    
    # === Setup Runner 1 (obstacle) ===
    runner1_spec = None
    for h in humans_spec:
        if "run" in h.get("name", ""):
            runner1_spec = h
            break
    runner1_xform = None
    if runner1_spec:
        name = runner1_spec["name"].replace(" ", "_")
        prim = stage.GetPrimAtPath(f"/World/Humans/{name}")
        if prim.IsValid():
            xf = UsdGeom.Xformable(prim)
            try: xf.ClearXformOpOrder()
            except: pass
            t = xf.AddTranslateOp()
            o = xf.AddOrientOp()
            s = xf.AddScaleOp()
            s.Set(Gf.Vec3d(0.01, 0.01, 0.01)) # FORCE 0.01 scale for human meshes
            runner1_xform = {"spec": runner1_spec, "trans_op": t, "orient_op": o}
    
    # === Setup Agent (Runner 2) ===
    agent_xf = UsdGeom.Xformable(agent_prim)
    try: agent_xf.ClearXformOpOrder()
    except: pass
    agent_trans = agent_xf.AddTranslateOp()
    agent_orient = agent_xf.AddOrientOp()
    agent_scale = agent_xf.AddScaleOp()
    agent_scale.Set(Gf.Vec3d(0.01, 0.01, 0.01)) # FORCE 0.01 scale for human meshes
    
    # Find camera prim for per-step repositioning
    nav_cam_prim = None
    for p in stage.Traverse():
        if "NavCamera" in str(p.GetPath()):
            nav_cam_prim = p
            break
    
    timeline = omni.timeline.get_timeline_interface()
    timeline.play()
    
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
    
    for step in range(MAX_STEPS):
        # 1. Update agent position in scene
        agent_trans.Set(Gf.Vec3d(agent_x, agent_y, AGENT_HEIGHT))
        yaw_rad = math.radians(agent_yaw)
        agent_orient.Set(Gf.Quatf(math.cos(yaw_rad/2), 0, 0, math.sin(yaw_rad/2)))
        
        # 2. Update camera to agent's eye position + facing direction
        # [FIX]: Native USD LookAt algorithm (Zero gimbal lock, zero replicator overhead)
        if nav_cam_prim and nav_cam_prim.IsValid():
            cam_xf = UsdGeom.Xformable(nav_cam_prim)
            try: cam_xf.ClearXformOpOrder()
            except: pass
            cam_trans = cam_xf.AddTranslateOp()
            cam_orient = cam_xf.AddOrientOp()
            
            cam_trans.Set(Gf.Vec3d(agent_x, agent_y, AGENT_EYE_HEIGHT))
            cam_orient.Set(get_camera_quat_from_yaw(agent_yaw))
        
        # 3. Update runner 1 position
        if runner1_xform:
            pos, rot_deg = sample_human_motion(runner1_xform["spec"], sim_time, anim_fps)
            offset = runner1_xform["spec"].get("visual_rotation_offset_deg_xyz", [0,0,0])
            final_rot = [rot_deg[i]+offset[i] for i in range(3)]
            runner1_xform["trans_op"].Set(Gf.Vec3d(pos[0], pos[1], pos[2]))
            r_yaw = math.radians(float(final_rot[2]))
            runner1_xform["orient_op"].Set(Gf.Quatf(math.cos(r_yaw/2), 0, 0, math.sin(r_yaw/2)))
        
        # 4. Advance timeline for skeleton animation
        timeline.set_current_time(sim_time)
        
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
                    thumb_path = frame_path.replace(".png", "_thumb.jpg")
                    with Image.open(frame_path) as img:
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
        dist_to_target = math.sqrt((agent_x - TARGET[0])**2 + (agent_y - TARGET[1])**2)
        with open(out_log, "a") as f: 
            f.write(f"[NAV] Step {step}: pos=({agent_x:.2f},{agent_y:.2f}) yaw={agent_yaw:.0f}° dist={dist_to_target:.2f}m\n")
        
        past_actions = [h["action"] for h in nav_history] if nav_history else None
        action = query_vlm(frame_path, out_log, collision_alert=collision_occurred, action_history=past_actions)
        collision_occurred = False # reset after consuming
        
        with open(out_log, "a") as f: f.write(f"[NAV] Step {step}: VLM action = {action}\n")
        
        # Anti-oscillation guard v4: track collision-blocked moves, not just position
        # --- Stuck detector: only fires after consecutive collision-blocked moves ---
        if len(nav_history) >= 3:
            # Count recent consecutive collision-blocked moves (action was MOVE but position didn't change)
            blocked_moves = 0
            for h in reversed(nav_history):
                if h["action"] == "MOVE_FORWARD" and blocked_moves == 0:
                    # Check if this move was blocked (same pos as current)
                    if abs(h["x"] - round(agent_x, 3)) < 0.01 and abs(h["y"] - round(agent_y, 3)) < 0.01:
                        blocked_moves += 1
                    else:
                        break
                elif h["action"] in ("TURN_LEFT", "TURN_RIGHT"):
                    continue  # skip turns — they don't indicate stuck
                elif h["action"] == "MOVE_FORWARD":
                    if abs(h["x"] - round(agent_x, 3)) < 0.01 and abs(h["y"] - round(agent_y, 3)) < 0.01:
                        blocked_moves += 1
                    else:
                        break
                else:
                    break
            
            if blocked_moves >= 3 and action == "MOVE_FORWARD":
                # Only override MOVE_FORWARD, let VLM-requested turns proceed naturally
                if blocked_moves >= 8:
                    agent_yaw = wrap_angle_deg(agent_yaw + 180)
                    with open(out_log, "a") as f: f.write(f"[NAV] Step {step}: STUCK ({blocked_moves} blocked moves)! 180° about-face\n")
                elif blocked_moves % 2 == 1:
                    agent_yaw += 60
                    action = "TURN_LEFT"
                    with open(out_log, "a") as f: f.write(f"[NAV] Step {step}: STUCK ({blocked_moves} blocked moves)! Forcing 90° LEFT\n")
                else:
                    agent_yaw -= 60
                    action = "TURN_RIGHT"
                    with open(out_log, "a") as f: f.write(f"[NAV] Step {step}: STUCK ({blocked_moves} blocked moves)! Forcing 90° RIGHT\n")
        # --- Turn oscillation detector (independent) ---
        if action in ("TURN_LEFT", "TURN_RIGHT") and len(nav_history) >= 2:
            last_actions = [h["action"] for h in nav_history[-2:]]
            if (last_actions == ["TURN_LEFT", "TURN_RIGHT"] and action == "TURN_LEFT") or \
               (last_actions == ["TURN_RIGHT", "TURN_LEFT"] and action == "TURN_RIGHT"):
                with open(out_log, "a") as f: f.write(f"[NAV] Step {step}: Turn oscillation detected! Overriding to MOVE_FORWARD\n")
                action = "MOVE_FORWARD"
        
        nav_history.append({
            "step": step,
            "x": round(agent_x, 3),
            "y": round(agent_y, 3),
            "yaw": round(agent_yaw, 1),
            "dist_to_target": round(dist_to_target, 3),
            "action": action,
        })
        
        # 8. Check STOP
        if action == "STOP":
            success = dist_to_target < SUCCESS_RADIUS
            with open(out_log, "a") as f:
                f.write(f"[NAV] STOP at step {step}. dist={dist_to_target:.2f}m. {'SUCCESS!' if success else 'FAIL (too far)'}\n")
            break
        
        # 9. Apply action
        if action == "MOVE_FORWARD":
            import omni.physx, carb
            # Sync PhysX broadphase with latest xform changes
            simulation_app.update()
            query = omni.physx.get_physx_scene_query_interface()
            dir_x = math.cos(math.radians(agent_yaw))
            dir_y = math.sin(math.radians(agent_yaw))
            
            # Multi-height sphere sweep: waist (0.5m) + chest (1.0m)
            # Note: floor collision is at z≈0.1, sphere r=0.2, so z must be >= 0.4
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
        
        # 10. Advance simulation time (runner keeps moving)
        sim_time += RUNNER_TIME_PER_STEP
    
    else:
        with open(out_log, "a") as f:
            f.write(f"[NAV] TIMEOUT after {MAX_STEPS} steps. dist={dist_to_target:.2f}m\n")
    
    # === Save navigation log ===
    log_path = "/home/qi/hc/Puppeteer/zehao_task/vlm_nav_history.json"
    with open(log_path, "w") as f:
        json.dump({
            "target": TARGET,
            "start": [AGENT_START_X, AGENT_START_Y, AGENT_START_YAW],
            "success_radius": SUCCESS_RADIUS,
            "history": nav_history,
        }, f, indent=2)
    with open(out_log, "a") as f: f.write(f"[NAV] History saved: {log_path}\n")
    
    # === Generate videos via FFmpeg (HD + Lite preview) ===
    import subprocess, shutil
    base_dir = "/home/qi/hc/Puppeteer/zehao_task"
    preview_dir = os.path.join(base_dir, "nav_preview")
    os.makedirs(preview_dir, exist_ok=True)
    
    if shutil.which("ffmpeg"):
        with open(out_log, "a") as f: f.write("[NAV] Generating MP4/GIF videos via FFmpeg...\n")
        
        for label, src_dir in [("fpv", out_dir_fpv), ("bird_rear", out_dir_bird), ("bird_front", out_dir_bird2)]:
            png_files = sorted(glob.glob(os.path.join(src_dir, "rgb_*.png")))
            if not png_files:
                continue
            frame_pattern = os.path.join(src_dir, "rgb_%04d.png")
            n_frames = len(png_files)
            
            # --- HD MP4 (1920x1080, 5fps) ---
            mp4_hd = os.path.join(base_dir, f"vlm_nav_{label}_hd.mp4")
            subprocess.run(["ffmpeg", "-y", "-framerate", "5", "-i", frame_pattern, "-frames:v", str(n_frames),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "fast", mp4_hd], capture_output=True)
            with open(out_log, "a") as flog: flog.write(f"[NAV] {label} HD MP4: {mp4_hd} ({n_frames} frames)\n")
            
            # --- HD GIF (960x540, 5fps, max 100 frames) ---
            gif_hd = os.path.join(base_dir, f"vlm_nav_{label}_hd.gif")
            max_gif_hd = min(n_frames, 100)
            subprocess.run(["ffmpeg", "-y", "-framerate", "5", "-i", frame_pattern, "-frames:v", str(max_gif_hd),
                "-vf", "scale=960:540,fps=5,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse", gif_hd], capture_output=True)
            with open(out_log, "a") as flog: flog.write(f"[NAV] {label} HD GIF: {gif_hd} ({max_gif_hd} frames)\n")
            
            # --- Preview MP4 (480x270, 5fps — nav_preview/) ---
            mp4_lite = os.path.join(preview_dir, f"{label}_preview.mp4")
            subprocess.run(["ffmpeg", "-y", "-framerate", "5", "-i", frame_pattern, "-frames:v", str(n_frames),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-vf", "scale=480:270",
                "-crf", "28", "-preset", "fast", mp4_lite], capture_output=True)
            with open(out_log, "a") as flog: flog.write(f"[NAV] {label} Preview MP4: {mp4_lite}\n")
            
            # --- Preview GIF (320x180, 5fps, max 100 frames — nav_preview/) ---
            gif_lite = os.path.join(preview_dir, f"{label}_preview.gif")
            max_gif_lite = min(n_frames, 100)
            subprocess.run(["ffmpeg", "-y", "-framerate", "5", "-i", frame_pattern, "-frames:v", str(max_gif_lite),
                "-vf", "scale=320:180,fps=5,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse", gif_lite], capture_output=True)
            with open(out_log, "a") as flog: flog.write(f"[NAV] {label} Preview GIF: {gif_lite}\n")
    else:
        with open(out_log, "a") as f:
            f.write("[NAV] ffmpeg not found in container. Run post-processing from host:\n")
            f.write("[NAV]   ssh GPU-843 'source ~/miniconda3/etc/profile.d/conda.sh && conda activate pp && cd /home/qi/hc/Puppeteer/zehao_task && bash gen_videos.sh'\n")
    
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
        ax.scatter(TARGET[0], TARGET[1], c='#44ff44', s=200, marker='s', zorder=10, label='Target (Sofa)', edgecolors='white', linewidths=1)
        
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
        
        traj_path = os.path.join(preview_dir, "trajectory_2d.png")
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
