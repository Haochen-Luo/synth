#!/usr/bin/env python3
"""Generate benchmark_tasks.json from REAL probed scene data.
Uses probed_*.json files to get exact prim paths and coordinates."""
import json, os, glob, math

BASE = os.path.dirname(os.path.abspath(__file__))

# Load all probed scenes
scenes = {}
for f in sorted(glob.glob(os.path.join(BASE, "probed_*.json"))):
    data = json.load(open(f))
    sname = os.path.basename(f).replace("probed_","").replace(".json","")
    # Deduplicate: keep first occurrence of each category (unique prims)
    cats = {}
    for p in data["prims"]:
        cat = p["category"]
        if cat in ("NatureShelfTrinkets","window"): continue
        if cat not in cats and p["center"]:
            cats[cat] = p
    scenes[sname] = {"dir": "native_" + sname + "_full_physics_scene", "objects": cats}

def obj_center(scene, cat):
    o = scenes[scene]["objects"].get(cat)
    return o["center"][:2] if o else None

def has_obj(scene, cat):
    return cat in scenes[scene]["objects"]

def pick_start(scene, target_cat, facing=True):
    """Pick a start position ~4-5m from target, well inside room bounds.
    If facing=True, yaw toward target; else yaw away."""
    tc = obj_center(scene, target_cat)
    if not tc: tc = [6, 6]
    all_centers = [o["center"][:2] for o in scenes[scene]["objects"].values()]
    if not all_centers: all_centers = [[6,6]]
    # Room bounds with 2m safety margin from walls
    x_min = min(c[0] for c in all_centers)
    x_max = max(c[0] for c in all_centers)
    y_min = min(c[1] for c in all_centers)
    y_max = max(c[1] for c in all_centers)
    margin = 2.0
    safe_xmin = x_min + margin
    safe_xmax = x_max - margin
    safe_ymin = y_min + margin
    safe_ymax = y_max - margin
    if safe_xmin >= safe_xmax: safe_xmin, safe_xmax = x_min+0.5, x_max-0.5
    if safe_ymin >= safe_ymax: safe_ymin, safe_ymax = y_min+0.5, y_max-0.5
    cx, cy = (x_min+x_max)/2, (y_min+y_max)/2
    
    # Start: go opposite direction from target, but stay well inside room
    dx = tc[0] - cx
    dy = tc[1] - cy
    d = math.sqrt(dx*dx+dy*dy) or 1
    # Only go 3m from center (not all the way to edge)
    sx = cx - 3*dx/d
    sy = cy - 3*dy/d
    # Clamp to safe zone
    sx = max(safe_xmin, min(safe_xmax, sx))
    sy = max(safe_ymin, min(safe_ymax, sy))
    
    if facing:
        yaw = math.degrees(math.atan2(tc[1]-sy, tc[0]-sx))
    else:
        yaw = math.degrees(math.atan2(tc[1]-sy, tc[0]-sx)) + 180
    yaw = ((yaw + 180) % 360) - 180
    return [round(sx,1), round(sy,1)], round(yaw, 0)

tasks = []
scene_dir_map = {s: scenes[s]["dir"] for s in scenes}

for sname, sdata in sorted(scenes.items()):
    sid = sname.split("_")[0]  # "case01", "case02", etc.
    sdir = sdata["dir"]
    objs = sdata["objects"]
    room = "living" if "living" in sname else "dining"
    
    print(f"\n=== {sname} ({room}) ===")
    for cat in sorted(objs.keys()):
        o = objs[cat]
        c = o["center"]
        print(f"  {cat:25s}  ({c[0]:6.2f},{c[1]:6.2f})  {o['path'][:60]}")
    
    # ── L1: Short instruction, target visible ──
    # Pick the most prominent furniture as L1 target
    if room == "living":
        l1_target = "Sofa" if has_obj(sname,"Sofa") else "CoffeeTable" if has_obj(sname,"CoffeeTable") else "TVStand"
    else:
        l1_target = "TableDining" if has_obj(sname,"TableDining") else "Chair"
    
    if has_obj(sname, l1_target):
        tc = obj_center(sname, l1_target)
        start, yaw = pick_start(sname, l1_target, facing=True)
        l1_radius = 3.0 if l1_target in ("Sofa","TableDining") else 2.0
        l1_desc = {"Sofa":"the sofa","CoffeeTable":"the coffee table","TVStand":"the TV stand","TableDining":"the dining table","Chair":"a chair"}
        tasks.append({
            "id": f"{sid}-L1", "level": "L1", "scene_dir": sdir,
            "instruction": f"Go to {l1_desc.get(l1_target, 'the '+l1_target.lower())}.",
            "agent_start": start, "agent_yaw": yaw,
            "phases": [{"name": f"nav_{l1_target.lower()}", "target_object": l1_target+"Factory",
                        "radius": l1_radius, "action": "STOP",
                        "desc": l1_desc.get(l1_target, l1_target.lower()), "place_at": None}]
        })
        print(f"  L1: Go to {l1_target} at {tc}")

    # ── L2: Short instruction, target NOT visible (agent faces away) ──
    l2_candidates = ["SimpleBookcase","LargeShelf","DeskLamp","Mirror","SingleCabinet","KitchenCabinet","CellShelf"]
    l2_target = None
    for c in l2_candidates:
        if has_obj(sname, c):
            l2_target = c; break
    
    if l2_target:
        tc = obj_center(sname, l2_target)
        start, yaw = pick_start(sname, l2_target, facing=False)  # facing AWAY
        l2_desc = {"SimpleBookcase":"the bookshelf","LargeShelf":"the tall shelf","DeskLamp":"the desk lamp",
                   "Mirror":"the mirror","SingleCabinet":"the cabinet","KitchenCabinet":"the kitchen cabinet",
                   "CellShelf":"the shelf"}
        tasks.append({
            "id": f"{sid}-L2", "level": "L2", "scene_dir": sdir,
            "instruction": f"Go to {l2_desc.get(l2_target, 'the '+l2_target.lower())}.",
            "agent_start": start, "agent_yaw": yaw,
            "phases": [{"name": f"nav_{l2_target.lower()}", "target_object": l2_target+"Factory",
                        "radius": 2.0, "action": "STOP",
                        "desc": l2_desc.get(l2_target, l2_target.lower()), "place_at": None}]
        })
        print(f"  L2: Go to {l2_target} at {tc} (facing away)")

    # ── L3: Multi-step, target visible ──
    # Pick up book + go to furniture
    if has_obj(sname, "BookStack"):
        bs = objs["BookStack"]
        bs_center = bs["center"]
        # Place book on floor near center of room
        all_c = [o["center"][:2] for o in objs.values()]
        room_cx = sum(c[0] for c in all_c)/len(all_c)
        room_cy = sum(c[1] for c in all_c)/len(all_c)
        book_floor = [round(room_cx, 1), round(room_cy, 1), 0.15]
        
        # Second target: go to a different piece of furniture
        l3_second = None
        for c in ["Sofa","SimpleBookcase","LargeShelf","TVStand","SimpleDesk","SingleCabinet","CellShelf","TableDining"]:
            if has_obj(sname, c) and c != l1_target:
                l3_second = c; break
        
        if l3_second:
            tc2 = obj_center(sname, l3_second)
            start, yaw = pick_start(sname, "BookStack", facing=True)
            # Override start to face the book floor position
            yaw = round(math.degrees(math.atan2(book_floor[1]-start[1], book_floor[0]-start[0])), 0)
            l3_desc2 = {"Sofa":"the sofa","SimpleBookcase":"the bookshelf","LargeShelf":"the tall shelf",
                        "TVStand":"the TV stand","SimpleDesk":"the desk","SingleCabinet":"the cabinet",
                        "CellShelf":"the shelf","TableDining":"the dining table"}
            tasks.append({
                "id": f"{sid}-L3", "level": "L3", "scene_dir": sdir,
                "instruction": f"Pick up the book from the floor and bring it to {l3_desc2.get(l3_second, l3_second.lower())}.",
                "agent_start": start, "agent_yaw": yaw,
                "phases": [
                    {"name": "pick_book", "target_object": "BookStackFactory",
                     "radius": 1.5, "action": "PICK_UP",
                     "desc": "the book on the floor", "place_at": book_floor},
                    {"name": f"go_{l3_second.lower()}", "target_object": l3_second+"Factory",
                     "radius": 2.5, "action": "STOP",
                     "desc": l3_desc2.get(l3_second, l3_second.lower()), "place_at": None}
                ]
            })
            print(f"  L3: Pick up book at {book_floor[:2]} -> {l3_second} at {tc2}")

    # ── L4: Multi-step, target NOT visible ──
    # Turn on lamp + go to sofa, OR pick up book + go to hidden target
    if has_obj(sname, "DeskLamp") and has_obj(sname, l1_target):
        lamp_c = obj_center(sname, "DeskLamp")
        start, yaw = pick_start(sname, "DeskLamp", facing=True)
        l4_second = l1_target
        l4_desc2 = {"Sofa":"the sofa","CoffeeTable":"the coffee table","TVStand":"the TV stand",
                    "TableDining":"the dining table","Chair":"a chair"}
        tasks.append({
            "id": f"{sid}-L4", "level": "L4", "scene_dir": sdir,
            "instruction": f"Find the lamp and turn it on, then navigate to {l4_desc2.get(l4_second, l4_second.lower())}.",
            "agent_start": start, "agent_yaw": yaw,
            "phases": [
                {"name": "turn_on_lamp", "target_object": "DeskLampFactory",
                 "radius": 2.0, "action": "TURN_ON",
                 "desc": "the desk lamp", "place_at": None},
                {"name": f"go_{l4_second.lower()}", "target_object": l4_second+"Factory",
                 "radius": 3.0, "action": "STOP",
                 "desc": l4_desc2.get(l4_second, l4_second.lower()), "place_at": None}
            ]
        })
        print(f"  L4: Turn on lamp at {lamp_c} -> {l4_second}")
    elif has_obj(sname, "BookStack"):
        # Fallback L4: pick up book, go to hidden target
        all_c = [o["center"][:2] for o in objs.values()]
        room_cx = sum(c[0] for c in all_c)/len(all_c)
        room_cy = sum(c[1] for c in all_c)/len(all_c)
        book_floor = [round(room_cx+1, 1), round(room_cy-1, 1), 0.15]
        
        l4_target = None
        for c in ["SimpleBookcase","LargeShelf","SingleCabinet","CellShelf","KitchenCabinet","Mirror"]:
            if has_obj(sname, c) and c != (l2_target or ""):
                l4_target = c; break
        if l4_target:
            start, _ = pick_start(sname, "BookStack", facing=True)
            yaw_to_book = round(math.degrees(math.atan2(book_floor[1]-start[1], book_floor[0]-start[0])), 0)
            l4_desc = {"SimpleBookcase":"the bookshelf","LargeShelf":"the tall shelf",
                       "SingleCabinet":"the cabinet","CellShelf":"the shelf",
                       "KitchenCabinet":"the kitchen cabinet","Mirror":"the mirror"}
            tasks.append({
                "id": f"{sid}-L4", "level": "L4", "scene_dir": sdir,
                "instruction": f"Pick up the book from the floor and bring it to {l4_desc.get(l4_target, l4_target.lower())}.",
                "agent_start": start, "agent_yaw": yaw_to_book,
                "phases": [
                    {"name": "pick_book", "target_object": "BookStackFactory",
                     "radius": 1.5, "action": "PICK_UP",
                     "desc": "the book on the floor", "place_at": book_floor},
                    {"name": f"go_{l4_target.lower()}", "target_object": l4_target+"Factory",
                     "radius": 2.0, "action": "STOP",
                     "desc": l4_desc.get(l4_target, l4_target.lower()), "place_at": None}
                ]
            })
            print(f"  L4: Pick up book -> {l4_target}")

# Build final config
config = {
    "benchmark": "4DSynth-Nav",
    "version": "1.1",
    "max_steps": 150,
    "step_distance_m": 0.25,
    "turn_angle_deg": 15.0,
    "tilt_angle_deg": 5.0,
    "camera_pitch_deg": -10,
    "agent_eye_height_m": 1.58,
    "scenes_base": BASE,
    "tasks": tasks
}

out = os.path.join(BASE, "benchmark_tasks.json")
with open(out, "w") as f:
    json.dump(config, f, indent=2)

# Summary
from collections import Counter
levels = Counter(t["level"] for t in tasks)
print(f"\n{'='*60}")
print(f"Generated {len(tasks)} tasks: {dict(levels)}")
print(f"Saved to {out}")
