"""Enumerate ALL floor/wall/room prims in case01 — is it one room or several?"""
import sys, traceback
sys.path.insert(0,'/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao')
from bench_helpers import discover_scene_files
from isaacsim import SimulationApp
app=SimulationApp({"headless":True})
import omni.usd
from omni.isaac.core.utils.stage import open_stage, is_stage_loading
from pxr import UsdGeom, Usd
R="/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/_room_probe_out.txt"
def w(m):
    with open(R,"a") as f: f.write(str(m)+"\n")
open(R,"w").close()
try:
    sf=discover_scene_files('/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/native_case01_living_follow_full_physics_scene')
    open_stage(sf['stage'])
    while is_stage_loading(): app.update()
    stage=omni.usd.get_context().get_stage()
    cache=UsdGeom.BBoxCache(Usd.TimeCode.Default(),[UsdGeom.Tokens.default_,UsdGeom.Tokens.render])
    w("=== prims with floor/room/wall/living/area in name ===")
    for p in stage.Traverse():
        nm=p.GetName().lower()
        if any(k in nm for k in ['floor','room','living','area','zone','partition','mezzan']):
            try:
                r=cache.ComputeWorldBound(p).ComputeAlignedRange()
                if not r.IsEmpty():
                    mn,mx=r.GetMin(),r.GetMax()
                    w(f"  {p.GetPath()}")
                    w(f"     x[{mn[0]:.2f},{mx[0]:.2f}] y[{mn[1]:.2f},{mx[1]:.2f}] z[{mn[2]:.2f},{mx[2]:.2f}]")
            except Exception as ex:
                w(f"  {p.GetPath()} (bbox err)")
except Exception as e:
    w("ERR "+traceback.format_exc())
app.close()
