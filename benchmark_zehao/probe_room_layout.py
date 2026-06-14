#!/usr/bin/env python3
"""Read-only static geometry of case11_bedroom_lift: floor XY bbox + every
InteractiveProps furniture XY bbox, so we can hand-pick a spawn that avoids the
mattress (and all furniture) inside the room. NO physics, NO render — just USD
world-bbox reads. Out: /tmp/room_layout.txt"""
import os
OUT=open("/tmp/room_layout.txt","w")
def emit(s): OUT.write(str(s)+"\n"); OUT.flush(); print(s,flush=True)

SCENE=("/home/liuqi/hc/synth/benchmark_zehao/full_scenarios_extracted/"
       "native_case11_bedroom_lift_full_physics_scene/compiled_stages/"
       "native_case11_bedroom_lift_full_physics.compiled.usda")
# known targets/spawn for reference
TARGETS={"plant":(11.7135,11.5181),"shelf":(9.6493,11.5885)}
OLD_SPAWN={"L2/L4":(9.5,10.0),"L3":(9.42,9.42)}

from isaacsim import SimulationApp
app=SimulationApp({"headless":True})
from omni.isaac.core.utils.stage import open_stage, is_stage_loading
from pxr import UsdGeom
import omni.usd
open_stage(SCENE)
n=0
while is_stage_loading() and n<3000: app.update(); n+=1
stage=omni.usd.get_context().get_stage()
bc=UsdGeom.BBoxCache(0,[UsdGeom.Tokens.default_, UsdGeom.Tokens.render])

def bbox_xy(prim):
    r=bc.ComputeWorldBound(prim).ComputeAlignedRange()
    if r.IsEmpty(): return None
    mn,mx=r.GetMin(),r.GetMax()
    return (mn[0],mx[0],mn[1],mx[1],mn[2],mx[2])

emit("=== FLOOR bbox (room extent) ===")
for prim in stage.Traverse():
    nl=prim.GetName().lower()
    if "floor" in nl and "bedroom" in str(prim.GetPath()).lower():
        b=bbox_xy(prim)
        if b: emit(f"  {prim.GetName()[:40]}: x[{b[0]:.2f},{b[1]:.2f}] y[{b[2]:.2f},{b[3]:.2f}]")

emit("\n=== FURNITURE bboxes (top-level InteractiveProps with Factory) — spawn must avoid these ===")
seen=set()
for prim in stage.Traverse():
    p=str(prim.GetPath())
    if "/World/InteractiveProps/" not in p: continue
    # only the immediate factory-level prims (one segment under InteractiveProps)
    seg=p.split("/World/InteractiveProps/")[1]
    if "/" in seg: continue
    if "Factory" not in seg: continue
    if seg in seen: continue
    seen.add(seg)
    b=bbox_xy(prim)
    if not b: continue
    # only furniture that reaches body height (z spans below ~1.6) matters for spawn
    emit(f"  {seg[:46]:46s} x[{b[0]:.2f},{b[1]:.2f}] y[{b[2]:.2f},{b[3]:.2f}] z[{b[4]:.2f},{b[5]:.2f}]")

emit("\n=== reference points ===")
for k,v in TARGETS.items(): emit(f"  target {k}: ({v[0]:.2f},{v[1]:.2f})")
for k,v in OLD_SPAWN.items(): emit(f"  OLD spawn {k}: {v}  (bad — in mattress)")
emit("  mattress XY footprint to AVOID: x[9.32,10.80] y[8.29,10.27]")
app.close()
