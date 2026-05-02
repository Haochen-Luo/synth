#!/bin/bash
# Post-processing: generate HD + preview videos from navigation frames
# Run from host (not Docker): conda activate pp && bash gen_videos.sh
set -e

BASE="/home/qi/hc/Puppeteer/zehao_task"
PREVIEW="$BASE/nav_preview"
mkdir -p "$PREVIEW"

for view in fpv bird_rear bird_front; do
    case $view in
        fpv)        SRC="$BASE/vlm_nav_frames_fpv" ;;
        bird_rear)  SRC="$BASE/vlm_nav_frames_bird" ;;
        bird_front) SRC="$BASE/vlm_nav_frames_bird2" ;;
    esac

    N=$(ls "$SRC"/rgb_*.png 2>/dev/null | wc -l)
    [ "$N" -eq 0 ] && echo "[$view] No frames, skipping" && continue
    echo "[$view] $N frames"

    PAT="$SRC/rgb_%04d.png"

    # HD MP4 (1920x1080)
    ffmpeg -y -hide_banner -loglevel error -framerate 5 -i "$PAT" -frames:v "$N" \
        -c:v libx264 -pix_fmt yuv420p -crf 18 -preset fast "$BASE/vlm_nav_${view}_hd.mp4"
    echo "  → vlm_nav_${view}_hd.mp4"

    # HD GIF (960x540, max 100 frames)
    GF=$((N < 100 ? N : 100))
    ffmpeg -y -hide_banner -loglevel error -framerate 5 -i "$PAT" -frames:v "$GF" \
        -vf "scale=960:540,fps=5,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" "$BASE/vlm_nav_${view}_hd.gif"
    echo "  → vlm_nav_${view}_hd.gif"

    # Preview MP4 (480x270)
    ffmpeg -y -hide_banner -loglevel error -framerate 5 -i "$PAT" -frames:v "$N" \
        -c:v libx264 -pix_fmt yuv420p -vf scale=480:270 -crf 28 -preset fast "$PREVIEW/${view}_preview.mp4"
    echo "  → nav_preview/${view}_preview.mp4"

    # Preview GIF (320x180, max 100 frames)
    ffmpeg -y -hide_banner -loglevel error -framerate 5 -i "$PAT" -frames:v "$GF" \
        -vf "scale=320:180,fps=5,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" "$PREVIEW/${view}_preview.gif"
    echo "  → nav_preview/${view}_preview.gif"
done

echo "Done! Preview files in: $PREVIEW/"
