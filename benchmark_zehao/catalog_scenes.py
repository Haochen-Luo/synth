#!/usr/bin/env python3
"""Catalog all scene objects across extracted benchmark scenes."""
import json, os, glob, re, sys
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))

def extract_factory_class(name):
    """Extract clean object type from factory name like 'SofaFactory(123).spawn_asset(456)'."""
    m = re.match(r'(\w+Factory)', name)
    if m:
        raw = m.group(1).replace('Factory', '')
        # CamelCase to readable
        return re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', raw)
    return name

def get_bbox_center(bbox):
    """Compute center from bbox dict or list."""
    if isinstance(bbox, dict):
        bmin = bbox.get('min', bbox.get('bbox_min', [0,0,0]))
        bmax = bbox.get('max', bbox.get('bbox_max', [0,0,0]))
    elif isinstance(bbox, list) and len(bbox) == 6:
        bmin = bbox[:3]
        bmax = bbox[3:]
    else:
        return None
    return [(a+b)/2 for a, b in zip(bmin, bmax)]

def get_bbox_size(bbox):
    if isinstance(bbox, dict):
        bmin = bbox.get('min', bbox.get('bbox_min', [0,0,0]))
        bmax = bbox.get('max', bbox.get('bbox_max', [0,0,0]))
    elif isinstance(bbox, list) and len(bbox) == 6:
        bmin = bbox[:3]
        bmax = bbox[3:]
    else:
        return None
    return [abs(b-a) for a, b in zip(bmin, bmax)]

scenes = sorted(glob.glob(os.path.join(BASE, "native_*_full_physics_scene")))

catalog = {}

for scene_dir in scenes:
    scene_name = os.path.basename(scene_dir).replace("native_", "").replace("_full_physics_scene", "")
    
    # Load scene inventory
    inv_path = os.path.join(scene_dir, "scene_inventory.json")
    spec_files = glob.glob(os.path.join(scene_dir, "compiled_specs", "*.compiled.spec.json"))
    
    scene_info = {"name": scene_name, "objects": [], "humans": [], "room_bounds": None}
    
    # Parse spec for humans and stage info
    if spec_files:
        spec = json.load(open(spec_files[0]))
        humans = spec.get("humans", [])
        for h in humans:
            scene_info["humans"].append({
                "name": h.get("name", ""),
                "position": h.get("placement_location_m", []),
                "scale": h.get("scale_xyz", []),
                "trajectory_frames": len(h.get("trajectory_keyframes_world", [])),
            })
        stage = spec.get("stage", {})
        scene_info["fps"] = stage.get("time_codes_per_second", 10.0)
    
    # Parse scene inventory for objects
    if os.path.exists(inv_path):
        inv = json.load(open(inv_path))
        
        # Try to find room bounds from room structure objects
        objects = inv.get("scene_objects", inv.get("objects", []))
        if isinstance(objects, dict):
            objects = list(objects.values())
        
        for obj in objects:
            name = obj.get("name", obj.get("logical_name", ""))
            factory = obj.get("factory_class", "")
            prim_paths = obj.get("runtime_prim_paths", obj.get("prim_paths", obj.get("source_prim_paths", [])))
            bbox = obj.get("bbox", obj.get("bounding_box", {}))
            module = obj.get("module_path", obj.get("module", ""))
            
            obj_type = extract_factory_class(factory or name)
            center = get_bbox_center(bbox) if bbox else None
            size = get_bbox_size(bbox) if bbox else None
            
            scene_info["objects"].append({
                "name": name,
                "type": obj_type,
                "factory": factory,
                "module": module,
                "prim_paths": prim_paths,
                "center": [round(c, 2) for c in center] if center else None,
                "size": [round(s, 2) for s in size] if size else None,
                "bbox_raw": bbox,
            })
    
    catalog[scene_name] = scene_info

# Print summary
print("=" * 80)
print(f"BENCHMARK SCENE CATALOG — {len(catalog)} scenes")
print("=" * 80)

# Collect all object types across scenes
all_types = defaultdict(list)

for sname, sinfo in sorted(catalog.items()):
    room_type = "living" if "living" in sname else "dining"
    print(f"\n{'='*60}")
    print(f"Scene: {sname} ({room_type} room)")
    print(f"  Humans: {len(sinfo['humans'])}")
    for h in sinfo["humans"]:
        pos = h["position"]
        pos_str = f"({pos[0]:.1f}, {pos[1]:.1f})" if len(pos) >= 2 else str(pos)
        print(f"    - {h['name']} at {pos_str}, {h['trajectory_frames']} keyframes")
    
    print(f"  Objects: {len(sinfo['objects'])}")
    for obj in sorted(sinfo["objects"], key=lambda x: x["type"]):
        center_str = f"({obj['center'][0]:.1f}, {obj['center'][1]:.1f})" if obj["center"] else "?"
        size_str = ""
        if obj["size"]:
            size_str = f" [{obj['size'][0]:.1f}x{obj['size'][1]:.1f}x{obj['size'][2]:.1f}m]"
        print(f"    - {obj['type']:30s} center={center_str:15s}{size_str}")
        all_types[obj["type"]].append(sname)

print(f"\n{'='*60}")
print("OBJECT TYPE FREQUENCY (across all scenes)")
print("="*60)
for otype, scenes_with in sorted(all_types.items(), key=lambda x: -len(x[1])):
    print(f"  {otype:30s} appears in {len(scenes_with):2d}/{len(catalog)} scenes")

# Save full catalog
out_path = os.path.join(BASE, "scene_catalog.json")
json.dump(catalog, open(out_path, "w"), indent=2)
print(f"\nFull catalog saved to: {out_path}")
