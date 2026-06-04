#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# extract_scenes.sh — Extract scene archives from deliverables
#
# Usage:
#   ./extract_scenes.sh                    # extract ALL categories
#   ./extract_scenes.sh 20_native          # extract only 20_native
#   ./extract_scenes.sh 20_native 40_textmask  # extract specific categories
#   ./extract_scenes.sh --list             # list available archives
#   ./extract_scenes.sh --dry-run          # show what would be extracted
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$SCRIPT_DIR/physics_isaac_deliverables_20260519"
DST_DIR="$SCRIPT_DIR/../full_scenarios_extracted"
mkdir -p "$DST_DIR"

CATEGORIES=("15_real2sim" "20_native" "40_textmask" "80_official")

# ── Parse args ──
DRY_RUN=false
LIST_ONLY=false
SELECTED=()

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --list)    LIST_ONLY=true ;;
        *)         SELECTED+=("$arg") ;;
    esac
done

# Default: all categories
if [ ${#SELECTED[@]} -eq 0 ]; then
    SELECTED=("${CATEGORIES[@]}")
fi

# ── List mode ──
if $LIST_ONLY; then
    for cat in "${CATEGORIES[@]}"; do
        cat_dir="$SRC_DIR/$cat"
        [ -d "$cat_dir" ] || continue
        count=$(find "$cat_dir" -name "*.tar.gz" | wc -l)
        echo "[$cat] $count archives:"
        find "$cat_dir" -name "*.tar.gz" -printf "  %f\n" | sort
        echo ""
    done
    exit 0
fi

# ── Extract ──
TOTAL=0; SKIP=0; EXTRACT=0; FAIL=0

for cat in "${SELECTED[@]}"; do
    cat_dir="$SRC_DIR/$cat"
    if [ ! -d "$cat_dir" ]; then
        echo "WARNING: Category '$cat' not found at $cat_dir"
        continue
    fi

    echo "========================================"
    echo "Processing category: $cat"
    echo "========================================"

    for archive in "$cat_dir"/*.tar.gz; do
        [ -f "$archive" ] || continue
        TOTAL=$((TOTAL + 1))

        # Scene name = archive basename without .tar.gz
        scene_name=$(basename "$archive" .tar.gz)

        # Check if already extracted
        if [ -d "$DST_DIR/$scene_name" ]; then
            echo "  SKIP (exists): $scene_name"
            SKIP=$((SKIP + 1))
            continue
        fi

        if $DRY_RUN; then
            echo "  WOULD EXTRACT: $scene_name"
            EXTRACT=$((EXTRACT + 1))
            continue
        fi

        echo -n "  Extracting: $scene_name ... "
        if tar -xzf "$archive" -C "$DST_DIR" 2>/dev/null; then
            echo "OK"
            EXTRACT=$((EXTRACT + 1))
        else
            echo "FAILED"
            FAIL=$((FAIL + 1))
        fi
    done
done

echo ""
echo "========================================"
echo "Summary: Total=$TOTAL  Extracted=$EXTRACT  Skipped=$SKIP  Failed=$FAIL"
if $DRY_RUN; then
    echo "(DRY RUN — nothing was actually extracted)"
fi
echo "========================================"
