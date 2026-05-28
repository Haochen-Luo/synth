#!/usr/bin/env python3
"""Probe sky/dome/environment prims in case06 scene to find what causes
the red sky bleed in bird's-eye view.

Usage (inside vlm-jupyter container):
  /isaac-sim/python.sh probe_sky.py
"""
import os, sys, json
sys.path.insert(0, "/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao")

OUT_FILE = "/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/probe_sky_result.txt"
TASKS_JSON = "/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/benchmark_tasks.json"

with open(TASKS_JSON) as f:
    cfg = json.load(f)
task = {t["id"]: t for t in cfg["tasks"]}["case06-L2"]

from bench_helpers import discover_scene_files
scene_dir = os.path.join("/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao", task["scene_dir"])
sf = discover_scene_files(scene_dir)

from isaacsim import SimulationApp
sim_app = SimulationApp({"headless": True, "width": 64, "height": 64})

import omni
from pxr import Usd, UsdGeom, UsdLux, UsdShade, Gf, Sdf
from omni.isaac.core.utils.stage import is_stage_loading

omni.usd.get_context().open_stage(sf["stage"])
while is_stage_loading():
    sim_app.update()
stage = omni.usd.get_context().get_stage()

lines = []
def log(msg):
    lines.append(msg)

log("=== CASE06 SKY/DOME PROBE ===\n")

# 1. Find prims with sky/dome/environment/hdri in their name or path
log("--- Section 1: Sky/Dome/Environment prims ---")
sky_count = 0
for p in stage.Traverse():
    pname = p.GetName().lower()
    ppath = str(p.GetPath()).lower()
    ptype = p.GetTypeName()
    
    if any(tok in pname for tok in ("sky", "dome", "hdri", "environment", "backdrop")):
        sky_count += 1
        log(f"\n  [{sky_count}] {p.GetPath()}")
        log(f"      Name: {p.GetName()}")
        log(f"      Type: {ptype}")
        log(f"      Active: {p.IsActive()}")
        try:
            img = UsdGeom.Imageable(p)
            vis = img.ComputeVisibility(Usd.TimeCode.Default())
            log(f"      Visibility: {vis}")
        except:
            log(f"      Visibility: N/A")
        
        # Print key attributes
        for attr in p.GetAttributes():
            try:
                val = attr.Get()
                if val is not None and len(str(val)) < 500:
                    log(f"      {attr.GetName()} = {val}")
            except:
                pass
        
        # Check children (1 level)
        for child in p.GetChildren():
            ctype = child.GetTypeName()
            log(f"      Child: {child.GetPath()} (type={ctype})")

if sky_count == 0:
    log("  (No prims with sky/dome/environment/hdri/backdrop in name)\n")

# 2. Find DomeLight prims specifically (these project sky images)
log("\n--- Section 2: DomeLight prims ---")
dome_count = 0
for p in stage.Traverse():
    ptype = p.GetTypeName()
    if ptype == "DomeLight":
        dome_count += 1
        log(f"\n  [{dome_count}] {p.GetPath()}")
        log(f"      Active: {p.IsActive()}")
        for attr in p.GetAttributes():
            try:
                val = attr.Get()
                if val is not None and len(str(val)) < 500:
                    log(f"      {attr.GetName()} = {val}")
            except:
                pass

if dome_count == 0:
    log("  (No DomeLight prims found)\n")

# 3. Look for any large-scale geometry that could be the sky sphere/dome mesh
log("\n--- Section 3: Large-scale geometry (bbox > 20m) ---")
large_count = 0
for p in stage.Traverse():
    if not p.IsA(UsdGeom.Gprim):
        continue
    try:
        imageable = UsdGeom.Imageable(p)
        bound = imageable.ComputeWorldBound(Usd.TimeCode.Default(), "default")
        box = bound.GetBox()
        mn, mx = box.GetMin(), box.GetMax()
        size = max(mx[0]-mn[0], mx[1]-mn[1], mx[2]-mn[2])
        if size > 20:  # > 20m in any dimension
            large_count += 1
            log(f"\n  [{large_count}] {p.GetPath()}")
            log(f"      Type: {p.GetTypeName()}")
            log(f"      BBox: min=({mn[0]:.1f},{mn[1]:.1f},{mn[2]:.1f}) "
                f"max=({mx[0]:.1f},{mx[1]:.1f},{mx[2]:.1f}) "
                f"size={size:.1f}m")
            pname = p.GetName().lower()
            log(f"      Name: {p.GetName()}")
            if large_count > 20:
                log("  ... (truncated)")
                break
    except:
        pass

if large_count == 0:
    log("  (No geometry > 20m found)\n")

# 4. Check /World/Env top-level children for anything sky-like
log("\n--- Section 4: /World/Env top-level children ---")
env_prim = stage.GetPrimAtPath("/World/Env")
if env_prim and env_prim.IsValid():
    for child in env_prim.GetChildren():
        cname = child.GetName()
        ctype = child.GetTypeName()
        # Check bbox for interesting ones
        bbox_str = ""
        try:
            img = UsdGeom.Imageable(child)
            bound = img.ComputeWorldBound(Usd.TimeCode.Default(), "default")
            box = bound.GetBox()
            mn, mx = box.GetMin(), box.GetMax()
            size = max(mx[0]-mn[0], mx[1]-mn[1], mx[2]-mn[2])
            bbox_str = f" bbox_max_dim={size:.1f}m"
        except:
            pass
        log(f"  {child.GetPath()} (type={ctype}{bbox_str})")
else:
    log("  /World/Env not found")

# Write results
with open(OUT_FILE, "w") as f:
    f.write("\n".join(lines) + "\n")

print(f"Probe results written to {OUT_FILE}")
sim_app.close()
