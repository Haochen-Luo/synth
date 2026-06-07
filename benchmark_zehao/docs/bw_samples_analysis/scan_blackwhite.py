#!/usr/bin/env python3
"""Scan FPV frames for pure-black / pure-white(overexposed) frames.
Reads the small *_thumb.jpg for speed. Reports per-task anomaly frame indices."""
import os, sys, glob, json
import numpy as np
from PIL import Image

ROOT = sys.argv[1] if len(sys.argv) > 1 else \
    "/home/liuqi/hc/synth/benchmark_zehao/results/eval_30B_333_v2"

BLACK_T = 12.0    # mean luminance below -> black
WHITE_T = 243.0   # mean luminance above -> white/overexposed
# also flag "mostly extreme" frames where a large fraction of pixels are at the rails
FRAC_T = 0.85

def frame_stat(path):
    try:
        im = Image.open(path).convert("L")
        a = np.asarray(im, dtype=np.float32)
    except Exception:
        return None
    m = float(a.mean())
    fb = float((a < 8).mean())    # fraction near-black
    fw = float((a > 248).mean())  # fraction near-white
    return m, fb, fw

def classify(m, fb, fw):
    if m < BLACK_T or fb > FRAC_T:
        return "BLACK"
    if m > WHITE_T or fw > FRAC_T:
        return "WHITE"
    return None

results = {}
task_dirs = sorted(glob.glob(os.path.join(ROOT, "*", "*")))
for td in task_dirs:
    fpv = os.path.join(td, "vlm_nav_frames_fpv")
    if not os.path.isdir(fpv):
        continue
    # prefer thumbs for speed
    thumbs = sorted(glob.glob(os.path.join(fpv, "rgb_*_thumb.jpg")))
    use_thumb = bool(thumbs)
    frames = thumbs if use_thumb else sorted(glob.glob(os.path.join(fpv, "rgb_*.png")))
    anomalies = []
    n = 0
    for f in frames:
        base = os.path.basename(f)
        # extract NNNN
        idx = base.replace("_thumb", "").replace("rgb_", "").replace(".jpg", "").replace(".png", "")
        st = frame_stat(f)
        if st is None:
            continue
        n += 1
        cls = classify(*st)
        if cls:
            anomalies.append({"idx": idx, "cls": cls, "mean": round(st[0], 1),
                              "fb": round(st[1], 2), "fw": round(st[2], 2)})
    if anomalies:
        rel = os.path.relpath(td, ROOT)
        results[rel] = {"n_frames": n, "n_anom": len(anomalies), "anomalies": anomalies}

# Summary
total_tasks = len([d for d in task_dirs if os.path.isdir(os.path.join(d, "vlm_nav_frames_fpv"))])
print(f"=== Scanned {total_tasks} tasks, {len(results)} have anomalies ===")
n_black = sum(1 for r in results.values() if any(a['cls']=='BLACK' for a in r['anomalies']))
n_white = sum(1 for r in results.values() if any(a['cls']=='WHITE' for a in r['anomalies']))
print(f"tasks with BLACK frames: {n_black}, with WHITE frames: {n_white}")
for rel, r in sorted(results.items()):
    cls_set = sorted(set(a['cls'] for a in r['anomalies']))
    idxs = ",".join(a['idx']+a['cls'][0] for a in r['anomalies'][:30])
    print(f"{rel}  [{r['n_anom']}/{r['n_frames']}] {cls_set}: {idxs}")

with open("/tmp/bw_scan_result.json", "w") as fh:
    json.dump(results, fh, indent=1)
print("\nwrote /tmp/bw_scan_result.json")
