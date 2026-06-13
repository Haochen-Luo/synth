#!/usr/bin/env python3
"""
Per-task black-frame audit for the 4DSynth-Nav eval results.

Motivation: black/blank FPV frames are a benchmark *input* defect (the agent is
blind), so any episode with many of them is a false failure that contaminates SR.
We need a per-task, threshold-free measure of how bad each task is, plus an
automatic root-cause class, so a single hand-picked cutoff doesn't drive the
conclusion.

For every task directory under a results folder this computes:
  - fpv_black_frac : fraction of FPV decision frames with mean luminance < DARK
  - bird_black_frac: same for the bird's-eye frames
  - sync           : of the FPV-black frames, fraction where bird is ALSO black
  - first_dark_step / displacement_at_first_dark (from run.log XY trajectory)
  - cls            : root-cause class, derived from the signals above:
      RENDER   = fpv & bird go black together (sync high)        -> renderer fault
                 (output is fake; SR invalid). e.g. case06/064/075.
      CAMERA   = only fpv black, bird stays lit, blackout begins
                 after the agent has moved                       -> camera near-clip
                 through thin geometry / walked into an unlit area. e.g. case18/069/076.
      DARK     = only fpv black but low displacement / lit bird   -> spawn/area is just
                 dim (fill-light not covering).                  e.g. case055.
      OK       = below MIN_FRAC, not flagged.

No threshold is baked into the verdict: fpv_black_frac is reported as a continuous
value per task. MIN_FRAC only decides which tasks get a non-OK *class label* and is
printed so it can be changed downstream.

Outputs a CSV (one row per task) and a per-level / per-class summary to stdout.
Pure Python + numpy + PIL; no GPU, no network. Run on the node that holds the
frames (HK).

Usage:
  python blackframe_audit.py [RESULTS_ROOT] [FOLDER1 FOLDER2 ...]
    RESULTS_ROOT defaults to ./results
    FOLDERS default to the three fixed/1frame eval folders.
Env:
  DARK      luminance below this = black frame (default 8.0)
  MIN_FRAC  fpv_black_frac >= this to assign a non-OK class (default 0.10)
  OUT       output CSV path (default /tmp/blackframe_audit.csv)
"""
import csv
import glob
import math
import os
import re
import sys
from collections import Counter, defaultdict

import numpy as np
from PIL import Image

DARK = float(os.environ.get("DARK", "8.0"))
MIN_FRAC = float(os.environ.get("MIN_FRAC", "0.10"))
OUT = os.environ.get("OUT", "/tmp/blackframe_audit.csv")
SYNC_HI = 0.7   # >= this fraction of fpv-black frames also bird-black -> RENDER
MOVED_M = 1.0   # displacement at first dark >= this -> agent had walked away (CAMERA)


def lum_series(frame_dir):
    out = {}
    for p in sorted(glob.glob(os.path.join(frame_dir, "rgb_*.png"))):
        b = os.path.basename(p)
        m = re.match(r"rgb_(\d+)\.png$", b)
        if not m:
            continue
        try:
            out[int(m.group(1))] = float(
                np.asarray(Image.open(p).convert("L"), dtype=np.float32).mean())
        except Exception:
            pass
    return out


def trajectory(run_log):
    out = {}
    if not os.path.exists(run_log):
        return out
    for line in open(run_log, errors="ignore"):
        m = re.match(r"\[BENCH\] Step (\d+): \(([-\d.]+),([-\d.]+)\)", line)
        if m:
            out[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
    return out


def classify(fpv_frac, bird_frac, sync, disp):
    if fpv_frac < MIN_FRAC:
        return "OK"
    if sync >= SYNC_HI:
        return "RENDER"          # fpv & bird die together = renderer fault
    if disp is not None and disp >= MOVED_M:
        return "CAMERA"          # only fpv, agent had moved = clip / walked out
    return "DARK"                # only fpv, low displacement = dim spawn/area


def audit_task(task_dir):
    fpv = lum_series(os.path.join(task_dir, "vlm_nav_frames_fpv"))
    if not fpv:
        return None
    bird = lum_series(os.path.join(task_dir, "vlm_nav_frames_bird"))
    traj = trajectory(os.path.join(task_dir, "run.log"))

    n = len(fpv)
    fpv_dark = {k for k, v in fpv.items() if v < DARK}
    bird_dark = {k for k, v in bird.items() if v < DARK}
    fpv_frac = len(fpv_dark) / n
    bird_frac = (len(bird_dark) / len(bird)) if bird else 0.0
    sync = (len(fpv_dark & bird_dark) / len(fpv_dark)) if fpv_dark else 0.0

    first_dark = min(fpv_dark) if fpv_dark else None
    disp = None
    if first_dark is not None and 0 in traj and first_dark in traj:
        x0, y0 = traj[0]
        xd, yd = traj[first_dark]
        disp = math.hypot(xd - x0, yd - y0)

    return dict(
        frames=n,
        fpv_black_frac=round(fpv_frac, 3),
        bird_black_frac=round(bird_frac, 3),
        sync=round(sync, 3),
        first_dark_step=first_dark if first_dark is not None else -1,
        disp_at_first_dark=round(disp, 2) if disp is not None else "",
        head_lum=round(float(np.mean([fpv[k] for k in sorted(fpv)[:max(1, n // 10)]])), 1),
        cls=classify(fpv_frac, bird_frac, sync, disp),
    )


def task_id(dirname):
    return re.sub(r"_\d{8}_\d{6}$", "", dirname)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "results"
    folders = sys.argv[2:] if len(sys.argv) > 2 else [
        "eval_30B_333_1frame",
        "eval_30B_333_rerun_fixed",
        "eval_30B_333_remaining_fixed",
    ]
    rows = []
    for fold in folders:
        seen = set()
        for td in sorted(glob.glob(os.path.join(root, fold, "*", "*"))):
            if not os.path.isdir(td):
                continue
            tid = task_id(os.path.basename(td))
            if tid in seen:          # one row per task_id (first = has results)
                continue
            r = audit_task(td)
            if r is None:
                continue
            seen.add(tid)
            level = os.path.basename(os.path.dirname(td))
            r.update(folder=fold, level=level, task_id=tid,
                     dir=os.path.basename(td))
            rows.append(r)

    if not rows:
        print("no task dirs found under", root, folders)
        return

    cols = ["folder", "level", "task_id", "frames", "fpv_black_frac",
            "bird_black_frac", "sync", "first_dark_step", "disp_at_first_dark",
            "head_lum", "cls", "dir"]
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})

    print(f"DARK<{DARK}  MIN_FRAC(class cutoff)={MIN_FRAC}  -> {OUT}")
    print(f"total tasks audited: {len(rows)}\n")

    # threshold-free distribution of fpv_black_frac
    fr = sorted(r["fpv_black_frac"] for r in rows)
    pct = lambda p: fr[min(len(fr) - 1, int(p * len(fr)))]
    print("fpv_black_frac distribution (threshold-free):")
    print(f"  median={pct(.5):.3f}  p90={pct(.9):.3f}  p95={pct(.95):.3f}  "
          f"p99={pct(.99):.3f}  max={fr[-1]:.3f}")
    for cut in (0.05, 0.10, 0.20, 0.30, 0.50):
        n = sum(1 for x in fr if x >= cut)
        print(f"  >= {cut:.2f} black: {n:3d} ({100*n/len(rows):.1f}%)")
    print()

    # per folder x class
    for fold in folders:
        sub = [r for r in rows if r["folder"] == fold]
        if not sub:
            continue
        by_cls = Counter(r["cls"] for r in sub)
        by_cls_lvl = defaultdict(Counter)
        for r in sub:
            if r["cls"] != "OK":
                by_cls_lvl[r["cls"]][r["level"]] += 1
        print(f"=== {fold}: {len(sub)} tasks ===")
        print("  class counts:", dict(by_cls))
        for c in ("RENDER", "CAMERA", "DARK"):
            if by_cls.get(c):
                print(f"    {c} by level: {dict(by_cls_lvl[c])}")
        flagged = [r for r in sub if r["cls"] != "OK"]
        for r in sorted(flagged, key=lambda r: -r["fpv_black_frac"]):
            print(f"    [{r['cls']:6}] {r['level']} {r['task_id'][:40]:40s} "
                  f"fpv={r['fpv_black_frac']*100:4.0f}% bird={r['bird_black_frac']*100:3.0f}% "
                  f"sync={r['sync']:.2f} firstdark={r['first_dark_step']} "
                  f"disp={r['disp_at_first_dark']}")
        print()


if __name__ == "__main__":
    main()
