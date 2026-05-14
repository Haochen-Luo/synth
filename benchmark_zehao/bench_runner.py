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

# ── Config ──
VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8300/v1/chat/completions")
MODEL_NAME = os.environ.get("MODEL_NAME", "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8")
TASKS_JSON = os.path.join(SCRIPT_DIR, "benchmark_tasks.json")
RESULTS_BASE = os.path.join(SCRIPT_DIR, "results")

STEP_DIST = 0.25; TURN_ANG = 15.0; TILT_ANG = 5.0
PITCH_MIN = -30; PITCH_MAX = 10; PITCH_INIT = -10
EYE_H = 1.58; MESH_YAW_OFF = 90.0; RUNNER_TIME_PER_STEP = 0.5
STOP_CONFIRM = 2

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
    task["max_steps"] = bench.get("max_steps", 150)

tid = task["id"]; level = task["level"]
scene_dir = os.path.join(SCRIPT_DIR, task["scene_dir"])
phases = task["phases"]; max_steps = task.get("max_steps", 150)
is_multi = len(phases) > 1 or phases[0]["action"] != "STOP"
agent_start_xy = task["agent_start"]; agent_start_yaw = task["agent_yaw"]

# ── Run dir ──
ts = _dt.datetime.now().strftime('%Y%m%d_%H%M%S')
RUN_DIR = os.path.join(RESULTS_BASE, f"{tid}_{ts}")
os.makedirs(RUN_DIR, exist_ok=True)
LOG = os.path.join(RUN_DIR, "run.log")

def log(msg):
    with open(LOG, "a") as f: f.write(msg + "\n")
    print(msg)

log(f"[BENCH] Task={tid} Level={level} Scene={task['scene_dir']}")
log(f"[BENCH] Instruction: {task['instruction']}")
log(f"[BENCH] Phases: {len(phases)}, MaxSteps={max_steps}")

# ── VLM query ──
ALL_ACTIONS = ["MOVE_FORWARD","TURN_LEFT","TURN_RIGHT","STOP","PICK_UP","PUT_DOWN","TURN_ON","TILT_UP","TILT_DOWN"]
ACTION_RE = re.compile(r"ACTION:\s*(" + "|".join(ALL_ACTIONS) + r")", re.IGNORECASE)

def query_vlm(img_path, prompt, system_prompt, step=0):
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
        # Log raw response
        resp_log = os.path.join(RUN_DIR, "vlm_responses.jsonl")
        with open(resp_log, "a") as f:
            f.write(json.dumps({"step":step,"response":text}) + "\n")
        m = ACTION_RE.search(text.upper())
        if m: return m.group(1), False
        # Fallback: last mentioned action
        best, bi = "MOVE_FORWARD", -1
        for a in ALL_ACTIONS:
            i = text.upper().rfind(a)
            if i > bi: bi, best = i, a
        return best, True
    except Exception as e:
        log(f"[VLM] Error: {e}"); return "MOVE_FORWARD", True

# ── Main ──
try:
    from isaacsim import SimulationApp
    sim_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})
    import omni.usd, omni.replicator.core as rep
    from omni.isaac.core.utils.stage import open_stage, is_stage_loading
    from pxr import Gf, UsdGeom, UsdLux

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

    # ── Hide ceiling ──
    for p in stage.Traverse():
        if "ceiling" in str(p.GetPath()).lower():
            try: UsdGeom.Imageable(p).MakeInvisible()
            except: pass

    # ── Get runner scale from spec ──
    runner_scale = [0.53, 0.53, 0.53]
    runner_root_off = [0, 0, 0.53]
    runner1_spec = None
    for ah in active_humans:
        if "run" in ah.get("name",""):
            ab = ah.get("animation_binding",{})
            runner_scale = ab.get("scale_xyz", runner_scale)
            runner_root_off = ab.get("root_offset_m", runner_root_off)
            break
    for h in humans_spec:
        if "run" in h.get("name",""):
            runner1_spec = h; break

    GROUND_Z = runner_root_off[2] if runner_root_off else 0.53

    # ── Resolve target positions for each phase ──
    resolved_targets = []
    pickup_prim_path = None
    for ph in phases:
        tobj = ph["target_object"]
        if tobj.startswith("__human_"):
            # Use human initial position
            idx = int(tobj.replace("__human_","").replace("__",""))
            if idx < len(humans_spec):
                pos = humans_spec[idx].get("placement_location_m",[0,0,0])
                resolved_targets.append([pos[0], pos[1]])
            else:
                resolved_targets.append([0, 0])
            log(f"[BENCH] Phase '{ph['name']}' -> human[{idx}] at {resolved_targets[-1]}")
        elif tobj == "door":
            # Find door prim
            dp = find_prim_by_factory(stage, "door")
            if dp:
                c = get_prim_world_center(stage, dp)
                resolved_targets.append(c[:2] if c else [6, 11])
            else:
                resolved_targets.append([6, 11])
            log(f"[BENCH] Phase '{ph['name']}' -> door at {resolved_targets[-1]}")
        else:
            pp = find_prim_by_factory(stage, tobj)
            if pp:
                c = get_prim_world_center(stage, pp)
                if c:
                    resolved_targets.append(c[:2])
                    log(f"[BENCH] Phase '{ph['name']}' -> {tobj} prim={pp} center={c[:2]}")
                else:
                    resolved_targets.append([5, 5])
                    log(f"[BENCH] WARNING: no bbox for {pp}")
                # Track pickup prims
                if ph["action"] == "PICK_UP" and not pickup_prim_path:
                    pickup_prim_path = pp
            else:
                resolved_targets.append([5, 5])
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
                    xf.AddTranslateOp().Set(Gf.Vec3d(pa[0], pa[1], pa[2]))
                    resolved_targets[i] = [pa[0], pa[1]]
                    if ph["action"] == "PICK_UP":
                        pickup_prim = prim; pickup_prim_path = pp
                    log(f"[BENCH] Placed {tobj} at {pa}")

    # ── Instance agent ──
    human_usd = sf["human_usds"][0] if sf["human_usds"] else None
    agent_prim = stage.DefinePrim("/World/Humans/agent_runner")
    if human_usd:
        agent_prim.GetReferences().AddReference(human_usd)
    sim_app.update()

    # ── Lighting ──
    for i, lp in enumerate([(5,6,2.3),(10,6,2.3),(15,6,2.3),(5,2,2.3),(10,10,2.3)]):
        lt = UsdLux.SphereLight.Define(stage, f"/World/Lights/BenchLight_{i}")
        lt.CreateIntensityAttr().Set(80000.0); lt.CreateRadiusAttr().Set(0.3)
        xf = UsdGeom.Xformable(lt); xf.ClearXformOpOrder()
        xf.AddTranslateOp().Set(Gf.Vec3d(*lp))

    # ── Warm up + PathTracing ──
    for _ in range(100): sim_app.update()
    import omni.kit.commands
    omni.kit.commands.execute("ChangeSetting", path="/rtx/rendermode", value="PathTracing")
    omni.kit.commands.execute("ChangeSetting", path="/rtx/pathtracing/spp", value=16)

    # ── Cameras ──
    def cam_quat(yaw_deg, pitch_deg=0.0):
        yr, pr = math.radians(yaw_deg), math.radians(pitch_deg)
        eye = Gf.Vec3d(0,0,0)
        tgt = Gf.Vec3d(math.cos(yr)*math.cos(pr), math.sin(yr)*math.cos(pr), math.sin(pr))
        mat = Gf.Matrix4d().SetLookAt(eye, tgt, Gf.Vec3d(0,0,1))
        qd = mat.GetInverse().ExtractRotation().GetQuat()
        return Gf.Quatf(qd.GetReal(), *qd.GetImaginary())

    fpv_cam = UsdGeom.Camera.Define(stage, "/World/NavCam")
    fpv_cam.CreateFocalLengthAttr().Set(17.0)
    fpv_cam.CreateHorizontalApertureAttr().Set(34.0)
    fpv_dir = os.path.join(RUN_DIR, "frames_fpv"); os.makedirs(fpv_dir, exist_ok=True)
    rp = rep.create.render_product("/World/NavCam", (1920,1080))
    wr = rep.WriterRegistry.get("BasicWriter")
    wr.initialize(output_dir=fpv_dir, rgb=True); wr.attach([rp])

    # ── Setup agent + runner xform ops ──
    agent_xf = UsdGeom.Xformable(agent_prim)
    try: agent_xf.ClearXformOpOrder()
    except: pass
    a_trans = agent_xf.AddTranslateOp()
    a_orient = agent_xf.AddOrientOp()
    a_scale = agent_xf.AddScaleOp()
    a_scale.Set(Gf.Vec3d(*runner_scale))

    # Runner 1
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

    nav_cam = stage.GetPrimAtPath("/World/NavCam")
    timeline = omni.timeline.get_timeline_interface(); timeline.play()
    anim_start = stage.GetStartTimeCode(); anim_end = stage.GetEndTimeCode()
    anim_dur = (anim_end - anim_start) / max(1.0, anim_fps)

    # ── System prompt ──
    if is_multi:
        sys_prompt = make_multistep_system_prompt(task["instruction"])
    else:
        sys_prompt = make_nav_system_prompt(phases[0]["desc"])

    # ── Nav loop ──
    ax, ay, ayaw = agent_start_xy[0], agent_start_xy[1], agent_start_yaw
    apitch = PITCH_INIT; sim_t = 0.0
    cur_phase = 0; inventory = []; action_fb = ""
    nav_hist = []; lamp_on = False

    log(f"[BENCH] Starting nav loop: start=({ax},{ay}) yaw={ayaw}")

    for step in range(max_steps):
        tgt = resolved_targets[cur_phase]
        tgt_radius = phases[cur_phase]["radius"]
        dist = math.sqrt((ax-tgt[0])**2 + (ay-tgt[1])**2)

        # Update agent pose
        a_trans.Set(Gf.Vec3d(ax, ay, GROUND_Z))
        myaw = math.radians(ayaw + MESH_YAW_OFF)
        a_orient.Set(Gf.Quatf(math.cos(myaw/2), 0, 0, math.sin(myaw/2)))

        # Update camera
        if nav_cam and nav_cam.IsValid():
            cxf = UsdGeom.Xformable(nav_cam)
            try: cxf.ClearXformOpOrder()
            except: pass
            cxf.AddTranslateOp().Set(Gf.Vec3d(ax, ay, EYE_H))
            cxf.AddOrientOp().Set(cam_quat(ayaw, apitch))

        # Animate runner
        if runner1_spec and r1_ops:
            rp, rr = sample_human_motion(runner1_spec, sim_t, anim_fps)
            r1_ops["t"].Set(Gf.Vec3d(rp[0], rp[1], GROUND_Z))
            ryr = math.radians(rr[2])
            r1_ops["o"].Set(Gf.Quatf(math.cos(ryr/2), 0, 0, math.sin(ryr/2)))

        # Timeline
        at = anim_start/anim_fps + (sim_t % anim_dur) if anim_dur > 0 else sim_t
        timeline.set_current_time(at)

        # Render
        rep.orchestrator.step(rt_subframes=16)

        # Wait for frame
        t0 = time.time()
        frame_path = None
        while time.time() - t0 < 5.0:
            frames = sorted(glob.glob(os.path.join(fpv_dir, "rgb_*.png")))
            if len(frames) >= step + 1:
                frame_path = frames[-1]; break
            time.sleep(0.1)
        if not frame_path:
            log(f"[BENCH] Step {step}: frame timeout"); break

        # Build prompt
        ph = phases[cur_phase]
        if is_multi:
            inv_s = ','.join(inventory) if inventory else 'empty'
            lamp_s = " Lamp: ON." if lamp_on else ""
            prompt = (f"Current objective: go to {ph['desc']} and use {ph['action']}. "
                      f"Carrying: [{inv_s}].{lamp_s} Progress: step {cur_phase+1}/{len(phases)}. "
                      f"What action should you take?")
            if action_fb:
                prompt += f" ⚠ PREVIOUS ACTION FAILED: {action_fb}"
        else:
            prompt = f"Navigate to {ph['desc']}. What action should you take?"

        # History
        if nav_hist:
            recent = nav_hist[-8:]
            hlines = []
            for h in recent:
                ms = "BLOCKED" if h.get("blocked") else ("moved" if h.get("moved") else "no movement")
                hlines.append(f"Step {h['step']}: {h['action']} ({ms}, yaw={h['yaw']:.0f}°)")
            prompt += " Recent history:\n" + "\n".join(hlines)
            rm = [h.get("moved", True) for h in nav_hist[-3:]]
            if len(rm) >= 3 and not any(rm):
                prompt += "\n⚠ WARNING: You have NOT moved for 3+ steps. Try a different direction."

        fq = check_frame_quality(frame_path)
        if fq.get('guidance'): prompt += fq['guidance']

        log(f"[BENCH] Step {step}: ({ax:.2f},{ay:.2f}) yaw={ayaw:.0f} dist={dist:.2f} phase={cur_phase+1}/{len(phases)}")

        action, fallback = query_vlm(frame_path, prompt, sys_prompt, step)
        action_fb = ""

        # STOP confirm
        if action == "STOP":
            for cr in range(1, STOP_CONFIRM):
                ca, _ = query_vlm(frame_path, "You chose STOP. Is the target within arm's reach? Confirm.", sys_prompt, step)
                if ca != "STOP": action = ca; break

        pre_x, pre_y = ax, ay
        nav_hist.append({"step":step,"x":round(ax,3),"y":round(ay,3),"yaw":round(ayaw,1),
                         "dist_to_target":round(dist,3),"action":action,"moved":False,"blocked":False})

        # ── Execute action ──
        if action == "STOP":
            if ph["action"] == "STOP" and dist < tgt_radius:
                cur_phase += 1
                log(f"[BENCH] STOP success phase {cur_phase}/{len(phases)} dist={dist:.2f}")
                if cur_phase >= len(phases):
                    log(f"[BENCH] ALL PHASES DONE — SUCCESS at step {step}")
                    break
                tgt = resolved_targets[cur_phase]
            else:
                action_fb = f"STOP rejected: still need to {ph['desc']}."
                log(f"[BENCH] STOP rejected dist={dist:.2f}")

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
            for sz in [0.5, 1.0]:
                hit = query_if.sweep_sphere_closest(0.2, carb.Float3(ax,ay,sz),
                                                     carb.Float3(dx,dy,0), STEP_DIST)
                if hit["hit"]: blocked = True; break
            if not blocked:
                ax += STEP_DIST * dx; ay += STEP_DIST * dy
            else:
                nav_hist[-1]["blocked"] = True; log(f"[BENCH] Step {step}: COLLISION")

        elif action == "TURN_LEFT": ayaw += TURN_ANG
        elif action == "TURN_RIGHT": ayaw -= TURN_ANG
        elif action == "TILT_UP": apitch = min(apitch + TILT_ANG, PITCH_MAX)
        elif action == "TILT_DOWN": apitch = max(apitch - TILT_ANG, PITCH_MIN)

        ayaw = wrap_angle_deg(ayaw)
        did_move = abs(ax-pre_x) > 0.001 or abs(ay-pre_y) > 0.001
        nav_hist[-1]["moved"] = did_move
        sim_t += RUNNER_TIME_PER_STEP
    else:
        log(f"[BENCH] TIMEOUT after {max_steps} steps, dist={dist:.2f}")

    # ── Save results ──
    metrics = compute_metrics(nav_hist, task, cur_phase, len(phases))
    results = {"task": task, "metrics": metrics, "nav_history": nav_hist,
               "resolved_targets": resolved_targets}
    with open(os.path.join(RUN_DIR, "results.json"), "w") as f:
        json.dump(results, f, indent=2)

    log(f"[BENCH] Results: SR={metrics['task_success_rate']} SP={metrics['subtask_progress']:.0%} "
        f"GD={metrics['goal_distance_m']:.2f}m Steps={metrics['steps_used']}")
    log(f"[BENCH] Saved to {RUN_DIR}")
    sim_app.close()

except Exception as e:
    with open(LOG, "a") as f:
        f.write(f"\n[BENCH] FATAL ERROR:\n{traceback.format_exc()}")
    raise
