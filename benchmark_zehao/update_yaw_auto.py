#!/usr/bin/env python3
import os
import json
import math

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_JSON = os.path.join(SCRIPT_DIR, "benchmark_tasks_0527fix.json")

def get_probed_filename(scene_dir):
    # e.g., native_case01_living_follow_full_physics_scene -> probed_case01_living_follow.json
    name = scene_dir.replace("native_", "probed_").replace("_full_physics_scene", "")
    return os.path.join(SCRIPT_DIR, f"{name}.json")

def get_target_center(probed_path, target_factory):
    if not os.path.exists(probed_path):
        return None
    with open(probed_path) as f:
        data = json.load(f)
    
    # SimpleBookcaseFactory -> SimpleBookcase
    clean_factory = target_factory.replace("Factory", "")
    
    # Find matching prims
    candidates = []
    for p in data.get("prims", []):
        name = p.get("name", "")
        path = p.get("path", "")
        # Check if the clean_factory name is in name/path
        if clean_factory in name or clean_factory in path:
            if p.get("center") is not None:
                candidates.append(p)
    
    if not candidates:
        return None
        
    # Pick the tallest candidate (by z size) as full_task_gen.py does
    tallest = max(candidates, key=lambda c: c.get("size", [0,0,0])[2])
    return tallest["center"]

def main():
    with open(TASKS_JSON) as f:
        data = json.load(f)
        
    print("Auto-calculating agent spawn yaw based on target positions...")
    
    updated_count = 0
    for t in data["tasks"]:
        tid = t["id"]
        level = t.get("level", "L1")
        scene_dir = t["scene_dir"]
        agent_start = t["agent_start"]
        
        # We only care about the target of the first phase
        if not t.get("phases"):
            continue
        first_phase = t["phases"][0]
        target_object = first_phase["target_object"] # e.g. "TVStandFactory"
        
        probed_path = get_probed_filename(scene_dir)
        target_center = get_target_center(probed_path, target_object)
        
        if target_center is None:
            print(f"  WARNING: target center not found for {tid} (object={target_object})")
            continue
            
        tx, ty = target_center[0], target_center[1]
        ax, ay = agent_start[0], agent_start[1]
        
        dx = tx - ax
        dy = ty - ay
        
        # Calculate yaw to target
        yaw_to = math.degrees(math.atan2(dy, dx))
        
        # Visible levels (L1, L3) face the target
        # Hidden levels (L2, L4) face 180 degrees away
        if level in ["L1", "L3"]:
            new_yaw = yaw_to
        else:
            new_yaw = yaw_to + 180.0
            
        # Normalize to [-180, 180]
        new_yaw = ((new_yaw + 180) % 360) - 180
        new_yaw = round(new_yaw, 1)
        
        old_yaw = t.get("agent_yaw")
        if old_yaw != new_yaw:
            t["agent_yaw"] = new_yaw
            print(f"  {tid} ({level}): target={target_object} at [{tx:.2f}, {ty:.2f}] | start=[{ax:.2f}, {ay:.2f}] | yaw: {old_yaw} -> {new_yaw}")
            updated_count += 1
            
    if updated_count > 0:
        with open(TASKS_JSON, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Successfully updated yaw for {updated_count} tasks in benchmark_tasks_0527fix.json")
    else:
        print("All yaws are already up-to-date and match the formulas!")

if __name__ == "__main__":
    main()
