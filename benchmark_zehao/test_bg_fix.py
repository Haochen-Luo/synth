#!/usr/bin/env python3
"""Decisive test using the REAL blue-frame camera pose (from case18 nav_history:
step100 xy=(-0.10,8.58) yaw=62.5 pitch=0, and step80 xy=(-0.80,6.27) yaw=77.5).
Reproduces FPV exactly via bench_runner's cam_quat, renders OFF (current=blue?)
vs candidate fixes, reports dominant pixel.

Run: /isaac-sim/python.sh test_bg_fix.py ; Out: /tmp/bg_fix.txt + /tmp/bg_fix/*.png
"""
import os, math
import numpy as np
from PIL import Image

OUT = open("/tmp/bg_fix.txt", "w")
def emit(s):
    OUT.write(str(s) + "\n"); OUT.flush(); print(s, flush=True)

os.makedirs("/tmp/bg_fix", exist_ok=True)
SCENE = ("/home/liuqi/hc/synth/benchmark_zehao/full_scenarios_extracted/"
         "native_case18_dining_push_lift_full_physics_scene/compiled_stages/"
         "native_case18_dining_push_lift_full_physics.compiled.usda")
EYE_H = 1.58
POSES = [("step100", -0.10, 8.575, 62.5), ("step80", -0.802, 6.265, 77.5)]

from isaacsim import SimulationApp
app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})
import omni.kit.commands, omni.replicator.core as rep, carb, omni.usd
from omni.isaac.core.utils.stage import open_stage, is_stage_loading
from pxr import UsdGeom, Gf

settings = carb.settings.get_settings()
open_stage(SCENE)
n = 0
while is_stage_loading() and n < 3000:
    app.update(); n += 1
for _ in range(60):
    app.update()
omni.kit.commands.execute("ChangeSetting", path="/rtx/rendermode", value="PathTracing")
omni.kit.commands.execute("ChangeSetting", path="/rtx/pathtracing/spp", value=16)
stage = omni.usd.get_context().get_stage()


def cam_quat(yaw_deg, pitch_deg=0.0):
    yr, pr = math.radians(yaw_deg), math.radians(pitch_deg)
    eye = Gf.Vec3d(0, 0, 0)
    tgt = Gf.Vec3d(math.cos(yr)*math.cos(pr), math.sin(yr)*math.cos(pr), math.sin(pr))
    mat = Gf.Matrix4d().SetLookAt(eye, tgt, Gf.Vec3d(0, 0, 1))
    qd = mat.GetInverse().ExtractRotation().GetQuat()
    return Gf.Quatf(qd.GetReal(), *qd.GetImaginary())


cam = UsdGeom.Camera.Define(stage, "/World/NavCamera")
cam.CreateFocalLengthAttr().Set(17.0)
cam.CreateHorizontalApertureAttr().Set(34.0)
cam.CreateClippingRangeAttr().Set(Gf.Vec2f(0.01, 10000.0))
rp = rep.create.render_product("/World/NavCamera", (256, 256))
wr = rep.WriterRegistry.get("BasicWriter")


def place(x, y, yaw):
    prim = stage.GetPrimAtPath("/World/NavCamera")
    xf = UsdGeom.Xformable(prim); xf.ClearXformOpOrder()
    xf.AddTranslateOp().Set(Gf.Vec3d(x, y, EYE_H))
    xf.AddOrientOp().Set(cam_quat(yaw, 0.0))


def render(tag):
    scratch = f"/tmp/bg_fix/_s_{tag}"
    os.makedirs(scratch, exist_ok=True)
    wr.initialize(output_dir=scratch, rgb=True); wr.attach([rp])
    for _ in range(15):
        app.update()
    rep.orchestrator.step()
    for _ in range(4):
        app.update()
    wr.detach()
    pngs = sorted(f for f in os.listdir(scratch) if f.endswith(".png"))
    if not pngs:
        emit(f"  {tag}: NO PNG"); return
    im = np.asarray(Image.open(os.path.join(scratch, pngs[-1])).convert("RGB"))
    Image.fromarray(im).save(f"/tmp/bg_fix/{tag}.png")
    flat = im.reshape(-1, 3)
    u, c = np.unique(flat, axis=0, return_counts=True)
    dom = u[c.argmax()]; frac = c.max()/len(flat)
    emit(f"  {tag}: mean=({flat[:,0].mean():.0f},{flat[:,1].mean():.0f},{flat[:,2].mean():.0f}) "
         f"dom=({dom[0]},{dom[1]},{dom[2]}) {frac*100:.0f}%")


def set_cfg(name):
    if name == "OFF":
        settings.set_bool("/rtx/post/backgroundZeroAlpha/enabled", False)
    elif name == "ZEROALPHA_ON":
        settings.set_bool("/rtx/post/backgroundZeroAlpha/enabled", True)
        settings.set("/rtx/post/backgroundZeroAlpha/backgroundDefaultColor", [0.0, 0.0, 0.0])


for cfg in ["OFF", "ZEROALPHA_ON"]:
    emit(f"=== cfg={cfg} ===")
    set_cfg(cfg)
    for (tag, x, y, yaw) in POSES:
        place(x, y, yaw)
        render(f"{cfg}_{tag}")

app.close()
