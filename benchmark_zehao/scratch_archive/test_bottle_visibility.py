"""Diagnose WHY the case003 bottle (Obj_26523_BottleFactory) is not visible in frame 0.
Checks: active? visible? has renderable Mesh children w/ points? render-purpose bbox.
Then PhysX-raycasts from the spawn camera toward the bottle to test OCCLUSION.
Dumps JSON sidecar (Isaac swallows stdout).

Run: docker exec vlm-jupyter-180 /isaac-sim/python.sh .../scratch_archive/test_bottle_visibility.py
"""
import sys, os, json, math
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

from isaacsim import SimulationApp
sim_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})
import omni.usd, omni.timeline, omni.physx
from omni.isaac.core.utils.stage import is_stage_loading
from pxr import UsdGeom, Usd

from bench_helpers import discover_scene_files, resolve_target

SCENE = os.path.join(os.path.dirname(SCRIPT_DIR),
    "full_scenarios_extracted/native_case003_official_solo_run_full_physics_scene")
PHASE = {"name": "pick", "target_object": "BottleFactory",
         "target_prim": "/World/Env/BottleFactory_9203792__spawn_asset_6999656_",
         "action": "PICK_UP"}
CAM = (10.91, 9.91, 1.58)  # spawn camera (agent_start + EYE_H)

omni.usd.get_context().open_stage(discover_scene_files(SCENE)["stage"])
while is_stage_loading():
    sim_app.update()
stage = omni.usd.get_context().get_stage()

res = resolve_target(stage, PHASE)
out = {"resolved": res}
bp = res["prim_path"]
prim = stage.GetPrimAtPath(bp)

# ── geometry / visibility inspection ──
out["active"] = bool(prim.IsActive())
out["visibility"] = str(UsdGeom.Imageable(prim).ComputeVisibility(Usd.TimeCode.Default()))
meshes = []
for d in Usd.PrimRange(prim):
    if d.GetTypeName() == "Mesh":
        m = UsdGeom.Mesh(d)
        pts = m.GetPointsAttr().Get()
        meshes.append({"path": d.GetPath().pathString,
                       "npoints": (len(pts) if pts else 0),
                       "active": bool(d.IsActive()),
                       "visible": str(UsdGeom.Imageable(d).ComputeVisibility(Usd.TimeCode.Default()))})
out["meshes"] = meshes
out["n_mesh"] = len(meshes)
out["total_points"] = sum(m["npoints"] for m in meshes)
# render-purpose bbox
bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
rb = bc.ComputeWorldBound(prim).GetRange()
out["render_bbox_empty"] = bool(rb.IsEmpty())
if not rb.IsEmpty():
    out["render_bbox_min"] = [round(float(v), 3) for v in rb.GetMin()]
    out["render_bbox_max"] = [round(float(v), 3) for v in rb.GetMax()]

# ── occlusion: raycast from camera toward bottle ──
timeline = omni.timeline.get_timeline_interface()
timeline.play()
for _ in range(20):
    sim_app.update()
timeline.pause()
q = omni.physx.get_physx_scene_query_interface()
tgt = res["center"]
d = (tgt[0]-CAM[0], tgt[1]-CAM[1], tgt[2]-CAM[2])
dist = math.sqrt(sum(c*c for c in d))
dirv = (d[0]/dist, d[1]/dist, d[2]/dist)
hit = q.raycast_closest(CAM, dirv, dist + 1.0)
out["cam_to_bottle_dist"] = round(dist, 3)
if hit and hit.get("hit"):
    out["ray_hit"] = {"collision": hit.get("collision") or hit.get("rigidBody"),
                      "distance": round(hit.get("distance", -1), 3)}
    out["occluded"] = hit.get("distance", 1e9) < dist - 0.15
else:
    out["ray_hit"] = None
    out["occluded"] = False  # nothing in the way (bottle may have no collider)

with open(os.path.join(SCRIPT_DIR, "_bottle_vis_result.json"), "w") as f:
    json.dump(out, f, indent=2)
sim_app.close()
