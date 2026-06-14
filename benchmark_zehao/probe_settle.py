#!/usr/bin/env python3
"""Track mattress + pillow Z over physics time to see HOW they move at sim start:
- smooth descent  -> simple free-fall
- spike up / jump  -> initial-penetration explosion (PhysX separating impulse)
Also reports the mattress final settled bbox vs the agent spawn (9.5,10.0), to
confirm it falls onto the spawn at z~0.5. NO render. Out: /tmp/settle.txt"""
import os
OUT=open("/tmp/settle.txt","w")
def emit(s): OUT.write(str(s)+"\n"); OUT.flush(); print(s,flush=True)

SCENE=("/home/liuqi/hc/synth/benchmark_zehao/full_scenarios_extracted/"
       "native_case11_bedroom_lift_full_physics_scene/compiled_stages/"
       "native_case11_bedroom_lift_full_physics.compiled.usda")
SPAWN=(9.5,10.0)

from isaacsim import SimulationApp
app=SimulationApp({"headless":True})
from omni.isaac.core.utils.stage import open_stage, is_stage_loading
from pxr import UsdGeom, Gf
import omni.usd, omni.timeline
open_stage(SCENE)
n=0
while is_stage_loading() and n<3000: app.update(); n+=1
stage=omni.usd.get_context().get_stage()

# track these bodies (the dynamic rigid-body parents)
names=["MattressFactory_1722653_spawn_asset_5578863",
       "PillowFactory_1722653_spawn_asset_5578863",
       "PillowFactory_1722653_spawn_asset_5578863_001"]
prims={}
for prim in stage.Traverse():
    nm=prim.GetName()
    if nm in names and nm not in prims:
        prims[nm]=prim

bc=UsdGeom.BBoxCache(0,[UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
def zc(prim):
    bc.SetTime(0)
    r=bc.ComputeWorldBound(prim).ComputeAlignedRange()
    if r.IsEmpty(): return None
    mn,mx=r.GetMin(),r.GetMax()
    return (mn[2],mx[2],(mn[0]+mx[0])/2,(mn[1]+mx[1])/2,mn[0],mx[0],mn[1],mx[1])

emit("=== Z over physics time (play timeline, sample bbox) ===")
emit("frame | " + " | ".join(n[:18] for n in names))
tl=omni.timeline.get_timeline_interface()
def snap(f):
    cells=[]
    for nm in names:
        v=zc(prims[nm])
        cells.append(f"z[{v[0]:.2f},{v[1]:.2f}]" if v else "none")
    emit(f"  {f:4d} | " + " | ".join(cells))

snap(0)
tl.play()
for f in range(1,121):
    app.update()
    if f in (2,5,10,20,40,60,90,120): snap(f)
tl.stop()

emit("\n=== mattress FINAL bbox vs spawn ===")
v=zc(prims[names[0]])
if v:
    inside = v[4]<=SPAWN[0]<=v[5] and v[6]<=SPAWN[1]<=v[7]
    emit(f"  mattress final: x[{v[4]:.2f},{v[5]:.2f}] y[{v[6]:.2f},{v[7]:.2f}] z[{v[0]:.2f},{v[1]:.2f}]")
    emit(f"  SPAWN(9.5,10.0) inside mattress XY footprint = {inside}; "
         f"mattress z spans 0.5? {v[0]<=0.5<=v[1]}")
app.close()
