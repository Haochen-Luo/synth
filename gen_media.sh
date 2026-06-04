#!/usr/bin/env bash
# Generate HD + Preview media from rendered navigation frames.
# Usage:
#   bash gen_media.sh [RUN_DIR]
# If RUN_DIR is provided, look for frames inside it. Otherwise use default paths.

set -u

# Accept run directory as argument, or use default
BASE="${1:-$(cd "$(dirname "$0")" && pwd)}"
PREVIEW="$BASE/nav_preview"
mkdir -p "$PREVIEW"

FPS=2          # decision-frame playback rate (one frame per agent step)
# Smooth playback rate. Match it to FILLER_FPS in bench_runner.py: filler
# frames are rendered at FILLER_FPS frames per second of sim-time, so playing
# them back at the same rate gives near-real-time motion. FILLER_FPS is 3.
SMOOTH_FPS=3
GIF_MAX=0    # 0 = no cap, include all frames (set >0 to limit GIF size)

for VIEW in fpv birdseye; do
    # FPV: use the *_smooth folder (decision + filler, contiguous, leap-free,
    #      fast playback).
    # Bird: if a bird _smooth folder exists (ENABLE_BIRD_SMOOTH was on) use it;
    #       otherwise fall back to the per-step decision folder (contiguous
    #       rgb_NNNN by step), played at the slow per-step rate.
    if [ "$VIEW" = "fpv" ]; then
        SMOOTH="$BASE/vlm_nav_frames_fpv_smooth"
        SRC="$BASE/vlm_nav_frames_fpv"
        FPS=2
        if [ -d "$SMOOTH" ] && [ "$(ls "$SMOOTH"/rgb_*.png 2>/dev/null | wc -l)" -gt 0 ]; then
            SRC="$SMOOTH"; FPS=$SMOOTH_FPS
            echo "[INFO] $VIEW: using smooth frames"
        fi
    else
        SMOOTH="$BASE/vlm_nav_frames_bird_smooth"
        SRC="$BASE/vlm_nav_frames_bird"
        FPS=2
        if [ -d "$SMOOTH" ] && [ "$(ls "$SMOOTH"/rgb_*.png 2>/dev/null | wc -l)" -gt 0 ]; then
            SRC="$SMOOTH"; FPS=$SMOOTH_FPS
            echo "[INFO] $VIEW: using bird smooth frames"
        else
            echo "[INFO] $VIEW: using per-step decision frames (bird filler disabled)"
        fi
    fi

    N=$(ls "$SRC"/rgb_*.png 2>/dev/null | wc -l)
    [ "$N" -eq 0 ] && echo "[SKIP] $VIEW: no frames" && continue
    echo "[INFO] $VIEW: $N frames in $SRC"

    PAT="$SRC/rgb_%04d.png"
    GN=$((GIF_MAX > 0 && N > GIF_MAX ? GIF_MAX : N))

    # ─── HD outputs (base_dir) ───
    # HD MP4 (1920x1080)
    echo "  -> HD MP4..."
    ffmpeg -y -framerate $FPS -i "$PAT" -frames:v "$N" \
        -c:v libx264 -pix_fmt yuv420p -crf 18 -preset fast \
        "$BASE/vlm_nav_${VIEW}_hd.mp4" 2>/dev/null
    
    # HD GIF (960x540)
    echo "  -> HD GIF ($GN frames)..."
    ffmpeg -y -framerate $FPS -i "$PAT" -frames:v "$GN" \
        -vf "scale=960:540,fps=$FPS,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" \
        "$BASE/vlm_nav_${VIEW}_hd.gif" 2>/dev/null

    # ─── Preview / thumbnail outputs (nav_preview/) ───
    # Preview MP4 (480x270)
    echo "  -> Preview MP4..."
    ffmpeg -y -framerate $FPS -i "$PAT" -frames:v "$N" \
        -c:v libx264 -pix_fmt yuv420p -vf "scale=480:270" \
        -crf 28 -preset fast \
        "$PREVIEW/${VIEW}_preview.mp4" 2>/dev/null

    # Preview GIF (320x180)
    echo "  -> Preview GIF ($GN frames)..."
    ffmpeg -y -framerate $FPS -i "$PAT" -frames:v "$GN" \
        -vf "scale=320:180,fps=$FPS,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" \
        "$PREVIEW/${VIEW}_preview.gif" 2>/dev/null

    # ─── Backfill per-frame thumbnails (480x270 JPEG) ───
    MISSING=0
    for png in "$SRC"/rgb_*.png; do
        thumb="${png%.png}_thumb.jpg"
        if [ ! -f "$thumb" ]; then
            ffmpeg -y -i "$png" -vf "scale=480:270" -q:v 5 "$thumb" 2>/dev/null
            MISSING=$((MISSING + 1))
        fi
    done
    [ "$MISSING" -gt 0 ] && echo "  -> Backfilled $MISSING missing thumbnails" || true

    echo "  [DONE] $VIEW"
done

# ─── Summary ───
echo ""
echo "=== HD outputs ==="
ls -lh "$BASE"/vlm_nav_*_hd.{mp4,gif} 2>/dev/null || echo "(none)"
echo ""
echo "=== Preview outputs ==="
ls -lh "$PREVIEW"/*_preview.{mp4,gif} 2>/dev/null || echo "(none)"
echo ""
echo "=== Per-frame thumbnails ==="
echo "FPV thumbs: $(ls "$BASE/vlm_nav_frames_fpv"/*_thumb.jpg 2>/dev/null | wc -l)"
echo "Bird thumbs: $(ls "$BASE/vlm_nav_frames_bird"/*_thumb.jpg 2>/dev/null | wc -l)"
