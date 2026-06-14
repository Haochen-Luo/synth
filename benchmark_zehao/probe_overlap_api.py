#!/usr/bin/env python3
"""Verify the PhysX query interface has an overlap API and that it detects the
case11 mattress at the spawn (where sweep MISSES it). Confirms both the fix API
and the Q1 diagnosis (sweep misses initial penetration; overlap catches it).
NO render. Out: /tmp/overlap_probe.txt"""
import os
OUT=open("/tmp/overlap_probe.txt","w")
def emit(s): OUT.write(str(s)+"\n"); OUT.flush(); print(s,flush=True)

SCENE=("/home/liuqi/hc/synth/benchmark_zehao/full_scenarios_extracted/"
       "native_case11_bedroom_lift_full_physics_scene/compiled_stages/"
       "native_case11_bedroom_lift_full_physics.compiled.usda")
SPAWN=(9.5,10.0)

from isaacsim import SimulationApp
app=SimulationApp({"headless":True})
from omni.isaac.core.utils.stage import open_stage, is_stage_loading
import omni.usd, carb
open_stage(SCENE)
n=0
while is_stage_loading() and n<3000: app.update(); n+=1

# Replicate bench_runner init: 100-update warmup (line 837), timeline stopped.
import omni.timeline
omni.timeline.get_timeline_interface().stop()
emit("warmup: 100 app.update() (matching bench_runner)...")
for _ in range(100): app.update()

# get the physx query interface the way bench_runner does (after warmup, line 1377)
import omni.physx
app.update()
query_if=omni.physx.get_physx_scene_query_interface()
emit("=== query interface methods (overlap / sweep) ===")
for m in dir(query_if):
    if "overlap" in m.lower() or "sweep" in m.lower() or "raycast" in m.lower():
        emit(f"  {m}")

R=0.40
# Scan many z heights to find WHERE the mattress collider actually is, and test
# overlap_sphere_any (cheapest) + overlap_sphere (enumerate) + raycast straight down.
emit(f"\n=== z-scan overlap_sphere_any at spawn (find collider height) ===")
import carb as _carb
for sz in [0.1,0.3,0.5,0.7,0.9,1.0,1.3,1.6]:
    try:
        any_hit=query_if.overlap_sphere_any(R, _carb.Float3(SPAWN[0],SPAWN[1],sz))
    except Exception as e:
        any_hit=f"ERR {e}"
    emit(f"  z={sz}: overlap_sphere_any = {any_hit}")
emit("=== raycast DOWN from z=2.0 (what's under spawn?) ===")
try:
    rh=query_if.raycast_closest(_carb.Float3(SPAWN[0],SPAWN[1],2.0), _carb.Float3(0,0,-1), 3.0)
    if rh["hit"]:
        wp=(rh.get("rigidBody") or rh.get("collider") or "")
        emit(f"  hit {wp.split('/')[-1][:50]} at dist={rh.get('distance',-1):.2f} (z={2.0-rh.get('distance',0):.2f})")
    else:
        emit("  raycast down: NO HIT")
except Exception as e:
    emit(f"  raycast down ERR: {e}")

emit(f"\n=== at spawn ({SPAWN[0]},{SPAWN[1]}) — sweep vs overlap at z=0.5,1.0 ===")
for sz in (0.5,1.0):
    # SWEEP (current validator method) — tiny travel, 8 dirs; report any hit
    import math
    DIRS=[(math.cos(math.radians(a)),math.sin(math.radians(a))) for a in range(0,360,45)]
    sweep_hit=None
    for dx,dy in DIRS:
        h=query_if.sweep_sphere_closest(R,carb.Float3(SPAWN[0],SPAWN[1],sz),carb.Float3(dx,dy,0),0.05)
        if h["hit"]:
            wp=(h.get("rigidBody") or h.get("collider") or "")
            d=float(h.get("distance",-1))
            if "mattress" in wp.lower(): sweep_hit=(wp.split("/")[-1][:40],d)
    emit(f"  z={sz}: SWEEP mattress hit = {sweep_hit}")

    # OVERLAP — try the available overlap method signatures
    ov_hits=[]
    def _cb(hit):
        wp=(getattr(hit,'rigid_body',None) or getattr(hit,'collision',None) or
            (hit.get('rigidBody') if isinstance(hit,dict) else None) or
            (hit.get('collider') if isinstance(hit,dict) else None) or "")
        ov_hits.append(str(wp)); return True
    method=None
    for cand in ("overlap_sphere","overlap_sphere_any"):
        if hasattr(query_if,cand): method=cand; break
    emit(f"  z={sz}: overlap method available = {method}")
    if method=="overlap_sphere":
        try:
            cnt=query_if.overlap_sphere(R, carb.Float3(SPAWN[0],SPAWN[1],sz), _cb, False)
            mats=[h for h in ov_hits if "mattress" in h.lower()]
            emit(f"        overlap_sphere count={cnt} total_hits={len(ov_hits)} mattress_hits={len(mats)}")
            for h in ov_hits[:8]: emit(f"          - {h.split('/')[-1][:50]}")
        except Exception as e:
            emit(f"        overlap_sphere ERR: {e}")
    elif method=="overlap_sphere_any":
        try:
            any_hit=query_if.overlap_sphere_any(R, carb.Float3(SPAWN[0],SPAWN[1],sz))
            emit(f"        overlap_sphere_any -> {any_hit}")
        except Exception as e:
            emit(f"        overlap_sphere_any ERR: {e}")

app.close()
