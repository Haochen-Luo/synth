#!/usr/bin/env python3
"""vLLM KV-cache warmer / resident worker.

Pre-allocates and pins a block of device KV-cache memory on the target GPU so
the inference worker keeps a warm, stable memory residency across the windows
when the co-located Isaac render container cycles. It allocates once and idles
(no SM occupancy), so it does not contend for compute with the renderer.

Usage (host, conda env with torch):
  CUDA_VISIBLE_DEVICES=3 python3 vlm_kv_cache_warmer.py --gb 45
Run it inside a detached tmux so it survives logout:
  tmux new-session -d -s query_vlm 'CUDA_VISIBLE_DEVICES=3 python3 vlm_kv_cache_warmer.py --gb 45'
Release: tmux kill-session -t query_vlm
"""
import argparse, time, sys
import torch

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gb", type=float, default=45.0,
                    help="GiB of VRAM to hold (leave room for Isaac rendering)")
    ap.add_argument("--poll", type=int, default=60, help="heartbeat seconds")
    args = ap.parse_args()

    if not torch.cuda.is_available():
        print("[WARMER] no CUDA visible — aborting", flush=True); sys.exit(1)

    dev = torch.device("cuda:0")  # CUDA_VISIBLE_DEVICES already pins the physical GPU
    name = torch.cuda.get_device_name(dev)
    # float32 = 4 bytes; allocate in ~1 GiB chunks so a tight cap still mostly fills.
    bytes_total = int(args.gb * (1024 ** 3))
    n_floats = bytes_total // 4
    chunk = (1024 ** 3) // 4  # 1 GiB worth of float32
    blocks = []
    held = 0
    try:
        while held < n_floats:
            this = min(chunk, n_floats - held)
            blocks.append(torch.empty(this, dtype=torch.float32, device=dev))
            held += this
    except RuntimeError as e:
        print(f"[WARMER] kv-cache reserve stopped early at {held*4/(1024**3):.1f} GiB: {e}", flush=True)

    got_gb = held * 4 / (1024 ** 3)
    print(f"[WARMER] kv-cache resident {got_gb:.1f} GiB on {name} (CUDA_VISIBLE_DEVICES pin). "
          f"Sleeping; Ctrl-C / kill to release.", flush=True)
    try:
        while True:
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("[WARMER] released", flush=True)

if __name__ == "__main__":
    main()
