#!/usr/bin/env python3
"""On HK: for selected anomaly tasks, copy FPV+bird frames covering each
anomaly segment (with 2-frame context before/after) into a staging dir,
organized by case, with a manifest. Keeps transfer small."""
import os, json, shutil, glob
import numpy as np
from PIL import Image

ROOT = "/home/liuqi/hc/synth/benchmark_zehao/results/eval_30B_333_v2"
OUT = "/home/liuqi/hc/synth/benchmark_zehao/bw_samples"
CTX = 2  # context frames before/after each anomaly segment

# Selected tasks (rel paths under ROOT) + short reason tag
TASKS = {
 "L2/case061_official_solo_run-L2_20260607_013621":      "black_longstuck_lowcollision",
 "L4/case069_official_solo_run-L4_20260607_021617":      "black_birdview_clip_userflag",
 "L2/case03_scene_gen_v5_test_input_case2-L2_20260607_001505": "white_from_spawn_filllight",
 "L4/case37_mask_dining_04_gallery_table-L4_20260607_055854":  "blackwhite_mixed_severe",
 "L2/case022_official_solo_run-L2_20260606_175822":      "black_single_transient",
 "L2/case11_bedroom_lift-L2_20260607_033200":            "white_midrun_segment",
 "L2/case060_official_run_dance-L2_20260606_191900":     "black_segment_dance",
 "L4/case12_text_bedroom_04_minimal_study-L4_20260606_214457": "black_long_segment",
}

BLACK_T, WHITE_T, FRAC_T = 12.0, 243.0, 0.85
def frame_cls(path):
    try:
        a = np.asarray(Image.open(path).convert("L"), dtype=np.float32)
    except Exception:
        return None, None
    m = float(a.mean()); fb=float((a<8).mean()); fw=float((a>248).mean())
    if m < BLACK_T or fb > FRAC_T: return "BLACK", m
    if m > WHITE_T or fw > FRAC_T: return "WHITE", m
    return None, m

def segments(idxs):
    """group consecutive ints into [(start,end),...]"""
    if not idxs: return []
    idxs=sorted(idxs); segs=[[idxs[0],idxs[0]]]
    for i in idxs[1:]:
        if i==segs[-1][1]+1: segs[-1][1]=i
        else: segs.append([i,i])
    return segs

os.umask(0)
if os.path.exists(OUT): shutil.rmtree(OUT)
os.makedirs(OUT)
manifest={}
for rel, tag in TASKS.items():
    td=os.path.join(ROOT, rel)
    fpv_dir=os.path.join(td,"vlm_nav_frames_fpv")
    bird_dir=os.path.join(td,"vlm_nav_frames_bird")
    thumbs=sorted(glob.glob(os.path.join(fpv_dir,"rgb_*_thumb.jpg")))
    anom=[]
    cls_by_idx={}
    for t in thumbs:
        n=int(os.path.basename(t).replace("_thumb.jpg","").replace("rgb_",""))
        c,m=frame_cls(t)
        if c: anom.append(n); cls_by_idx[n]=c
    segs=segments(anom)
    maxn=len(thumbs)-1
    case=os.path.basename(rel)
    case_out=os.path.join(OUT, f"{tag}__{case}")
    os.makedirs(os.path.join(case_out,"fpv"))
    os.makedirs(os.path.join(case_out,"bird"))
    picked=set()
    seg_info=[]
    # to limit size: if a segment is long, sample its interior (every 4th) but
    # always keep both boundaries + context
    for s,e in segs:
        rng=set(range(max(0,s-CTX), min(maxn,e+CTX)+1))
        if e-s>10:
            interior=set(range(s,e+1,4))
            keep={x for x in rng if (x<s-1 or x>e+1 or x in interior or x in (s,e,s-1,s-2,e+1,e+2))}
            rng=keep
        picked|=rng
        seg_info.append({"start":s,"end":e,"len":e-s+1,
                         "cls":sorted(set(cls_by_idx.get(i) for i in range(s,e+1)))})
    copied=[]
    for n in sorted(picked):
        if n<0 or n>maxn: continue
        f_src=os.path.join(fpv_dir,f"rgb_{n:04d}.png")
        b_src=os.path.join(bird_dir,f"rgb_{n:04d}.png")
        lab = cls_by_idx.get(n,"ok")
        if os.path.exists(f_src):
            shutil.copy(f_src, os.path.join(case_out,"fpv",f"{n:04d}_{lab}.png"))
        if os.path.exists(b_src):
            shutil.copy(b_src, os.path.join(case_out,"bird",f"{n:04d}_{lab}.png"))
        copied.append(n)
    # results summary
    try:
        r=json.load(open(os.path.join(td,"results.json")))["metrics"]
        summ={k:r.get(k) for k in("instruction","success","goal_distance_m","steps_used","vlm_calls","collision_count","static_collision_count","timeout")}
    except Exception: summ={}
    manifest[case]={"tag":tag,"segments":seg_info,"n_anom":len(anom),
                    "n_frames":len(thumbs),"frames_copied":copied,"metrics":summ}
    print(f"{tag:35s} {case}: {len(segs)} segs, {len(anom)} anom frames, copied {len(copied)} frame-pairs")

json.dump(manifest, open(os.path.join(OUT,"MANIFEST.json"),"w"), indent=1)
# also a readable txt
with open(os.path.join(OUT,"MANIFEST.txt"),"w") as fh:
    for case,m in manifest.items():
        fh.write(f"\n### {case}  [{m['tag']}]\n")
        mt=m['metrics']
        fh.write(f"  instruction: {mt.get('instruction')}\n")
        fh.write(f"  success={mt.get('success')} goalDist={mt.get('goal_distance_m')} steps={mt.get('steps_used')} vlm_calls={mt.get('vlm_calls')} timeout={mt.get('timeout')}\n")
        fh.write(f"  collisions={mt.get('collision_count')} (static={mt.get('static_collision_count')})\n")
        fh.write(f"  anom_frames={m['n_anom']}/{m['n_frames']}\n")
        for s in m['segments']:
            fh.write(f"    seg {s['start']:04d}-{s['end']:04d} (len {s['len']}) {s['cls']}\n")
print("\nstaging dir:", OUT)
du=sum(os.path.getsize(os.path.join(dp,f)) for dp,_,fs in os.walk(OUT) for f in fs)
print(f"total size: {du/1e6:.1f} MB")
