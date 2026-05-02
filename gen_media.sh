#!/usr/bin/env bash
# Generate HD + Preview media from rendered navigation frames.
# Run on host (not in Docker) with conda env 'pp' for ffmpeg.
#
# Usage:
#   ssh GPU-843 'source ~/miniconda3/etc/profile.d/conda.sh && conda activate pp && \
#     bash /home/qi/hc/Puppeteer/zehao_task/gen_media.sh'

set -u

BASE="/home/qi/hc/Puppeteer/zehao_task"
PREVIEW="$BASE/nav_preview"
mkdir -p "$PREVIEW"

FPS=5
GIF_MAX=80   # max frames for GIF to keep file size reasonable

for VIEW in fpv birdseye; do
    if [ "$VIEW" = "fpv" ]; then
        SRC="$BASE/vlm_nav_frames_fpv"
    else
        SRC="$BASE/vlm_nav_frames_bird"
    fi

    N=$(ls "$SRC"/rgb_*.png 2>/dev/null | wc -l)
    [ "$N" -eq 0 ] && echo "[SKIP] $VIEW: no frames" && continue
    echo "[INFO] $VIEW: $N frames in $SRC"

    PAT="$SRC/rgb_%04d.png"
    GN=$((N < GIF_MAX ? N : GIF_MAX))

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
