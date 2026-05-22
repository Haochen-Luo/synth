"""Scene Prober: Load each scene in Isaac Sim, extract all prim paths + bboxes.
Outputs a JSON catalog of real prim paths and world-space positions.
Usage: SCENE_DIR=/path/to/scene /isaac-sim/python.sh scene_prober.py
"""
import sys, os, json, glob

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bench_helpers import discover_scene_files

scene_dir = os.environ.get("SCENE_DIR", "")
if not scene_dir:
    print("ERROR: Set SCENE_DIR env var"); sys.exit(1)

try:
    from isaacsim import SimulationApp
    sim_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})
    import omni.usd
    from omni.isaac.core.utils.stage import open_stage, is_stage_loading
    from pxr import Gf, UsdGeom

    sf = discover_scene_files(scene_dir)
    assert sf["stage"], f"No stage in {scene_dir}"
    open_stage(sf["stage"])
    while is_stage_loading(): sim_app.update()
    stage = omni.usd.get_context().get_stage()

    # Keywords for furniture we care about
    FURNITURE_KEYS = [
        "Sofa", "CoffeeTable", "SimpleBookcase", "LargeShelf", "CellShelf",
        "TVStand", "TV", "Monitor", "SimpleDesk", "SingleCabinet",
        "KitchenCabinet", "TableDining", "Chair", "DeskLamp", "Mirror",
        "Rug", "LargePlantContainer", "PlantContainer", "BookStack",
        "BookColumn", "SupportBookStack", "Cup", "Plate", "Wineglass",
        "Bowl", "Pot", "Fork", "Knife", "Pan", "Chopsticks",
        "NatureShelfTrinkets", "door", "window",
    ]

    results = {"scene_dir": os.path.basename(scene_dir), "prims": [], "room_bounds": None}

    # ── Room structure: the REAL floor bbox defines the walkable room extent.
    # Furniture does not fill the room, so a furniture-center range is NOT a
    # valid room boundary — task generators must use this floor bbox instead.
    def _world_bbox(prim):
        try:
            r = UsdGeom.Imageable(prim).ComputeWorldBound(0, "default").GetRange()
            if r.IsEmpty():
                return None
            mn, mx = r.GetMin(), r.GetMax()
            return ([round(float(mn[i]), 3) for i in range(3)],
                    [round(float(mx[i]), 3) for i in range(3)])
        except Exception:
            return None

    for prim in stage.Traverse():
        nm = prim.GetName().lower()
        if "floor" in nm and "ground" not in nm:
            bb = _world_bbox(prim)
            if bb:
                mn, mx = bb
                results["room_bounds"] = {
                    "floor_prim": str(prim.GetPath()),
                    "x": [mn[0], mx[0]], "y": [mn[1], mx[1]], "z": [mn[2], mx[2]],
                }
                print(f"  ROOM FLOOR: {prim.GetPath()}  x[{mn[0]},{mx[0]}]  y[{mn[1]},{mx[1]}]")
                break

    for prim in stage.Traverse():
        path = str(prim.GetPath())
        if not path.startswith("/World/"): continue
        name = path.split("/")[-1]
        depth = len(path.split("/"))
        if depth > 5: continue  # skip deep children

        # Check if it matches any furniture key
        matched_key = None
        for k in FURNITURE_KEYS:
            if k in name:
                matched_key = k; break
        if not matched_key: continue

        # Get bbox
        center, size = None, None
        try:
            imageable = UsdGeom.Imageable(prim)
            bbox = imageable.ComputeWorldBound(0, "default")
            r = bbox.GetRange()
            if not r.IsEmpty():
                mn, mx = r.GetMin(), r.GetMax()
                center = [round(float((mn[i]+mx[i])/2), 3) for i in range(3)]
                size = [round(float(mx[i]-mn[i]), 3) for i in range(3)]
        except: pass

        results["prims"].append({
            "path": path,
            "name": name,
            "category": matched_key,
            "center": center,
            "size": size,
        })
        if center:
            print(f"  {matched_key:25s}  {name:50s}  center=({center[0]:6.2f},{center[1]:6.2f},{center[2]:5.2f})  size={size}")
        else:
            print(f"  {matched_key:25s}  {name:50s}  (no bbox)")

    # Save
    out_name = os.path.basename(scene_dir).replace("native_","").replace("_full_physics_scene","")
    out_path = os.path.join(SCRIPT_DIR, f"probed_{out_name}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {len(results['prims'])} prims to {out_path}")

    sim_app.close()
except Exception as e:
    import traceback; traceback.print_exc()
