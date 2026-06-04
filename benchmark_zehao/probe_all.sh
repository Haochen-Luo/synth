#!/bin/bash
# Probe all scenes sequentially inside the bench-isaac container
SCENES_DIR="$(cd "$(dirname "$0")" && pwd)"
PROBER="$SCENES_DIR/scene_prober.py"

for scene in $SCENES_DIR/native_*_full_physics_scene; do
    name=$(basename $scene)
    short=$(echo $name | sed 's/native_//' | sed 's/_full_physics_scene//')
    out="$SCENES_DIR/probed_${short}.json"
    if [ -f "$out" ]; then
        echo "[SKIP] $short (already probed)"
        continue
    fi
    echo "[PROBE] $short ..."
    docker exec -e SCENE_DIR=$scene bench-isaac /isaac-sim/python.sh $PROBER 2>&1 | tail -5
    echo "[DONE] $short"
    echo
done

echo "=== All probed files ==="
ls -la $SCENES_DIR/probed_*.json 2>/dev/null
