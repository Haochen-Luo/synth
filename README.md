# VLM Navigation Benchmark

Closed-loop indoor navigation benchmark powered by a Vision-Language Model (VLM) running inside NVIDIA Isaac Sim. A human-mesh agent navigates a physically-accurate living room to reach a target object, while avoiding a dynamically-moving obstacle (runner).

The benchmark follows **Habitat ObjectNav** conventions: the VLM receives egocentric RGB frames, outputs discrete actions, and relies on its own visual reasoning for all navigation decisions. No oracle planner, no pre-built map, no distance-to-target leakage.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Isaac Sim (Docker)                     │
│  ┌───────────────┐   ┌──────────────┐   ┌────────────┐ │
│  │  USD Scene     │   │  PhysX Engine│   │ Replicator  │ │
│  │  (Case 11)     │──▶│  Sweep Query │   │ PathTracing │ │
│  └───────────────┘   └──────────────┘   └────────────┘ │
│         │                    │                  │        │
│    Room Geometry      Collision Check     Frame Render   │
│    + Furniture        (Sphere r=0.2m)    (1920×1080)     │
└─────────┬───────────────────┬───────────────┬───────────┘
          │                   │               │
          ▼                   ▼               ▼
     ┌─────────────────────────────────────────────┐
     │            Navigation Loop (Python)          │
     │  1. Update agent pose in scene               │
     │  2. Update FPV camera (yaw + pitch tilt)     │
     │  3. Render frame (PathTracing, 16 subframes)  │
     │  4. Send frame + history to VLM API          │
     │  5. Apply action (MOVE/TURN/STOP)            │
     │  6. PhysX sweep_sphere_closest collision      │
     └──────────────────────┬──────────────────────┘
                            │
                            ▼
     ┌─────────────────────────────────────────────┐
     │          VLM (vLLM on localhost:8300)         │
     │  Model: Qwen/Qwen3-VL-30B-A3B-Instruct-FP8  │
     │  Input: Base64 PNG frame + nav history       │
     │  Output: MOVE_FORWARD | TURN_LEFT |          │
     │          TURN_RIGHT | STOP                   │
     └─────────────────────────────────────────────┘
```

---

## Core Files

| File | Purpose |
|------|---------|
| `vlm_nav_benchmark.py` | **Headless benchmark script.** Runs the full loop, writes logs/media to a timestamped `runs/<target>_<timestamp>/` directory. |
| `vlm_nav_interactive.py` | **Jupyter Notebook script** (`.py` with `# %%` cell markers). Same logic split into cells for interactive debugging. |
| `gen_media.sh` | FFmpeg-based media post-processing. Called automatically at end of benchmark run. |
| `regen_trajectory_plot.py` | Standalone 2D trajectory plotter. Can regenerate trajectory maps from any run's `vlm_nav_history.json`. |
| `start_jupyter.sh` | Launches the Isaac Sim Docker container with Jupyter on port 8888. |

---

## Navigation Parameters

```python
STEP_DISTANCE      = 0.25    # meters per MOVE_FORWARD
TURN_ANGLE         = 15.0    # degrees per TURN_LEFT / TURN_RIGHT
MAX_STEPS          = 250     # timeout limit
STOP_CONFIRM_ROUNDS = 2      # require 2 consecutive STOP predictions

AGENT_HEIGHT       = 0.07    # Z-offset for agent mesh (skeleton rest-pose correction)
AGENT_EYE_HEIGHT   = 1.58    # Z of FPV camera (human eye level)
CAMERA_PITCH_DEG   = -10     # degrees downward tilt (see low furniture)

AGENT_START        = (12.0, 4.0)
AGENT_START_YAW    = 160.0   # degrees
```

## Multi-Target Support

Target is selected via environment variable:

```bash
# Sofa (default)
docker exec vlm-jupyter /isaac-sim/python.sh vlm_nav_benchmark.py

# Bookshelf
NAV_TARGET=bookshelf docker exec vlm-jupyter /isaac-sim/python.sh vlm_nav_benchmark.py
```

| Target | Coords | Success Radius | Description |
|--------|--------|---------------|-------------|
| `sofa` | (4.37, 6.43) | 3.0m | Large light-green couch (easy — direct path) |
| `bookshelf` | (0.34, 8.76) | 1.5m | Tall white 4-tier shelf (hard — sofa blocks direct path) |

---

## Design Decisions (Habitat-Aligned)

### 1. Pure VLM Reasoning — No Oracle Planner

Following Habitat ObjectNav conventions, the VLM has full agency over navigation decisions. We intentionally do **not** provide:
- Distance to target (would leak oracle info)
- Pre-built occupancy map
- Action masking based on ground truth

The VLM receives only: **egocentric RGB frame + recent action history**.

### 2. Camera Pitch Tilt (-10°)

The camera is tilted 10° downward from horizontal. This is aligned with Habitat Challenge conventions (Hello Stretch robot configuration) where cameras are tilted to see the ground/obstacles ahead.

**Rationale**: At a perfectly horizontal 1.58m eye height, low furniture like sofas (~0.8m) barely appears in the FOV. The VLM sees the target *over* the sofa and walks straight into it. The -10° pitch makes obstacles significantly more visible.

### 3. Collision Feedback in Prompt (Not Oracle Planner)

When `MOVE_FORWARD` is blocked by physics collision, the nav history reports **"BLOCKED by obstacle"** instead of generic "no movement":

```
Step 103: MOVE_FORWARD (BLOCKED by obstacle, yaw=145°)
Step 104: MOVE_FORWARD (BLOCKED by obstacle, yaw=160°)
⚠ WARNING: You have NOT moved for the last 3+ steps. You are likely stuck.
```

This is equivalent to proprioceptive feedback (robot knows its wheels stopped turning) — not an oracle. The VLM still decides *how* to respond.

### 4. No VLM-Level Anti-Oscillation Override

VLM oscillation (repeating TURN_LEFT/TURN_RIGHT) is treated as a **diagnostic metric of model reasoning failure**, not something to paper over with code. This aligns with Habitat research where oscillation rates are reported as evaluation metrics.

### 5. Physics-Level Safety Mechanisms

Two safety mechanisms operate below the VLM decision layer. These are analogous to hardware-level safety (e.g., bumper sensors) and do not interfere with VLM reasoning during normal navigation:

#### Dark-Frame Escape
When 3 of the last 4 rendered frames are dark (camera facing a wall at close range), forces a 180° about-face + MOVE_FORWARD. After triggering, a 3-step cooldown prevents re-triggering.

```python
DARK_WINDOW_SIZE   = 4    # sliding window
DARK_THRESHOLD     = 3    # trigger threshold
DARK_ESCAPE_COOLDOWN = 3  # cooldown steps after trigger
```

#### Stuck Detector (Position Stagnation)
When the agent's position hasn't changed for 6+ steps (regardless of action type), intervenes on blocked MOVE_FORWARD actions. Uses the agent's actual approach direction to compute escape angles:
- **6–11 steps stuck**: Try ±90° off the approach direction (alternate sides)
- **12+ steps stuck**: Retreat backward (reverse of approach — guaranteed free path)

---

## Physics & Collision

### Collision Detection: PhysX Sphere Sweep

Volumetric sphere sweep at two heights (waist=0.5m, chest=1.0m) to catch both low and high obstacles:

```python
query = omni.physx.get_physx_scene_query_interface()
for sweep_z in [0.5, 1.0]:
    origin = carb.Float3(agent_x, agent_y, sweep_z)
    direction = carb.Float3(dir_x, dir_y, 0.0)
    hit = query.sweep_sphere_closest(0.2, origin, direction, STEP_DISTANCE)
```

### Human Mesh Scaling

All human `.usdc` meshes use the runner's `animation_binding` scale (`0.53`). Root Z-offset (`runner_root_offset[2] ≈ 0.534m`) keeps feet on the ground.

The VLM agent mesh (`agent_runner`) is a fresh USD reference instance. Its skeleton rest-pose root sits ~7cm lower than the animated runner, corrected by `AGENT_HEIGHT = 0.07`.

---

## Camera System

### FPV Camera (VLM Input)

- **Position**: `(agent_x, agent_y, 1.58)` — human eye height
- **Orientation**: Yaw rotation + pitch tilt (-10° downward)
- **Resolution**: 1920 × 1080 (PathTracing, 16 SPP subframes)

### Bird's-Eye Camera (Visualization Only)

- **Position**: `(13.0, 7.0, 2.7)` — elevated corner of room
- **Purpose**: Third-person view for trajectory analysis. Never fed to VLM.

---

## Output Structure

Each run produces a self-contained directory:

```
runs/bookshelf_20260503_051350/
├── vlm_nav.log              # Step-by-step log (positions, actions, collisions)
├── vlm_nav_history.json     # Structured trajectory data
├── vlm_responses.jsonl      # Raw VLM API responses
├── trajectory_2d.png        # Top-down trajectory visualization
├── vlm_nav_frames_fpv/      # Per-step FPV frames (rgb_0000.png, ...)
├── vlm_nav_frames_bird/     # Per-step bird's-eye frames
├── demo_fpv_hd.mp4          # HD FPV video
├── demo_fpv_lite.mp4        # Compressed FPV video
├── demo_birdseye_hd.mp4     # HD bird's-eye video
└── demo_birdseye_lite.mp4   # Compressed bird's-eye video
```

---

## Run Results

| Run | Target | Result | Steps | Final Dist | Notes |
|-----|--------|--------|-------|-----------|-------|
| `sofa_20260503_040344` | Sofa | ✅ SUCCESS | 33 | 2.82m | Clean path, STOP confirmed |
| `bookshelf_20260503_051350` | Bookshelf | ❌ TIMEOUT | 250 | 5.25m | Navigated correctly but got stuck at sofa edge (obstacle in path) |
| `bookshelf_20260503_041038` | Bookshelf | ❌ TIMEOUT | 250 | 12.4m | "Left wall" bias in prompt caused wrong turn from start (fixed) |
| `bookshelf_20260503_074526` | Bookshelf | ❌ TIMEOUT | 250 | 14.92m | VLM navigation failure (walked wrong direction) |

### Known Failure Mode: Sofa Obstruction

The bookshelf is behind the sofa. The VLM navigates correctly toward the bookshelf but gets physically blocked by the sofa. From the agent's eye level, the sofa appears below the bookshelf and the VLM doesn't recognize it as an obstacle. The camera tilt (-10°) and collision feedback ("BLOCKED by obstacle") were added to address this. Full validation pending next run with these changes.

---

## Docker Environment

### Container: `vlm-jupyter`

```bash
# Launch (on GPU-843):
bash /home/qi/hc/Puppeteer/zehao_task/start_jupyter.sh
```

- **Image**: `nvcr.io/nvidia/isaac-sim:4.5.0`
- **GPU**: Device 4 (`--gpus '"device=4"'`)
- **Network**: Host mode (port 8888 Jupyter, port 8300 vLLM)

### Running the Benchmark

```bash
# From login node — sofa target:
ssh GPU-843 "docker exec vlm-jupyter /isaac-sim/python.sh \
  /home/qi/hc/Puppeteer/zehao_task/vlm_nav_benchmark.py"

# Bookshelf target:
ssh GPU-843 "docker exec -e NAV_TARGET=bookshelf vlm-jupyter /isaac-sim/python.sh \
  /home/qi/hc/Puppeteer/zehao_task/vlm_nav_benchmark.py"
```

### VLM Server (vLLM)

Must be running on `localhost:8300`:
```
Model: Qwen/Qwen3-VL-30B-A3B-Instruct-FP8
Endpoint: http://localhost:8300/v1/chat/completions
```

---

## Conda Environment: `pp`

FFmpeg for media post-processing:
```bash
source /home/qi/miniconda3/etc/profile.d/conda.sh && conda activate pp
```

---

## Known Issues

1. **OptixDenoiser warnings** — Harmless; denoiser is optional.
2. **VLM oscillation** — Model sometimes alternates TURN_LEFT/TURN_RIGHT. This is a VLM reasoning limitation, not a code bug. Reported as a diagnostic metric.
3. **Low-obstacle blindness** — Even with -10° camera tilt, the VLM may not recognize low furniture as obstacles. This is a known limitation in VLM-only navigation (Habitat research uses separate geometric planners for this).
4. **Agent mesh Z-offset** — The `AGENT_HEIGHT = 0.07` correction is empirical. May need fine-tuning if the mesh USD changes.
5. **Double-Scaling** — The 4D human `.usdc` meshes may have baked USD scales. Ensure `scale_xyz` in `.spec.json` is `[1.0, 1.0, 1.0]` for models with baked scales.
