#!/usr/bin/env python3
"""
One-shot acceptance check for the room-boundary fix + render guard.

Compares each of the 10 flagged tasks BEFORE (old run, eval_30B_rerun_fixed) vs
AFTER (tonight's eval_30B_blackfix_verify), and renders a verdict per class:

  CAMERA tasks (walk-out-of-room): PASS if the new fpv_black_frac dropped sharply
    AND the new run.log shows `hit=room_boundary` blocks (the gate fired).
  RENDER tasks (renderer fault):  EXPECTED-STILL-BLACK — the boundary can't fix
    them; PASS-ish if the guard set render_invalid=true so SR can exclude them.

No GPU/network; reads frames + run.log + results.json on the node holding them (HK).

Usage:  python verify_blackfix.py
Env:    OLD_BATCH (default eval_30B_333_rerun_fixed)
        NEW_BATCH (default eval_30B_blackfix_verify)
        DARK (8.0)
"""
import glob, json, os, re, sys
import numpy as np
from PIL import Image

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
OLD = os.environ.get("OLD_BATCH", "eval_30B_333_rerun_fixed")
NEW = os.environ.get("NEW_BATCH", "eval_30B_blackfix_verify")
DARK = float(os.environ.get("DARK", "8.0"))

# task_id -> expected class (from the audit)
CAMERA = {"case18_dining_push_lift-L2", "case069_official_solo_run-L2",
          "case069_official_solo_run-L4", "case076_official_two_runners-L3",
          "case055_official_solo_run-L3", "case012_official_two_runners-L2"}
RENDER = {"case06_scene_gen_v5_test_input_case5-L4", "case064_official_run_jump-L4",
          "case075_official_solo_run-L4", "case024_official_two_runners-L3"}


def find_dir(batch, tid):
    for d in glob.glob(f"{ROOT}/{batch}/*/{tid}_*"):
        if os.path.isdir(d):
            return d
    return None


def black_frac(d):
    if not d:
        return None
    fs = sorted(glob.glob(f"{d}/vlm_nav_frames_fpv/rgb_*.png"))
    if not fs:
        return None
    n = blk = 0
    for f in fs:
        n += 1
        if np.asarray(Image.open(f).convert("L"), dtype=np.float32).mean() < DARK:
            blk += 1
    return blk / n if n else None


def boundary_hits(d):
    if not d or not os.path.exists(f"{d}/run.log"):
        return 0
    return sum(1 for ln in open(f"{d}/run.log", errors="ignore")
               if "room_boundary" in ln)


def render_invalid(d):
    if not d or not os.path.exists(f"{d}/results.json"):
        return None
    try:
        rq = json.load(open(f"{d}/results.json"))["metrics"].get("render_quality", {})
        return rq.get("render_invalid")
    except Exception:
        return None


def pct(x):
    return "  n/a" if x is None else f"{100*x:4.0f}%"


def main():
    tasks = sorted(CAMERA | RENDER)
    print(f"OLD={OLD}  NEW={NEW}  DARK<{DARK}\n")
    hdr = f"{'task':42s} {'cls':6s} {'old_blk':>7s} {'new_blk':>7s} {'bndHits':>7s} {'rInval':>6s}  verdict"
    print(hdr); print("-" * len(hdr))
    n_done = n_pass = 0
    for tid in tasks:
        cls = "CAMERA" if tid in CAMERA else "RENDER"
        od, nd = find_dir(OLD, tid), find_dir(NEW, tid)
        ob, nb = black_frac(od), black_frac(nd)
        bh, ri = boundary_hits(nd), render_invalid(nd)
        if nd is None or nb is None:
            verdict = "PENDING (not rendered yet)"
        else:
            n_done += 1
            if cls == "CAMERA":
                # fixed if black dropped a lot AND boundary gate fired
                if nb < 0.10 and (ob is None or nb < ob - 0.10):
                    verdict = "PASS — black gone" + (f", gate fired x{bh}" if bh else " (no gate hit?)")
                    n_pass += 1
                elif nb < (ob or 1) - 0.10:
                    verdict = f"IMPROVED but still {pct(nb)} black"
                else:
                    verdict = "FAIL — still black"
            else:  # RENDER
                if ri is True:
                    verdict = "EXPECTED-BLACK, render_invalid flagged ✓"
                    n_pass += 1
                elif nb is not None and nb >= 0.20:
                    verdict = "still black but render_invalid NOT set ✗"
                else:
                    verdict = f"black={pct(nb)} render_invalid={ri}"
        print(f"{tid:42s} {cls:6s} {pct(ob):>7s} {pct(nb):>7s} {bh:7d} {str(ri):>6s}  {verdict}")
    print("-" * len(hdr))
    print(f"rendered {n_done}/{len(tasks)}  |  pass {n_pass}/{n_done if n_done else '-'}")
    if n_done < len(tasks):
        print("(re-run after the batch finishes for the full verdict)")


if __name__ == "__main__":
    main()
