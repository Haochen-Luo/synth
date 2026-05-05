"""Probe dancer bbox AFTER applying the benchmark's exact xform (scale + translate).
This simulates what the benchmark does to find the correct Z offset."""
from isaacsim import SimulationApp
app = SimulationApp({"headless": True})

import omni.usd, math, json
from omni.isaac.core.utils.stage import open_stage, is_stage_loading
from pxr import Gf, UsdGeom, Usd

scene_dir = "/home/qi/hc/Puppeteer/zehao_new_folder/phy_env/case11_multi_surface_turn_right_full_physics_scene"
usd_path = scene_dir + "/compiled_stages/case11_multi_surface_turn_right_full_physics.compiled.usda"
spec_path = scene_dir + "/compiled_specs/case11_multi_surface_turn_right_full_physics.compiled.spec.json"
spec = json.load(open(spec_path))
active_humans = spec.get("active_humans", [])

open_stage(usd_path)
while is_stage_loading():
    app.update()
stage = omni.usd.get_context().get_stage()
for _ in range(10):
    app.update()

# Get runner binding
runner_binding = {}
dancer_binding = {}
for ah in active_humans:
    if "run" in ah.get("name", ""):
        runner_binding = ah.get("animation_binding", {})
    if "dance" in ah.get("name", ""):
        dancer_binding = ah.get("animation_binding", {})

runner_scale = runner_binding.get("scale_xyz", [0.53, 0.53, 0.53])
runner_root_offset = runner_binding.get("root_offset_m", [0, 0, 0.53])

results = []

# === Test dancer at various Z values ===
dancer_prim = stage.GetPrimAtPath("/World/Humans/obj_2_dance_anim_2")
if dancer_prim and dancer_prim.IsValid():
    d_pos = dancer_binding.get("placement_location_m", [2.34, 2.13, 1.18])
    d_rot = dancer_binding.get("rotation_deg_xyz", [0, 0, 132.7])
    d_root_off = dancer_binding.get("root_offset_m", [0, 0, 1.04])
    d_orig_scale = dancer_binding.get("scale_xyz", [1.0, 1.0, 1.0])
    
    # Apply exactly what benchmark does: ClearXformOpOrder, then add translate+orient+scale
    d_xf = UsdGeom.Xformable(dancer_prim)
    try: d_xf.ClearXformOpOrder()
    except: pass
    d_trans = d_xf.AddTranslateOp()
    d_orient = d_xf.AddOrientOp()
    d_scale = d_xf.AddScaleOp()
    
    d_yaw_rad = math.radians(d_rot[2])
    d_orient.Set(Gf.Quatf(math.cos(d_yaw_rad/2), 0, 0, math.sin(d_yaw_rad/2)))
    d_scale.Set(Gf.Vec3d(runner_scale[0], runner_scale[1], runner_scale[2]))
    
    # Test Z values
    test_z_values = [
        ("runner_root_offset", runner_root_offset[2]),
        ("proportional", d_root_off[2] * (runner_scale[2] / d_orig_scale[2])),
        ("0.60", 0.60),
        ("0.65", 0.65),
        ("0.70", 0.70),
        ("0.75", 0.75),
        ("0.80", 0.80),
        ("0.85", 0.85),
        ("0.90", 0.90),
    ]
    
    for label, z_val in test_z_values:
        d_trans.Set(Gf.Vec3d(d_pos[0], d_pos[1], z_val))
        app.update()
        
        bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"])
        bbox_cache.Clear()
        bbox = bbox_cache.ComputeWorldBound(dancer_prim)
        rng = bbox.ComputeAlignedRange()
        feet_z = rng.GetMin()[2]
        
        results.append({
            "label": label,
            "set_z": round(z_val, 4),
            "feet_z": round(feet_z, 4),
            "feet_status": "ON FLOOR" if abs(feet_z) < 0.02 else ("SINKING" if feet_z < -0.02 else "FLOATING"),
        })

# === Also check runner ===
runner_prim = stage.GetPrimAtPath("/World/Humans/obj_1_run_anim_1")
if runner_prim and runner_prim.IsValid():
    r1_xf = UsdGeom.Xformable(runner_prim)
    try: r1_xf.ClearXformOpOrder()
    except: pass
    r1_trans = r1_xf.AddTranslateOp()
    r1_orient = r1_xf.AddOrientOp()
    r1_scale = r1_xf.AddScaleOp()
    r1_scale.Set(Gf.Vec3d(runner_scale[0], runner_scale[1], runner_scale[2]))
    r1_orient.Set(Gf.Quatf(1, 0, 0, 0))
    r1_trans.Set(Gf.Vec3d(5.0, 5.0, runner_root_offset[2]))
    app.update()
    
    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"])
    bbox_cache.Clear()
    bbox = bbox_cache.ComputeWorldBound(runner_prim)
    rng = bbox.ComputeAlignedRange()
    results.append({
        "label": "RUNNER_at_root_offset",
        "set_z": round(runner_root_offset[2], 4),
        "feet_z": round(rng.GetMin()[2], 4),
        "feet_status": "ON FLOOR" if abs(rng.GetMin()[2]) < 0.02 else ("SINKING" if rng.GetMin()[2] < -0.02 else "FLOATING"),
    })

out_path = "/home/qi/hc/Puppeteer/zehao_task/dancer_bbox_results.json"
with open(out_path, "w") as f:
    json.dump(results, f, indent=2)

app.close()
