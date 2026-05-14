#!/usr/bin/env python3
"""Extract object prim paths and positions from scene_inventory + physics_assets.
Also parse the USDA to find prim world positions."""
import json, os, glob, re
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
scenes = sorted(glob.glob(os.path.join(BASE, "native_*_full_physics_scene")))

def clean_type(factory):
    """Extract clean category from factory name."""
    m = re.match(r'(\w+?)Factory', factory)
    if m:
        raw = m.group(1)
        return re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', raw)
    return factory

# Scan physics_assets.json + runtime_intent.json for object positions
for scene_dir in scenes:
    sname = os.path.basename(scene_dir).replace("native_", "").replace("_full_physics_scene", "")
    
    # Parse runtime_intent for prim paths and physics roles
    ri_path = os.path.join(scene_dir, "runtime_intent.json")
    pa_path = os.path.join(scene_dir, "physics_assets.json")
    inv_path = os.path.join(scene_dir, "scene_inventory.json")
    
    room_type = "Living" if "living" in sname else "Dining"
    print(f"\n{'='*70}")
    print(f"  {sname} ({room_type})")
    print(f"{'='*70}")
    
    # Get object list from scene_inventory
    obj_list = []
    if os.path.exists(inv_path):
        inv = json.load(open(inv_path))
        scene_objects = inv.get("scene_objects", [])
        if isinstance(scene_objects, list):
            for obj in scene_objects:
                name = obj.get("logical_name", obj.get("name", ""))
                factory = obj.get("factory_class", "")
                prims = obj.get("runtime_prim_paths", obj.get("source_prim_paths", []))
                cat = clean_type(factory) if factory else name
                # Skip structural / invisible
                if cat in ("Area", "window", "env_light", "skirtingboard_support", "Point Lamp"):
                    continue
                if "room_0" in cat or "ceiling" in cat.lower():
                    continue
                obj_list.append({"name": name, "cat": cat, "prims": prims, "factory": factory})
    
    # Also try to get positions from physics_assets (has estimation details)
    obj_physics = {}
    if os.path.exists(pa_path):
        pa = json.load(open(pa_path))
        for oa in pa.get("object_assets", []):
            prims = oa.get("source_prim_paths", [])
            role = oa.get("sim_role", "")
            label = oa.get("semantic_label", "")
            est = oa.get("physics_estimation", {})
            mass = est.get("total_mass_kg", None)
            dims = est.get("characteristic_dims_m", {})
            for p in prims:
                obj_physics[p] = {
                    "role": role,
                    "label": label, 
                    "mass_kg": mass,
                    "dims": dims,
                }
    
    # Get activation requests from runtime_intent for positions
    obj_activations = {}
    if os.path.exists(ri_path):
        ri = json.load(open(ri_path))
        for act in ri.get("activation_requests", []):
            prim_path = act.get("target_prim_path", "")
            name = act.get("name", "")
            mode = act.get("requested_mode", "")
            mass = act.get("overrides", {}).get("mass_kg")
            obj_activations[prim_path] = {"name": name, "mode": mode, "mass": mass}
    
    # Print combined
    categories = defaultdict(list)
    for obj in obj_list:
        categories[obj["cat"]].append(obj)
    
    for cat in sorted(categories.keys()):
        objs = categories[cat]
        for obj in objs:
            prims_str = obj["prims"][0] if obj["prims"] else "?"
            phys = obj_physics.get(prims_str, {})
            act = obj_activations.get(prims_str, {})
            role = phys.get("role", "?")
            label = phys.get("label", "")
            mass = phys.get("mass_kg") or act.get("mass")
            dims = phys.get("dims", {})
            
            # Build dims string
            ds = ""
            if dims:
                h = dims.get("height_m", 0)
                w = dims.get("width_m", dims.get("diameter_m", 0))
                d = dims.get("depth_m", 0)
                if h or w or d:
                    ds = f"  {w:.1f}x{d:.1f}x{h:.1f}m" if d else f"  Ø{w:.1f}x{h:.1f}m"
            
            mass_s = f"  {mass:.1f}kg" if mass else ""
            print(f"  {cat:25s}  role={role:8s}{ds}{mass_s}  [{label}]")
