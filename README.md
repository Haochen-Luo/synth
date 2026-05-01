# VLM Navigation Benchmark

Closed-loop indoor navigation benchmark powered by a Vision-Language Model (VLM) running inside NVIDIA Isaac Sim.

An agent (human mesh) navigates a physically-accurate living room to reach a **sofa**, while avoiding a dynamically-moving obstacle (runner). The VLM receives first-person camera frames each step and outputs discrete navigation actions.

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
     │  2. Update FPV camera (pure Yaw rotation)    │
     │  3. Render frame (PathTracing, 16 subframes)  │
     │  4. Send frame to VLM API                    │
     │  5. Apply action (MOVE/TURN/STOP)            │
     │  6. PhysX sweep_sphere_closest collision      │
     └──────────────────────┬──────────────────────┘
                            │
                            ▼
     ┌─────────────────────────────────────────────┐
     │          VLM (vLLM on localhost:8300)         │
     │  Model: Qwen/Qwen3-VL-30B-A3B-Instruct-FP8  │
     │  Input: Base64 PNG frame                     │
     │  Output: MOVE_FORWARD | TURN_LEFT |          │
     │          TURN_RIGHT | STOP                   │
     └─────────────────────────────────────────────┘
```

---

## Core Files

| File | Purpose |
|------|---------|
| `vlm_nav_benchmark.py` | **Headless benchmark script.** Runs the full loop end-to-end in Docker, writes logs to `vlm_nav.log`, and generates MP4/GIF outputs. Best for automated batch testing. |
| `vlm_nav_interactive.py` | **Jupyter Notebook script** (`.py` with `# %%` cell markers). Same logic split into 5 cells for interactive step-by-step debugging in the browser. |
| `start_jupyter.sh` | Launches the Isaac Sim Docker container with Jupyter Notebook server on port 8888. |
| `render_demo_nav.py` | Earlier demo rendering script (reference only). |

### Supporting / Test Files (can be archived)

| File | Purpose |
|------|---------|
| `test_cameras_orient.py` | Camera orientation debugging (quaternion experiments) |
| `test_quat.py` | Quaternion math validation |
| `test_issac_quick.py` | Minimal Isaac Sim startup test |
| `camera_grid_search.py` | Bird's-eye camera position sweep |

---

## Navigation Parameters

```python
STEP_DISTANCE   = 0.25    # meters per MOVE_FORWARD
TURN_ANGLE      = 30.0    # degrees per TURN_LEFT / TURN_RIGHT
SUCCESS_RADIUS  = 0.8     # agent STOPs within 0.8m → SUCCESS
MAX_STEPS       = 250     # timeout limit (interactive) / 5 (quick test in benchmark)

AGENT_HEIGHT    = 0.0     # Z-position of agent root (floor level)
AGENT_EYE_HEIGHT = 1.58   # Z-position of FPV camera (human eye level)

TARGET          = [4.38, 6.44]  # Sofa center coordinates
AGENT_START     = (12.0, 4.0)   # Start on the rug
AGENT_START_YAW = 160.0         # Facing roughly toward sofa
```

---

## Physics & Collision

### Collision Detection: PhysX Sphere Sweep

Instead of a thin 1D raycast, we use a **volumetric sphere sweep** (`sweep_sphere_closest`) that wraps the agent in an invisible 0.4m-diameter collision shell:

```python
import omni.physx, carb
query = omni.physx.get_physx_scene_query_interface()

origin    = carb.Float3(agent_x, agent_y, 0.5)     # sphere center at knee/waist height
direction = carb.Float3(dir_x, dir_y, 0.0)          # forward direction
hit       = query.sweep_sphere_closest(0.2, origin, direction, 0.25)  # radius=0.2m, dist=0.25m

if hit["hit"]:
    # BLOCKED — agent stays in place, VLM receives collision warning
```

### Human Mesh Scaling

All human `.usdc` meshes are authored in **centimeter** units. Isaac Sim uses **meters**. Both the agent and the obstacle runner must have:

```python
scale_op.Set(Gf.Vec3d(0.01, 0.01, 0.01))  # cm → m conversion
```

Without this, humans appear as 180m giants and all physics thresholds become meaningless.

---

## Camera System

### FPV Camera (VLM Input)

- **Position**: Follows agent at `(agent_x, agent_y, 1.58)` — simulating human eye height.
- **Orientation**: Pure Z-axis yaw rotation only. No pitch, no roll.
  ```python
  q_yaw = Gf.Rotation(Gf.Vec3d(0, 0, 1), float(agent_yaw)).GetQuat()
  ```
- **Resolution**: 1920 × 1080 (PathTracing, 16 SPP subframes)

> **Design note**: The default USD camera at `rotation=(0,0,0)` already looks horizontally forward. Applying only yaw rotation keeps the horizon perfectly level. Using `lookat_to_quatf` causes a 90° pitch-down because it aligns the +X axis (not -Z lens axis) toward the target.

### Bird's-Eye Camera (Visualization)

- **Position**: `(13.0, 7.0, 2.7)` — elevated corner of the room
- **Look-at**: `(5.0, 5.0, 0.5)` — center of the living room
- **Purpose**: Third-person visualization only; not fed to VLM.

---

## Scene Assets

All scene data lives under:
```
/home/qi/hc/Puppeteer/zehao_new_folder/phy_env/case11_multi_surface_turn_right_full_physics_scene/
```

| Path | Contents |
|------|----------|
| `compiled_stages/*.usda` | Main USD stage (room geometry, furniture, walls, floor) |
| `compiled_specs/*.spec.json` | Human trajectory keyframes, animation FPS, loop modes |
| `assets/humans/obj_1_run_anim_1.usdc` | Animated human mesh (runner/agent, centimeter-scale) |
| `metadata/` | Scene metadata |
| `runtime/` | Runtime intent and validation data |

---

## Docker Environment

### Container: `vlm-jupyter`

```bash
# Launch (run on GPU-843):
bash /home/qi/hc/Puppeteer/zehao_task/start_jupyter.sh
```

This starts:
- **Image**: `nvcr.io/nvidia/isaac-sim:4.5.0`
- **GPU**: Device 4 (`--gpus '"device=4"'`)
- **Network**: Host mode (port 8888 for Jupyter, port 8300 for vLLM)
- **Volume mount**: `/home/qi/hc/Puppeteer` → same path inside container
- **Working dir**: `/home/qi/hc/Puppeteer/zehao_task`

### Running the Benchmark (Headless)

```bash
# From login node:
ssh GPU-843 "docker exec vlm-jupyter /isaac-sim/python.sh \
  /home/qi/hc/Puppeteer/zehao_task/vlm_nav_benchmark.py"
```

### Running Interactively (Jupyter)

1. SSH port-forward: `ssh -L 8888:localhost:8888 GPU-843`
2. Open `http://localhost:8888` in browser
3. Open `vlm_nav_interactive.py`
4. Run cells sequentially (Cell 1 → Cell 5)

### VLM Server (vLLM)

The VLM must be running separately on `localhost:8300`:
```
Model: Qwen/Qwen3-VL-30B-A3B-Instruct-FP8
Endpoint: http://localhost:8300/v1/chat/completions
```

---

## Output Files

After a benchmark run, the following are generated:

### Frame Directories
| Directory | Contents |
|-----------|----------|
| `vlm_nav_frames_fpv/` | Per-step FPV camera frames (`rgb_0000.png`, ...) |
| `vlm_nav_frames_bird/` | Per-step bird's-eye camera frames |

### Media (Auto-generated via FFmpeg in `pp` conda env)

| File | Type | Resolution | Use Case |
|------|------|-----------|----------|
| `demo_fpv_hd.mp4` | H.264 MP4 | 1920×1080 | Archival / paper figures |
| `demo_fpv_lite.mp4` | H.264 MP4 | 640p | Quick browser preview |
| `demo_fpv_hd.gif` | GIF | 960p | Slides / README embedding |
| `demo_fpv_lite.gif` | GIF | 480p | Instant preview |
| `demo_birdseye_hd.*` | Same 4 variants for bird's-eye view | | |

### Logs
| File | Contents |
|------|----------|
| `vlm_nav.log` | Step-by-step navigation log (positions, actions, collisions) |
| `vlm_nav_history.json` | Structured JSON log of the full trajectory |

---

## Known Issues & Gotchas

1. **OptixDenoiser warnings** (`nvoptix.bin` not found) — harmless; denoiser is optional and not used.
2. **`shutil` must be imported** inside the `try` block of `vlm_nav_benchmark.py` (it runs inside Isaac Sim's embedded Python).
3. **Frame directory cleanup** — Both scripts call `shutil.rmtree()` before each run to prevent stale frames from previous runs contaminating the output video.
4. **Camera default facing direction** — In USD, a camera at `rotation=(0,0,0)` faces along -Z (forward). Applying `lookat_to_quatf` rotates the +X axis instead, causing a 90° downward pitch. Always use pure Z-axis yaw rotation for FPV.
5. **VLM oscillation** — The VLM sometimes oscillates between TURN_LEFT and TURN_RIGHT indefinitely. This is a model-level limitation, not a physics bug.

---

## Conda Environment: `pp`

FFmpeg for media post-processing is available in the `pp` conda environment:
```bash
source /home/qi/miniconda3/etc/profile.d/conda.sh && conda activate pp
ffmpeg -version  # should work
```

This is used both by the interactive script's Cell 5 and by manual command-line media generation.
