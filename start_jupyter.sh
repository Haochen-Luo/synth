#!/bin/bash
# start_jupyter.sh

echo "Killing any existing Jupyter containers..."
docker rm -f vlm-jupyter || true

# Repo root = this script's directory (synth repo root). Container mounts the
# repo at the SAME path it lives on the host so in-container abs paths match.
REPO="$(cd "$(dirname "$0")" && pwd)"

echo "Launching Isaac Sim Jupyter Notebook Server..."
docker run --name vlm-jupyter -d --network=host --gpus '"device=4"' \
    -e "ACCEPT_EULA=Y" -e "PRIVACY_CONSENT=Y" \
    --entrypoint /isaac-sim/jupyter_notebook.sh \
    -v "$REPO:$REPO" \
    -w "$REPO" \
    nvcr.io/nvidia/isaac-sim:4.5.0 \
    --allow-root --ip=0.0.0.0 --port=8888 --NotebookApp.token='' --NotebookApp.password=''

echo "Jupyter Notebook Server launched in background!"
echo "Please wait ~10 seconds for it to start."
echo "Access it at: http://<GPU-843-IP>:8888"

echo "Installing ffmpeg in container..."
docker exec vlm-jupyter bash -c "apt-get update -qq && apt-get install -y -qq ffmpeg > /dev/null 2>&1" && \
    echo "ffmpeg installed." || echo "ffmpeg install failed (non-critical)."
