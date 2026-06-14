#!/usr/bin/env python3
"""Determine why the case11 mattress collider isn't query-able at spawn time.
Reads the PHYSICS SCHEMA of the mattress + pillow + bed + floor prims: RigidBodyAPI
(dynamic, falls under gravity?), CollisionAPI (has a collider at all?), and whether
collision is enabled. Answers: is the mattress a dynamic body that FALLS at sim
start (explaining 'pillow drops' + the collider appearing mid-episode)?
NO render. Out: /tmp/phys_schema.txt"""
import os
OUT=open("/tmp/phys_schema.txt","w")
def emit(s): OUT.write(str(s)+"\n"); OUT.flush(); print(s,flush=True)

SCENE=("/home/liuqi/hc/synth/benchmark_zehao/full_scenarios_extracted/"
       "native_case11_bedroom_lift_full_physics_scene/compiled_stages/"
       "native_case11_bedroom_lift_full_physics.compiled.usda")

from isaacsim import SimulationApp
app=SimulationApp({"headless":True})
from omni.isaac.core.utils.stage import open_stage, is_stage_loading
from pxr import Usd, UsdPhysics, UsdGeom, PhysxSchema
import omni.usd
open_stage(SCENE)
n=0
while is_stage_loading() and n<3000: app.update(); n+=1
stage=omni.usd.get_context().get_stage()

def describe(prim):
    p=prim.GetPath().pathString
    has_rb = prim.HasAPI(UsdPhysics.RigidBodyAPI)
    has_col= prim.HasAPI(UsdPhysics.CollisionAPI)
    # rigid body enabled? kinematic?
    rb_en=kin=None
    if has_rb:
        rb=UsdPhysics.RigidBodyAPI(prim)
        a=rb.GetRigidBodyEnabledAttr(); rb_en=a.Get() if a else None
        a=rb.GetKinematicEnabledAttr(); kin=a.Get() if a else None
    col_en=None
    if has_col:
        c=UsdPhysics.CollisionAPI(prim)
        a=c.GetCollisionEnabledAttr(); col_en=a.Get() if a else None
    return f"RigidBody={has_rb}(enabled={rb_en},kinematic={kin}) Collision={has_col}(enabled={col_en}) active={prim.IsActive()}"

emit("=== physics schema of key prims (and their ancestors) ===")
targets=["mattress","pillow","bed","floor"]
seen=set()
for prim in stage.Traverse():
    nl=prim.GetName().lower(); pl=prim.GetPath().pathString.lower()
    if not any(t in nl or t in pl for t in targets): continue
    # only report prims that actually carry physics APIs (collider/body)
    if prim.HasAPI(UsdPhysics.RigidBodyAPI) or prim.HasAPI(UsdPhysics.CollisionAPI):
        key=prim.GetName()
        if key in seen: continue
        seen.add(key)
        emit(f"  {prim.GetPath().pathString[:70]}")
        emit(f"      {describe(prim)}")

emit("\n=== is there a global PhysicsScene + gravity? ===")
for prim in stage.Traverse():
    if prim.IsA(UsdPhysics.Scene):
        sc=UsdPhysics.Scene(prim)
        g=sc.GetGravityMagnitudeAttr().Get() if sc.GetGravityMagnitudeAttr() else None
        d=sc.GetGravityDirectionAttr().Get() if sc.GetGravityDirectionAttr() else None
        emit(f"  PhysicsScene {prim.GetPath()} gravity_mag={g} dir={d}")
emit(f"\nmattress/pillow/bed physics prims found: {len(seen)}")
app.close()
