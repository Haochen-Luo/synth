# 4DSynth-Nav Benchmark — Hands-off

VLM indoor-navigation benchmark on physically-accurate 4D scenes. This file is
the session-handoff: read it to pick up where the last session left off.

Code lives in `benchmark_zehao/`. Git branch: **`benchmark-multiaction`**
(commits `2ac89be`, `9b417e4`). The repo is otherwise on a detached HEAD —
stay on this branch.

---

## Environment

- **Always run inside the `vlm-jupyter` Docker container on GPU-843** — never
  run `pxr`/`omni`/`isaacsim` code on the host.
  - `ssh GPU-843` then `docker exec vlm-jupyter /isaac-sim/python.sh <script>`
  - Container is bound to physical **GPU 4** (isaac-sim 4.5.0).
- vLLM server (Qwen3-VL-30B) serves the VLM on `localhost:8300`.
- If Isaac Sim segfaults on startup (happens after the container has been up a
  long time / many runs): `ssh GPU-843 'docker restart vlm-jupyter'`, wait ~10s.
- **OptiX denoiser** — PathTracing's denoiser needs `/usr/share/nvidia/nvoptix.bin`,
  which the container image is missing (NVIDIA runtime does not auto-mount this
  96 MB data file even with `NVIDIA_DRIVER_CAPABILITIES=all`). Without it,
  renders are noisy. It was `docker cp`'d in, but
  **a container restart loses it** — after any `docker restart vlm-jupyter`,
  re-run:
  ```
  ssh GPU-843 'docker cp /usr/share/nvidia/nvoptix.bin vlm-jupyter:/usr/share/nvidia/nvoptix.bin'
  ```
  (The DLSS-RR "not supported on H100" warning is a separate, unfixable Isaac
  bug affecting RayTracedLighting mode only — irrelevant to PathTracing.)
- Python syntax check: `PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m py_compile <f>`
  (the repo `__pycache__` is root-owned; the prefix dodges it).

---

## Run a single task

```
ssh GPU-843 'docker exec -e TASK_ID=case01-L2 \
  -e VLLM_URL=http://localhost:8300/v1/chat/completions \
  -e BATCH_NAME=myrun \
  vlm-jupyter /isaac-sim/python.sh \
  /home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/bench_runner.py'
```

Output: `benchmark_zehao/results/<BATCH_NAME>/<LEVEL>/<TASK>_<ts>/` —
`run.log`, `results.json`, `vlm_nav_frames_fpv/`, `vlm_nav_frames_bird/`,
`vlm_nav_frames_fpv_smooth/`, plus MP4/GIF from `gen_media.sh`.

Task file: `benchmark_zehao/benchmark_tasks.json` (40 tasks, `caseNN-LN`).
This is the original, verified task set — its `agent_start` positions place
the agent in sensible, on-floor, lit room locations. **Use this file.**

### Env-var knobs (bench_runner.py)
- `MAX_VLM_CALLS` (default 50) — episode ends at min(150 steps, this).
- `RENDER_W` / `RENDER_H` (default 960×540) — render resolution.
- `ENABLE_BIRD_SMOOTH` (default 0) — bird filler frames + bird `_smooth` folder.
- `TASKS_JSON` — override the task file path.

---

## What this session built

All on branch `benchmark-multiaction`:

1. **Obstacle-runner leap fix.** The runner mesh has a baked timeline
   animation; a playing timeline free-ran during renders, teleporting the
   runner. Fix: stop the timeline + one-time render-pipeline priming.
2. **Multi-action planning.** The VLM returns a queue of up to 5 actions
   (`PLAN: MOVE_FORWARD, ...`), executed one per step; the queue aborts on
   collision / phase change / failure. ~1.8 actions/call observed.
3. **Plan-history feedback** — recent plans + outcomes fed back into the prompt.
4. **Semantic-class target dedup** (`semantic_classes.py`) — same-semantic-class
   non-target props are deactivated so "go to the bookshelf" is unambiguous.
5. **Render performance** — 960×540 (was 1920×1080, ~4.6× faster), cheap
   filler subframes, timing probes. An episode is now ~3-4 min per 15 VLM
   calls (was unrunnable / >100 min).
6. **Visibility-gated filler frames** — filler frames only render when the
   obstacle runner is in the FPV FOV cone (pure-geometry check, ~2ms/episode).
   Off-screen: filler skipped, `sim_t` still advances (no leap on reappear).
7. **Filler-frame timing model** — runner advances smoothly during VLM
   thinking; `gen_media.sh` builds video from `*_smooth` (fpv) / per-step
   (bird) frames at `SMOOTH_FPS=3` (matches `FILLER_FPS`).
8. **Runner Pose Frozen Coordinates** — Prevented runners from getting stuck in
   corners and pushing the agent into illegal coordinates. Frozen runners retain
   their last valid coordinates while keeping rotation updates active.
9. **Multi-Runner Collision Fall-Through** — Integrated frozen runner coordinate
   checks in the agent overlap pass to resolve sandwich deadlocks correctly.
10. **Automatic Spawn Yaw Calculation (`update_yaw_auto.py`)** — Automates agent
    spawn orientation. L1/L3 tasks face the target center, and L2/L4 tasks
    face 180 degrees away.
11. **Adjusted Spawn Configuration (`benchmark_tasks_0527fix.json`)** — Relocated
    spawns for `case02-L1`, `case03-L1`, and `case09-L1` to prevent spawning
    inside partitions/mezzanines, verified via initial frame FPV and bird-view.

---

## Batch run + aggregate metrics

`bench_batch.py` runs many tasks and aggregates a summary report.

```
# from inside GPU-843 (it docker-exec's the runner; does NOT ssh itself)
python bench_batch.py --level L2 --batch-name batch_L2
python bench_batch.py --all --batch-name full_run
python bench_batch.py --report-only --batch-name batch_L2   # re-aggregate
```

Metrics per task: SR (success), SP (subtask progress), GD (goal distance),
steps, `vlm_calls`, `actions_per_call`, timing breakdown. Aggregated into
`results/<batch>/benchmark_report.json`.

> NOTE: `bench_batch.py` has a `--ssh` arg that is currently a no-op (it
> docker-exec's directly, assuming it already runs on GPU-843). Per-task
> `timeout` is 1800s. It does not pass `MAX_VLM_CALLS` (uses default 50).
> Minor — fix if batch runs need it.

---

## Open TODOs

- **Render noise — PARTLY improved.** The OptiX denoiser was silently failing
  (container lacked `/usr/share/nvidia/nvoptix.bin`). Copying it in (see
  Environment) restored it: whole-image high-freq noise dropped ~20.4 → ~4.6
  (~4.4×) at the same 960×540 / `rt_subframes`, for free. NOTE: flat walls
  denoise nearly perfectly, but complex/dark regions (window, rug) still show
  residual noise (~5-6) — visible when viewed full-size. The denoiser cleans,
  it cannot invent detail; to go further raise `rt_subframes` (cleaner input,
  slower) or raise resolution. Caveat: a container restart loses the file —
  re-copy it (Environment).
- **Agent gets stuck (oscillation).** On some tasks the VLM repeatedly turns
  ±15° and collides without escaping — a VLM-capability issue. Decision was
  **not to intervene** in agent behaviour.
- **`full_task_gen.py`** — a geometric task generator (FOV-frustum visibility,
  on-floor start validation). Its start-generation was unreliable and is
  **shelved**; the original `benchmark_tasks.json` starts are used instead.
  The FOV/raycast visibility check could be repurposed as an informational
  audit of existing tasks.
- **L3/L4** — multi-subtask interaction tasks (PICK_UP/PUT_DOWN/TURN_ON).
  Per-design, the second axis is **subtask count** (1 vs 2-3), not instruction
  length; interaction is just a subtask carrier, not a separate axis.

---

## Key files

| File | Role |
|---|---|
| `bench_runner.py` | runs ONE task in Isaac Sim |
| `bench_batch.py` | batch orchestrator + metric aggregation |
| `bench_helpers.py` | motion sampling, metrics, prompts |
| `semantic_classes.py` | factory → semantic-class map (target dedup) |
| `full_task_gen.py` | geometric task generator (shelved) |
| `gen_media.sh` | builds MP4/GIF from rendered frames |
| `benchmark_tasks.json` | the 40-task benchmark (use this) |
| `validate_and_fix_spawns.py` | PhysX-based spawn validation + reachability check |

---

## Spawn Validation Pipeline (V1 - V4)

We built a robust, automated spawn coordinate validation and auto-fix system (`validate_all_spawns.py`) to eliminate edge-case bugs that previously caused agents to spawn outside rooms, clip into walls, or face blank walls. The following historical progression of fixes led to the current highly stable (V4) state:

1. **V1: L-Shaped Room Void Bug:** Initially used Convex Hull/Bounding Box for the floor, causing agents to spawn in the external "black void" of L-shaped rooms.
   * **Fix:** Ported precise **Concave Boundary** extraction logic from `extract_bev_annotation_data_blender.py`, matching single-occurrence boundary edges of the USD mesh faces to create an exact, non-convex 2D footprint.
2. **V2: `shrink_polygon` Bulge Bug:** An artificial inward shrink algorithm mistakenly pushed "inner corner" vertices outward, causing the safe zone to bulge into the void.
   * **Fix:** Deleted `shrink_polygon` entirely. Relied strictly on the precise concave footprint.
3. **V3: `WALKABLE` Whitelist Exploit:** The PhysX collision sweep originally whitelisted `wall` and `exterior`, causing physical wall collisions to be ignored and allowing spawns just outside the room.
   * **Fix:** Removed structural elements from the whitelist. It now strictly allows only floor-level surfaces (`floor`, `rug`, `mat`).
4. **V4: Forward Clearance Raycast ("Staring at a Wall"):** Agents could mathematically satisfy FOV constraints but end up physically with their camera pressed into a wall or painting (e.g., L2/L4 rotating exactly 180° into a wall, or L1/L3 looking at a target located in a different room).
   * **Fix:** Injected a 1.2m `check_forward_clearance` raycast. For L2/L4, the script tests multiple angles (180, 150, 210...) until it finds a clear view. For L1/L3, if the view to the target is blocked, it rejects the `(x, y)` coordinate entirely and resamples the room.

---

## Known Issues / Future Work

### Frozen Runner Visual Mismatch (Low Priority)
When a runner enters corner-deadlock freeze (`push_agent_if_overlap` Step 3), its
**position** is frozen but **rotation and animation** continue to follow the baked
trajectory. This results in the runner visually rotating and playing walk animations
in-place ("moonwalking"). Ideally, the runner should freeze to an idle pose or loop
the current frame.

**Impact**: Visual only — collision detection and task logic are correct.
**Fix**: Record the freeze timecode and freeze both `timeline.set_current_time()` and
`r*_ops["o"].Set()` to that timecode. Requires non-trivial changes to `pose_runners_at()`.
### Camera Near-Clip Through Thin Geometry
When the agent is pushed very close to thin geometry (window frames, glass doors),
the camera may visually "see through" the geometry due to the 0.3m near-clip distance.
Mitigated by increasing `_sweep_clear` wall buffer from 0.05→0.15m (2025-05-28), but
grazing-angle approaches can still place the camera close to walls.

### Ground Truth Walkable Bounding Boxes (Infinigen Floor Meshes)
For future tasks and more robust spawn generations, we have discovered that every Compiled Scene USDA has a explicit floor geometry prim representing the Infinigen room structure:
* Path: `/World/Env/.../living_room_0_0_floor` (and similarly structured `living_room_0_0_wall` / `living_room_0_0_exterior`).
* This floor mesh's 3D bounding box serves as the absolute ground truth area for valid indoor spawn points (distinct from mezzanine/exterior spaces).
* Any auto-nudge or auto-fix verification tool can query this mesh boundary to determine if a spawn is valid, or fallback/resample within it.

### PathTracing Fallback Lighting Overexposure
In `bench_runner.py`, there is a fallback lighting logic that adds 5 `SphereLight`s (intensity 80000.0) in a fixed `±2m` cross around the agent's spawn point because PathTracing mode otherwise renders pitch-black if native scene lights are inadequate.
Since `scene_objects` bounds are missing from all task specs, this fallback is always triggered.
**Impact**: If the agent spawns near a wall or in a narrow room (e.g. `case02-L3` at `Y=1.25`), the `-2m` offset places one of the 80000.0 intensity lights deep inside or behind the wall geometry. This causes massive ray-bounced light bleeding ("fireflies") and locally blows out the exposure of the room in FPV and Bird views.
**Fix**: Left as a legacy issue as it guarantees scenes are at least visible for VLM evaluation. A future fix should either use a properly ray-cast uniform `RectLight` ceiling panel, or calculate strict scene bounds using the Infinigen floor meshes to ensure fill-lights never spawn inside walls.

---

## References

- [Spawn Validation & Wall-Clipping Fix (2025-05-28)](benchmark_zehao/docs/walkthrough_spawn_validation_0528.md) — wall buffer fix, spawn nudge, FOV gate, BFS reachability
