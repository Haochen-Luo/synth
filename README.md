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

### Cross-Room Target Resolution (Robustness)
Currently, `find_prim_by_factory()` picks the first prim matching a factory name from the
*entire* stage. If the same factory spawns furniture in multiple rooms (e.g. a
`SimpleBookcaseFactory` in both the living room and the hallway), the target could resolve
to a prim outside the agent's floor polygon. V5 validation audits this via
`target_in_room` (all 38 current tasks pass), but for future-proofing:
* **Auto-fix Option 1**: `find_all_prims_by_factory()` + `point_in_polygon_xy()` to prefer
  an in-room instance of the same factory class.
* **Auto-fix Option 2**: For L3 `place_at` targets, nudge `place_at` coordinates into
  the floor polygon (JSON-only edit, no USD modification needed).
* **Auto-fix Option 3**: If no in-room instance of the same semantic class exists, swap
  to a different semantic class target that IS in-room, ensuring the chosen factory isn't
  already used by another task in the same scene (uniqueness constraint).
* **Last resort**: Flag as `UNFIXABLE` for manual review.

---

## Spawn Validation Pipeline (V1 - V5)

V1–V4 history documented below. **V5** adds:

12. **Multi-Ray Line of Sight (LOS) Check (`validate_all_spawns.py`)** — For L1/L3 tasks,
    casts 5 rays from agent camera height to a ±0.3m spread around the target center.
    Passes if *any* ray reaches the target unblocked (existential ∃ check, not universal ∀).
    Prevents agents from spawning behind furniture or L-shaped wall corners that block
    the view of the target they must navigate to.
13. **Target-In-Room Audit** — Validates that each task's first-phase target prim center is
    inside the primary room's concave floor polygon. Flags tasks where the target may be
    in a different room region (hallway, mezzanine).
14. **Bird's-Eye DomeLight Hide (`bench_runner.py`)** — The Infinigen `DomeLight` at
    `/World/Env/env_light` projects an HDR sky texture onto an infinite sphere. With the
    ceiling hidden for bird-eye view, some timecodes expose red/blue sky through wall gaps.
    Fixed by `MakeInvisible()` on `/World/Env/` DomeLights (intensity=0.25, negligible vs
    5× 80000 fill lights). Runner-scoped DomeLights under `/World/Humans/` are kept.

---

## References

- [Spawn Validation & Wall-Clipping Fix (2025-05-28)](benchmark_zehao/docs/walkthrough_spawn_validation_0528.md) — wall buffer fix, spawn nudge, FOV gate, BFS reachability

---

## Session 2026-05-31: Full Dataset Scale-Up & Overnight Pipeline

### Scale-Up: 9 → 122 Scenes, 366 Tasks

Expanded from 9 pilot scenes to **122 physics scenes** from `full_scenarios_extracted/`.
Difficulty levels redesigned:

| Level | Phases | Spawn Facing | Description |
|-------|--------|-------------|-------------|
| ~~L1~~ | ~~1~~ | ~~face~~ | **Dropped** — 90%+ SR with 30B, no discrimination |
| L2 | 1 (navigate) | back | Pure navigation, target behind agent |
| L3 | 2 (pick+place / turn_on) | face | Multi-step interaction |
| L4 | 2 (pick+place / turn_on) | back | L3 + blind start |
| L5 (TODO) | 3 (pick → deliver → turn_on) | back | Multi-target sequential |

Total: **122 × 3 = 366 tasks** (L5 adds ~103 more when implemented).

### Task Diversity

| Dimension | Distribution |
|-----------|-------------|
| Task types | 64.5% pick_place, 34.4% navigate, 1.1% turn_on |
| Room types | 35% living, 30% kitchen, 24% bedroom, 7% dining, 4% bathroom |
| Objects | 18 categories (book, cup, pillow, towel, pot, vase, etc.) |
| Destinations | 13 types (shelf, desk, bed, kitchen island, sink, etc.) |
| Motion types | 62% solo_run, 33% two_runners, 3% run_dance, 3% run_jump |
| L3 instructions | 103/122 unique |

### DomeLight Fix (Regression & Revert)

- **Bug**: Commit `0e1a102` forced DomeLight color to white (`dl.GetColorAttr().Set(Gf.Vec3f(1,1,1))`), causing material appearance changes (e.g., case03 door turned red under uniform white GI).
- **Fix**: Only clear the HDR sky texture (prevents bird-view sky bleed) while preserving the original DomeLight color and intensity. Commit `ab2d177`.

### Shelf Deactivation Physics Bug (Open)

When `deactivate_same_semantic_class` removes shelves, dynamic objects attached to them
(lamps, books) lose support and fall in the first frame. Needs anchoring or removal of
children before deactivation. Visible in case02-L3 (lamp falls).

### Floodfill-Validated Spawn Pipeline

Auto-spawn without validation is unreliable (spawn outside room / behind walls).
Built a 3-phase overnight pipeline:

```
Phase 1: validate_full_spawns.py (~30-40min, Isaac Sim)
  - Loads each of 122 scenes
  - BFS floodfill at 0.25m resolution via PhysX sphere sweep
  - Identifies reachable/unreachable props per scene
  - Finds valid spawn candidates (walkable, 3-7m from target)
  - Caches to spawn_cache/{scene}.json (skip on re-run)
  - Auto-fix: swaps unreachable targets for reachable same-factory prims

Phase 2: generate_validated_benchmark.py (~5s, plain Python)
  - Reads spawn cache
  - Only uses reachable targets
  - Generates benchmark_tasks_full_runner.json with verified agent_start/yaw

Phase 3: 30B batch (~48h)
  - Sequential scene-grouped execution
  - Results in results/fullrun_30B_validated_L2L4/
```

**Launch command** (one-shot, nohup-safe):
```bash
ssh GPU-843 'nohup bash /home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/run_full_benchmark_overnight.sh \
    > /home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/overnight_pipeline.log 2>&1 &'
```

### Qwen3-VL-235B-A22B Comparison

Tested `Qwen3-VL-235B-A22B-Thinking-FP8` on port 8301 (4×H100 TP4).

| Metric | 30B-A3B | 235B-A22B |
|--------|---------|-----------|
| VLM call speed | ~8s/call | ~45s/call (5-6×) |
| PLAN parsing | 95%+ | 96% (22/23) |
| Full-run feasibility | 48h for 366 tasks | Not feasible (~200h) |
| Thinking | No `<think>` tags | Has `</think>` but no `<think>` start tag |

235B suitable for subset comparison only (~30-50 tasks).

### Backward Compatibility

All `bench_runner.py` changes use `.get()` with defaults:
- `task.get("agent_start")` — existing tasks have this → works as before
- `task.get("spawn_facing", "face")` — existing tasks lack this → defaults to face
- `ph.get("target_prim", "")` — existing tasks lack this → falls back to factory search
- Original `benchmark_tasks.json` (40 tasks, 9 scenes) remains fully compatible

### Key New Files

| File | Role |
|---|---|
| `validate_full_spawns.py` | PhysX floodfill spawn validation for 122 scenes |
| `generate_validated_benchmark.py` | Reads spawn cache, generates verified runner tasks |
| `generate_full_benchmark.py` | Initial task generation (pre-validation draft) |
| `run_full_benchmark_overnight.sh` | One-shot overnight pipeline script |
| `benchmark_tasks_full_runner.json` | 366 validated tasks for full benchmark |
| `spawn_cache/*.json` | Cached floodfill results per scene |

### Timing Reference (30B, H100 GPU 4)

| Level | Avg Duration | Steps |
|-------|-------------|-------|
| L1 | 3.5 min | ~30 |
| L2 | 9.1 min | ~80 |
| L3 | 10.1 min | ~100 |
| L4 | 9.0 min | ~100 |
| Overall | 8.0 min/task | — |
| 12h overnight | ~89 tasks | — |
| Full 366 | ~48h (2 days) | — |

### Active Runs (as of 2026-05-31 16:15 UTC)

1. **multiaction_v1_full** (30B, 38 tasks L1-L4 pilot) — ~34/38 done, finishing
2. **qwen235b_L3L4_test** (235B, 18 tasks L3-L4 pilot) — running
3. **overnight pipeline** (floodfill validate → 366-task batch) — Phase 1 running
