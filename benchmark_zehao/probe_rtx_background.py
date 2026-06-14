#!/usr/bin/env python3
"""Probe the RTX background/fallback color that produces pure-blue (0,0,255)
escape frames. Loads case18 like bench_runner (PathTracing, spp=16), then dumps
every carb setting under /rtx that could be the background/clear/environment
color. Read-only diagnosis — finds the setting key + current value to fix.

Run: /isaac-sim/python.sh probe_rtx_background.py
Out: /tmp/rtx_probe.txt
"""
import os, json

OUT = open("/tmp/rtx_probe.txt", "w")
def emit(s):
    OUT.write(str(s) + "\n"); OUT.flush(); print(s, flush=True)

SCENE = ("/home/liuqi/hc/synth/benchmark_zehao/full_scenarios_extracted/"
         "native_case18_dining_push_lift_full_physics_scene/compiled_stages/"
         "native_case18_dining_push_lift_full_physics.compiled.usda")

from isaacsim import SimulationApp
app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})
import omni.kit.commands
from omni.isaac.core.utils.stage import open_stage, is_stage_loading
import carb

settings = carb.settings.get_settings()

# Replicate bench_runner's render config
open_stage(SCENE)
n = 0
while is_stage_loading() and n < 3000:
    app.update(); n += 1
for _ in range(60):
    app.update()
omni.kit.commands.execute("ChangeSetting", path="/rtx/rendermode", value="PathTracing")
omni.kit.commands.execute("ChangeSetting", path="/rtx/pathtracing/spp", value=16)
for _ in range(30):
    app.update()

emit("=== candidate RTX background / environment / fallback settings ===")
CANDIDATES = [
    "/rtx/rendermode",
    "/rtx/pathtracing/spp",
    # background / clear color candidates
    "/rtx/sceneDb/ambientLightColor",
    "/rtx/scene/common/backgroundColor",
    "/rtx/background/color",
    "/rtx/background/source",
    "/rtx/backgroundColor",
    "/rtx/pathtracing/clampSpp",
    "/rtx/domeLight/upperLowerStrategy",
    "/rtx/domeLight/enabled",
    "/rtx/environment/backgroundColor",
    "/rtx/post/backgroundZeroAlpha/backgroundDefaultColor",
    "/rtx/post/backgroundZeroAlpha/enabled",
    "/rtx/pathtracing/fireflyFilter/maxIntensityPerSample",
    "/rtx/transient/dlssg/enabled",
    "/app/viewport/defaults/fillViewport",
    "/rtx/sceneDb/skyEnabled",
    "/persistent/rtx/background/color",
]
for k in CANDIDATES:
    try:
        v = settings.get(k)
    except Exception as e:
        v = f"<err {e}>"
    emit(f"  {k} = {v}")

emit("\n=== dump ALL settings whose key contains background/clear/ambient/sky/environment ===")
# carb settings has no easy 'list all' in py; try common roots via get_settings_dictionary
try:
    import omni.kit.app
    d = settings.get_settings_dictionary("/rtx")
    def walk(node, prefix="/rtx"):
        if isinstance(node, dict):
            for kk, vv in node.items():
                walk(vv, prefix + "/" + kk)
        else:
            low = prefix.lower()
            if any(t in low for t in ("background", "clear", "ambient", "sky",
                                       "environment", "fallback", "default")):
                emit(f"  {prefix} = {node}")
    walk(d)
except Exception as e:
    emit(f"  (dict walk failed: {e})")

app.close()
