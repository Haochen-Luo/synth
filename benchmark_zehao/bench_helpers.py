"""Shared helpers for 4DSynth-Nav benchmark runner and evaluator."""
import json, os, math, re, glob

# ── Motion sampling (from Zehao's runtime) ──
def lerp(a,b,alpha): return float(a)*(1-alpha)+float(b)*alpha
def lerp_angle_deg(a,b,alpha):
    d=((float(b)-float(a)+180)%360)-180; return float(a)+d*float(alpha)
def wrap_angle_deg(v): return ((float(v)+180)%360)-180

def trajectory_loop_mode(hs):
    m=str(hs.get("trajectory_loop_mode","") or "").strip().lower()
    if m in {"ping_pong_180_turn","pingpong_180_turn","ping_pong"}: return "ping_pong_180_turn"
    return "repeat"

def _looped_frame(hs,elapsed_s,fps,sf,ef,fcl):
    fcl=max(1,int(fcl or 1)); mode=trajectory_loop_mode(hs)
    if mode=="ping_pong_180_turn":
        full=float(max(2,2*fcl)); fo=(float(elapsed_s)*float(fps))%full
        if fo<float(fcl): return float(sf)+fo, False
        return float(ef)-(fo-float(fcl)), True
    return float(sf)+(float(elapsed_s)*float(fps))%float(fcl), False

def sample_human_motion(hs, elapsed_s, fps):
    samples=hs.get("trajectory_keyframes_world",[])
    if not isinstance(samples,list) or not samples:
        rot=list(hs.get("rotation_deg_xyz",[0,0,0]))[:3]
        pos=list(hs.get("placement_location_m",[0,0,0]))[:3]
        while len(rot)<3: rot.append(0.0)
        while len(pos)<3: pos.append(0.0)
        return pos,rot
    ordered=sorted(samples,key=lambda x:int(x.get("frame",1) or 1))
    sf=int(ordered[0].get("frame",1) or 1)
    ef=int(ordered[-1].get("frame",sf) or sf)
    cl=int(hs.get("trajectory_cycle_frame_count",ef-sf+1) or (ef-sf+1))
    loop=bool(hs.get("loop_trajectory",False)) and cl>1 and len(ordered)>=2
    if loop: ff,rev=_looped_frame(hs,elapsed_s,fps,sf,ef,cl)
    else: ff=min(float(ef),sf+float(elapsed_s)*float(fps)); rev=False
    if ff<=float(sf): sa,sb,alpha=ordered[0],ordered[0],0.0
    elif ff>=float(ef): sa,sb,alpha=ordered[-1],ordered[-1],0.0
    else:
        sa,sb,alpha=ordered[0],ordered[-1],0.0
        for i in range(len(ordered)-1):
            a,b=ordered[i],ordered[i+1]
            fa=float(a.get("frame",sf) or sf); fb=float(b.get("frame",sf) or sf)
            if fa<=ff<=fb:
                sa,sb=a,b; alpha=0.0 if abs(fb-fa)<1e-6 else (ff-fa)/(fb-fa); break
    ta=list(sa.get("translation_m",hs.get("placement_location_m",[0,0,0])))[:3]
    tb=list(sb.get("translation_m",ta))[:3]
    ra=list(sa.get("rotation_deg_xyz",hs.get("rotation_deg_xyz",[0,0,0])))[:3]
    rb=list(sb.get("rotation_deg_xyz",ra))[:3]
    pos=[lerp(ta[i],tb[i],alpha) for i in range(3)]
    rot=[lerp_angle_deg(ra[i],rb[i],alpha) for i in range(3)]
    if rev: rot[2]=wrap_angle_deg(rot[2]+180.0)
    return pos, rot

def check_frame_quality(image_path):
    try:
        from PIL import Image; import numpy as np
        img=Image.open(image_path).convert('L'); pixels=np.array(img)
        mean_val=float(pixels.mean())
        dark_frac=float((pixels<15).sum())/pixels.size
        bright_frac=float((pixels>240).sum())/pixels.size
        r={'mean_brightness':mean_val,'is_dark':False,'is_overexposed':False,'guidance':''}
        if mean_val<12 or dark_frac>0.85:
            r['is_dark']=True
            r['guidance']=" VISUAL WARNING: The current camera frame is almost entirely BLACK. You are likely facing a wall. Consider turning left or right."
        elif mean_val>235 or bright_frac>0.85:
            r['is_overexposed']=True
            r['guidance']=" VISUAL WARNING: The current camera frame is almost entirely WHITE. Consider turning to find an open area."
        return r
    except: return {'mean_brightness':-1,'is_dark':False,'is_overexposed':False,'guidance':''}

# ── Scene discovery ──
def discover_scene_files(scene_dir):
    """Auto-detect compiled stage, spec, and human USD from a scene package."""
    stage = glob.glob(os.path.join(scene_dir,"compiled_stages","*.compiled.usda"))
    spec = glob.glob(os.path.join(scene_dir,"compiled_specs","*.compiled.spec.json"))
    humans = glob.glob(os.path.join(scene_dir,"assets","humans","*.usdc"))
    if not humans:
        # full_scenarios_extracted/ nests each human one level deeper:
        # assets/humans/<obj_name>/<obj_name>.usdc. Only consulted when the
        # flat layout above finds nothing, so legacy scene packages (which DO
        # have flat humans/*.usdc) keep byte-identical behavior. Sorted so
        # obj_1_* (the agent) precedes obj_2_* in multi-runner scenes.
        humans = sorted(glob.glob(os.path.join(scene_dir,"assets","humans","*","*.usdc")))
    return {
        "stage": stage[0] if stage else None,
        "spec": spec[0] if spec else None,
        "human_usds": humans,
    }

def find_prim_by_factory(stage, factory_class):
    """Find first prim path whose name contains the factory class string.
    Searches all prims under /World/ (not just /World/Env/)."""
    key = factory_class.replace("Factory","")
    # First try exact factory class match (e.g. "SofaFactory" in path)
    for prim in stage.Traverse():
        path = str(prim.GetPath())
        if "/World/" in path and factory_class in path:
            # Skip deep children — want the top-level prim
            parts = path.split("/")
            if len(parts) <= 5:  # /World/Env/SofaFactory_xxx or /World/SofaFactory_xxx
                return path
    # Fallback: search for stripped key (e.g. "Sofa" in path)
    for prim in stage.Traverse():
        path = str(prim.GetPath())
        name = path.split("/")[-1]
        if key in name and "/World/" in path:
            parts = path.split("/")
            if len(parts) <= 5:
                return path
    return None

def find_all_prims_by_factory(stage, factory_class):
    """Find ALL prim paths matching a factory class."""
    results = []
    key = factory_class.replace("Factory","")
    for prim in stage.Traverse():
        path = str(prim.GetPath())
        name = path.split("/")[-1]
        if (key in name or factory_class in name) and "/World/" in path:
            parts = path.split("/")
            if len(parts) <= 5:
                results.append(path)
    return results

def get_prim_world_center(stage, prim_path):
    """Get the world-space center of a prim's bounding box. Returns [x,y,z] or None."""
    from pxr import UsdGeom, Gf
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid(): return None
    try:
        imageable = UsdGeom.Imageable(prim)
        bbox = imageable.ComputeWorldBound(0, "default")
        r = bbox.GetRange()
        if r.IsEmpty(): return None
        c = (r.GetMin() + r.GetMax()) / 2.0
        return [float(c[0]), float(c[1]), float(c[2])]
    except: return None

# ── Target resolution (single source of truth: generator + validator + runner) ──
# In these compiled stages the real geometry lives under
#   /World/Env/Obj_<id>_<Factory>/<Factory>_<facId>_spawn_asset_<spawnId>   (ACTIVE)
# while the manifest-style path stored in task JSON,
#   /World/Env/<Factory>__spawn_asset_<id>_  (double underscore + trailing underscore)
# is only an INACTIVE `over` with an empty bbox. Trusting that path silently breaks
# target distance (→ [5,5] fallback) and dedup. resolve_target() keys on the unique
# factory/spawn id, finds the ACTIVE prim carrying it (stage.Traverse() skips inactive
# overs), and walks up to the Obj_<id>_<Factory> wrapper. NEVER invents coordinates.

_TARGET_CONTAINERS = ("/World/Env", "/World/InteractiveProps")

def _extract_id_tokens(target_prim):
    """Parse (factory_id, spawn_id) from a target_prim leaf name, or (None, None)."""
    if not target_prim:
        return None, None
    leaf = str(target_prim).rstrip("/").split("/")[-1]
    m = re.search(r'_(\d+)__spawn_asset_(\d+)_?$', leaf)
    if m:
        return m.group(1), m.group(2)
    ids = re.findall(r'\d+', leaf)
    if len(ids) >= 2:
        return ids[0], ids[1]
    if len(ids) == 1:
        return ids[0], None
    return None, None

def _world_bbox(stage, prim):
    """Return (center, bmin, bmax) in world space, or None if the bbox is empty."""
    from pxr import UsdGeom
    try:
        bound = UsdGeom.Imageable(prim).ComputeWorldBound(0, "default")
        r = bound.GetRange()
        if r.IsEmpty():
            return None
        mn, mx = r.GetMin(), r.GetMax()
        c = (mn + mx) / 2.0
        return ([float(c[0]), float(c[1]), float(c[2])],
                [float(mn[0]), float(mn[1]), float(mn[2])],
                [float(mx[0]), float(mx[1]), float(mx[2])])
    except Exception:
        return None

def _find_active_prim_by_ids(stage, fac_id, spawn_id):
    """First ACTIVE prim whose name carries the unique id(s), most-specific first."""
    for required in ([fac_id, spawn_id], [fac_id], [spawn_id]):
        required = [t for t in required if t]
        if not required:
            continue
        for prim in stage.Traverse():  # default predicate = active+defined → skips overs
            name = prim.GetName()
            if all(tok in name for tok in required):
                return prim
    return None

def resolve_target(stage, phase):
    """Resolve a phase's target to the REAL active geometry on the compiled stage.

    Returns {prim_path, center, bbox_min, bbox_max, half_extent_xy} (prim_path is the
    Obj_<id>_<Factory> wrapper, suitable for dedup/center), or **None** if no active
    prim with a valid (non-empty) bbox can be found. Callers MUST treat None as a hard
    failure (runner aborts the task, generator quarantines it) — never substitute a
    placeholder coordinate.
    """
    phase = phase or {}
    target_prim = phase.get("target_prim", "") or ""
    factory = phase.get("target_object", "") or ""

    fac_id, spawn_id = _extract_id_tokens(target_prim)
    candidate = _find_active_prim_by_ids(stage, fac_id, spawn_id) if fac_id else None

    # Fallback 1: explicit path, only if it is itself active with a real bbox.
    if candidate is None and target_prim:
        p = stage.GetPrimAtPath(target_prim)
        if p and p.IsValid() and p.IsActive() and _world_bbox(stage, p):
            candidate = p
    # Fallback 2: by factory class (first active match).
    if candidate is None and factory:
        pp = find_prim_by_factory(stage, factory)
        if pp:
            candidate = stage.GetPrimAtPath(pp)

    if candidate is None or not candidate.IsValid():
        return None

    # Walk up to the wrapper that is a direct child of a known container.
    wrapper, cur = candidate, candidate
    while cur and cur.IsValid():
        parent = cur.GetParent()
        if not parent or not parent.IsValid():
            break
        if parent.GetPath().pathString in _TARGET_CONTAINERS:
            wrapper = cur
            break
        cur = parent

    bb = _world_bbox(stage, wrapper) or _world_bbox(stage, candidate)
    if bb is None:
        return None
    center, bmin, bmax = bb
    bw, bd = bmax[0] - bmin[0], bmax[1] - bmin[1]
    half_ext = max(bw, bd) / 2.0 if 0 < bw < 100 else 0.0
    return {
        "prim_path": wrapper.GetPath().pathString,
        "center": center,
        "bbox_min": bmin,
        "bbox_max": bmax,
        "half_extent_xy": half_ext,
    }

# ── Prompt builders ──
def make_nav_system_prompt(target_desc):
    return f"""You are a navigation robot inside an indoor room. Your goal is to reach the {target_desc}.

You see the room from your first-person camera. You receive up to 3 images showing your recent viewpoints (oldest to newest) — use them to understand how the scene has changed and avoid repeating failed actions.

There may be people moving — avoid colliding with them.

You can use these actions:
- MOVE_FORWARD (move 0.25 meters in your current facing direction)
- TURN_LEFT (rotate 15 degrees to the left)
- TURN_RIGHT (rotate 15 degrees to the right)
- TILT_UP (tilt camera up by 5 degrees)
- TILT_DOWN (tilt camera down by 5 degrees)
- PAUSE (stand still for one step — your position and facing don't change, but time passes and other agents keep moving)
- DONE (you believe you have reached the target — this is your final answer for this navigation)

Each action is SMALL. Plan a SEQUENCE of up to 5 actions you are confident about
from the current view (e.g. several MOVE_FORWARD in a row, or a few turns then
moves). The robot executes your plan one action at a time and STOPS the plan
early if it hits an obstacle or reaches the target — so plan boldly but sensibly.

You will be shown your recent plans, which actions actually executed, and why
each plan ended (e.g. "BLOCKED at action 1"). Use this: if a plan was BLOCKED,
the route ahead is obstructed — do NOT plan the same direction again, turn away.

Rules:
- If the target is directly ahead, queue multiple MOVE_FORWARD.
- If the target is to your left or right, queue turns first, then moves.
- If a person is in your path, plan a route around them.
- If your last plan was BLOCKED, queue several turns to face a clear direction.
- When you expect to be within arm's reach, end the plan with DONE.

First, briefly explain your reasoning. Then, as the VERY LAST line, output ONLY
a comma-separated plan of 1-5 actions:
PLAN: <action_1>, <action_2>, ..., <action_n>"""

def make_multistep_system_prompt(instruction):
    return f"""You are a service robot inside an indoor room. Your task:
{instruction}

You receive up to 3 images showing your recent viewpoints (oldest to newest) — use them to understand how the scene has changed and avoid repeating failed actions.

You can use these actions:
- MOVE_FORWARD (move 0.25 meters in your current facing direction)
- TURN_LEFT (rotate 15 degrees to the left)
- TURN_RIGHT (rotate 15 degrees to the right)
- TILT_UP (tilt camera up by 5 degrees)
- TILT_DOWN (tilt camera down by 5 degrees)
- PAUSE (stand still for one step — your position and facing don't change, but time passes and other agents keep moving)
- PICK_UP (pick up an object near you — only works when very close)
- PUT_DOWN (put down the object you are carrying — only works near target)
- TURN_ON (turn on a device near you — only works when very close)
- DONE (you have completed the final step of the task)

Each action is SMALL. Plan a SEQUENCE of up to 5 actions you are confident about
from the current view (e.g. several MOVE_FORWARD, or turns then moves). The robot
executes your plan one action at a time and STOPS the plan early if it hits an
obstacle or completes a sub-task — so plan boldly but sensibly.

You will be shown your recent plans, which actions actually executed, and why
each plan ended (e.g. "BLOCKED at action 1"). Use this: if a plan was BLOCKED,
the route ahead is obstructed — do NOT plan the same direction again, turn away.

Rules:
- Think step by step: what sub-task should I do next?
- Queue navigation moves to approach the current target.
- If your last plan was BLOCKED, queue several turns to face a clear direction.
- Put an interaction action (PICK_UP / PUT_DOWN / TURN_ON / DONE) as the LAST
  action of a plan only when you expect to be within arm's reach by then.
- Do NOT attempt PICK_UP/PUT_DOWN unless you expect the object within reach.
- Only use DONE after ALL steps of the task are done.

First, briefly explain your reasoning. Then, as the VERY LAST line, output ONLY
a comma-separated plan of 1-5 actions:
PLAN: <action_1>, <action_2>, ..., <action_n>"""

# ── Metrics computation ──
def compute_metrics(nav_history, task_config, completed_phases, total_phases):
    """Compute benchmark metrics from a single episode."""
    n_steps = len(nav_history)
    sr = 1.0 if completed_phases >= total_phases else 0.0
    sp = completed_phases / max(1, total_phases)
    gd = nav_history[-1]["dist_to_target"] if nav_history else float('inf')
    static_collisions = sum(1 for h in nav_history if h.get("blocked_reason") == "static_obstacle")
    dynamic_collisions = sum(1 for h in nav_history if h.get("blocked_reason") == "dynamic_runner")
    collision_count = static_collisions + dynamic_collisions
    return {
        "task_id": task_config["id"],
        "level": task_config["level"],
        "scene": task_config["scene_dir"],
        "instruction": task_config["instruction"],
        "success": bool(sr),
        "task_success_rate": sr,
        "subtask_progress": sp,
        "completed_phases": completed_phases,
        "total_phases": total_phases,
        "goal_distance_m": round(gd, 3),
        "steps_used": n_steps,
        "max_steps": task_config.get("max_steps", 150),
        "timeout": n_steps >= task_config.get("max_steps", 150),
        "collision_count": collision_count,
        "static_collision_count": static_collisions,
        "dynamic_runner_collision_count": dynamic_collisions,
        "collision_rate": round(collision_count / max(1, n_steps), 3),
    }

def aggregate_metrics(all_results):
    """Aggregate per-episode metrics into per-level and overall summaries."""
    from collections import defaultdict
    by_level = defaultdict(list)
    for r in all_results:
        by_level[r["level"]].append(r)
    
    summary = {"per_level": {}, "overall": {}}
    for level in sorted(by_level.keys()):
        rs = by_level[level]
        n = len(rs)
        summary["per_level"][level] = {
            "n_tasks": n,
            "success_rate": sum(r["task_success_rate"] for r in rs) / n,
            "avg_subtask_progress": sum(r["subtask_progress"] for r in rs) / n,
            "avg_goal_distance_m": sum(r["goal_distance_m"] for r in rs) / n,
            "avg_steps_used": sum(r["steps_used"] for r in rs) / n,
            "avg_collision_count": sum(r.get("collision_count", 0) for r in rs) / n,
            "avg_static_collision_count": sum(r.get("static_collision_count", 0) for r in rs) / n,
            "avg_dynamic_runner_collision_count": sum(r.get("dynamic_runner_collision_count", 0) for r in rs) / n,
            "avg_agent_pushed_events": sum(r.get("agent_pushed_events", 0) for r in rs) / n,
            "avg_agent_pushed_frames": sum(r.get("agent_pushed_frames", 0) for r in rs) / n,
            "avg_steps_success": (
                sum(r["steps_used"] for r in rs if r["success"]) /
                max(1, sum(1 for r in rs if r["success"]))
            ),
            "timeout_rate": sum(1 for r in rs if r["timeout"]) / n,
        }
    
    n_all = len(all_results)
    if n_all:
        summary["overall"] = {
            "n_tasks": n_all,
            "success_rate": sum(r["task_success_rate"] for r in all_results) / n_all,
            "avg_subtask_progress": sum(r["subtask_progress"] for r in all_results) / n_all,
            "avg_goal_distance_m": sum(r["goal_distance_m"] for r in all_results) / n_all,
            "avg_steps_used": sum(r["steps_used"] for r in all_results) / n_all,
            "avg_collision_count": sum(r.get("collision_count", 0) for r in all_results) / n_all,
            "avg_agent_pushed_events": sum(r.get("agent_pushed_events", 0) for r in all_results) / n_all,
            "avg_agent_pushed_frames": sum(r.get("agent_pushed_frames", 0) for r in all_results) / n_all,
        }
    return summary

def print_report(summary, all_results):
    """Pretty-print benchmark results."""
    print("\n" + "="*70)
    print("  4DSynth-Nav Benchmark Results")
    print("="*70)
    
    for level, s in sorted(summary["per_level"].items()):
        print(f"\n  {level} ({s['n_tasks']} tasks):")
        print(f"    SR={s['success_rate']:.1%}  SP={s['avg_subtask_progress']:.1%}  "
              f"GD={s['avg_goal_distance_m']:.2f}m  Steps={s['avg_steps_used']:.0f}  "
              f"Coll={s.get('avg_collision_count', 0):.1f}  "
              f"Pushed={s.get('avg_agent_pushed_events', 0):.1f}ev/"
              f"{s.get('avg_agent_pushed_frames', 0):.1f}fr  "
              f"Timeout={s['timeout_rate']:.0%}")

    o = summary.get("overall", {})
    if o:
        print(f"\n  OVERALL ({o['n_tasks']} tasks):")
        print(f"    SR={o['success_rate']:.1%}  SP={o['avg_subtask_progress']:.1%}  "
              f"GD={o['avg_goal_distance_m']:.2f}m  Steps={o['avg_steps_used']:.0f}  "
              f"Coll={o.get('avg_collision_count', 0):.1f}  "
              f"Pushed={o.get('avg_agent_pushed_events', 0):.1f}ev/"
              f"{o.get('avg_agent_pushed_frames', 0):.1f}fr")

    print("\n  Per-task details:")
    for r in all_results:
        s = "✅" if r["success"] else ("⏰" if r["timeout"] else "❌")
        print(f"    {s} {r['task_id']:8s}  SP={r['subtask_progress']:.0%}  "
              f"GD={r['goal_distance_m']:.1f}m  Steps={r['steps_used']}  "
              f"Coll={r.get('collision_count', 0)}  "
              f"Pushed={r.get('agent_pushed_events', 0)}ev/"
              f"{r.get('agent_pushed_frames', 0)}fr  "
              f"{r['instruction'][:50]}")
    print("="*70)
