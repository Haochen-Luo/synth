#!/usr/bin/env python3
"""Scan FPV preview mp4s (nav_preview_threeframe_fixed) for solid/dominant color
anomalies and map each hit to the concat-video timestamp via the metadata CSV.

Frames come from each case's nav_preview/fpv_preview.mp4 (cv2). A frame is flagged
by mean RGB:
  GREEN : G-R >= DOM and G-B >= DOM
  PURPLE: R-G >= DOM and B-G >= DOM
  BLUE  : B-R >= DOM and B-G >= DOM

Run locally (cv2 available):
  python3 scan_color_frames.py --csv fpv_metadata_L2.csv --want green
Out: stdout + /tmp/color_scan.txt
"""
import os, csv, argparse
import cv2
import numpy as np

BENCH = "/home/qi/hc/synth/benchmark_zehao"
PREVIEW = os.path.join(BENCH, "nav_preview_threeframe_fixed")
OUT = open("/tmp/color_scan.txt", "w")
def emit(s):
    OUT.write(str(s)+"\n"); OUT.flush(); print(s, flush=True)


def find_mp4(level, source, case_folder):
    p = os.path.join(PREVIEW, source, level, case_folder,
                     "nav_preview", "fpv_preview.mp4")
    return p if os.path.isfile(p) else None


def classify(r, g, b, DOM):
    # GREEN: green is clearly the top channel
    if g - r >= DOM and g - b >= DOM:
        return "GREEN"
    # BLUE: blue strongly dominates BOTH others (near-pure blue)
    if b - r >= DOM and b - g >= DOM:
        return "BLUE"
    # PURPLE/violet: blue high, GREEN is the suppressed channel, red mid-high
    #   (magenta cast). Characteristic: b-g large AND g is the min channel AND
    #   red elevated relative to green. Looser than BLUE (red need not be low).
    if b - g >= DOM and g == min(r, g, b) and r - g >= DOM // 3:
        return "PURPLE"
    return None


def scan_mp4(path, DOM):
    """Return list of (frame_idx, (r,g,b), cls) flagged."""
    cap = cv2.VideoCapture(path)
    hits = []
    fi = -1
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        fi += 1
        b, g, r = fr.reshape(-1, 3).mean(0)   # cv2 is BGR
        cls = classify(r, g, b, DOM)
        if cls:
            hits.append((fi, (r, g, b), cls))
    cap.release()
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--want", default="green",
                    choices=["green", "purple", "blue", "all"])
    ap.add_argument("--dom", type=int, default=40)
    ap.add_argument("--fps", type=float, default=3.0)
    a = ap.parse_args()
    level = "L" + a.csv.split("_L")[-1][0]
    rows = list(csv.DictReader(open(os.path.join(BENCH, a.csv))))
    want = a.want.upper()
    emit(f"# scan {a.csv} level={level} want={a.want} dom={a.dom} cases={len(rows)}")
    flagged = []
    for r in rows:
        mp4 = find_mp4(level, r["source"], r["case_folder"])
        if not mp4:
            continue
        hits = scan_mp4(mp4, a.dom)
        sel = [h for h in hits if a.want == "all" or h[2] == want]
        if not sel:
            continue
        # report the worst (max color separation) frame for this case
        def sep(h):
            rr, gg, bb = h[1]
            return max(gg-rr, gg-bb) if h[2] == "GREEN" else max(rr-gg, bb-gg, bb-rr)
        fi, (rr, gg, bb), cls = max(sel, key=sep)
        start = float(r["start_seconds"])
        ts = start + fi / a.fps
        mm, ss = int(ts//60), ts % 60
        t10 = ts/10.0; m10, s10 = int(t10//60), t10 % 60
        emit(f"  {cls}  {r['case_folder']}  frame{fi:03d}/{len(sel)}hit  "
             f"concat={mm:02d}:{ss:04.1f}({ts:.0f}s)  10x={m10:02d}:{s10:04.1f}  "
             f"mean=({rr:.0f},{gg:.0f},{bb:.0f})  src={r['source']}")
        flagged.append((cls, r["case_folder"], fi, ts))
    emit(f"\n# total {a.want} cases flagged: {len(flagged)}")


if __name__ == "__main__":
    main()
