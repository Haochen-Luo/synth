"""Latency probe: render at 4 distinct camera positions, one orchestrator.step
each. Check whether frame N shows pose N (no lag) or pose N-1 (one-frame lag)."""
import sys, os, glob, time, traceback
RESULT="/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/runner_probe_result.txt"
def w(m):
    with open(RESULT,"a") as f: f.write(m+"\n")
    print(m,flush=True)
open(RESULT,"w").close()
try:
    sys.path.insert(0,"/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao")
    from bench_helpers import discover_scene_files
    from isaacsim import SimulationApp
    app=SimulationApp({"headless":True,"renderer":"RayTracedLighting"})
    import omni.usd, omni.replicator.core as rep, omni.kit.commands
    from omni.isaac.core.utils.stage import open_stage, is_stage_loading
    from pxr import Gf, UsdGeom, UsdLux
    import math, numpy as np
    from PIL import Image
    sf=discover_scene_files("/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/native_case01_living_follow_full_physics_scene")
    open_stage(sf["stage"])
    while is_stage_loading(): app.update()
    stage=omni.usd.get_context().get_stage()
    for p in stage.Traverse():
        pn=p.GetName().lower()
        if ("ceiling" in pn or "roof" in pn) and "light" not in pn and "lamp" not in pn:
            try: UsdGeom.Imageable(p).MakeInvisible()
            except: pass
    cam=UsdGeom.Camera.Define(stage,"/World/ProbeCam")
    cam.CreateFocalLengthAttr().Set(17.0); cam.CreateHorizontalApertureAttr().Set(34.0)
    for i in range(5):
        lt=UsdLux.SphereLight.Define(stage,f"/World/Lights/L{i}")
        lt.CreateIntensityAttr().Set(80000.0); lt.CreateRadiusAttr().Set(0.3)
        lxf=UsdGeom.Xformable(lt); lxf.ClearXformOpOrder()
        lxf.AddTranslateOp().Set(Gf.Vec3d(6+i,8,2.3))
    for _ in range(100): app.update()
    omni.kit.commands.execute("ChangeSetting",path="/rtx/rendermode",value="PathTracing")
    omni.kit.commands.execute("ChangeSetting",path="/rtx/pathtracing/spp",value=16)
    out="/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/_probe_out"
    os.system(f"rm -rf {out}; mkdir -p {out}")
    rp=rep.create.render_product("/World/ProbeCam",(480,270))
    wr=rep.WriterRegistry.get("BasicWriter")
    wr.initialize(output_dir=out,rgb=True); wr.attach([rp])

    def wait(n):
        t0=time.time()
        while time.time()-t0<30:
            ff=sorted(glob.glob(out+"/rgb_*.png"))
            if len(ff)>=n: return ff[-1]
            time.sleep(0.05)
        return None

    # 4 very different camera yaws — each renders a distinct wall direction.
    yaws=[0,90,180,270]
    for i,yaw in enumerate(yaws):
        cxf=UsdGeom.Xformable(cam); cxf.ClearXformOpOrder()
        cxf.AddTranslateOp().Set(Gf.Vec3d(6.3,9.0,1.5))
        yr=math.radians(yaw)
        tgt=Gf.Vec3d(6.3+math.cos(yr),9.0+math.sin(yr),1.4)
        mat=Gf.Matrix4d().SetLookAt(Gf.Vec3d(6.3,9.0,1.5),tgt,Gf.Vec3d(0,0,1))
        qd=mat.GetInverse().ExtractRotation().GetQuat()
        cxf.AddOrientOp().Set(Gf.Quatf(qd.GetReal(),*qd.GetImaginary()))
        app.update()
        rep.orchestrator.step(rt_subframes=16)
        f=wait(i+1)
        if f:
            a=np.array(Image.open(f).convert('RGB'))
            # crop left/right halves to fingerprint the view direction
            lh=a[:,:240].mean(); rh=a[:,240:].mean()
            w(f"set yaw={yaw} -> {os.path.basename(f)}  leftMean={lh:.0f} rightMean={rh:.0f} fullMean={a.mean():.0f}")
    w("DONE")
    app.close()
except Exception as e:
    w("ERR:\n"+traceback.format_exc())
