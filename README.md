# 4DSynth-Nav Benchmark — Hands-off

VLM indoor-navigation benchmark on physically-accurate 4D scenes. This file is
the session-handoff: read it to pick up where the last session left off.

Code lives in `benchmark_zehao/`. Git branch: **`benchmark-multiaction`**
(commits `2ac89be`, `9b417e4`). The repo is otherwise on a detached HEAD —
stay on this branch.

---

## Environment
conda activate evo_llm 

&& CUDA_VISIBLE_DEVICES=0 vllm serve Qwen/Qwen3-VL-30B-A3B-Thinking-FP8   --port 8300 --tensor-parallel-size 1 --enable-expert-parallel   --limit-mm-per-prompt.video 0 --max-model-len 16384   --gpu-memory-utilization 0.6
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


### VERY IMPORTANT UTILS
please read /home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/UTILS_README.md

---

## Session 2026-06-02: Benchmark-Correctness Audit & Resolver Unification

**Core requirement driving this work:** the benchmark itself must be *provably
correct*, so that when the model fails at navigation it is **necessarily the model's
fault — not a broken task.** Everything below is judged against that single standard.

### Trigger
case003-L3 ("Pick up the bottle and bring it to the kitchen cabinet") renders **no
bottle** in the FPV frame. Root-causing it exposed a *dataset-wide* correctness bug.

### Core Observations

1. **Prim-path convention mismatch — the root cause, affects 606/606 `target_prim`s.**
   In the compiled stages, real geometry lives at
   `/World/Env/Obj_<id>_<Factory>/<Factory>_<facId>_spawn_asset_<spawnId>` (active,
   single underscores). The path stored in every task JSON / manifest / inventory,
   `/World/Env/<Factory>__spawn_asset_<id>_` (double underscore + trailing underscore),
   is **only an inactive `over` (`active=false`, empty bbox)**.
   - case003 evidence: real bottle = `/World/Env/Obj_26523_BottleFactory`
     ([compiled USDA L9887](benchmark_zehao/full_scenarios_extracted/native_case003_official_solo_run_full_physics_scene/compiled_stages/native_case003_official_solo_run_full_physics.compiled.usda#L9887));
     the task's `BottleFactory_9203792__spawn_asset_6999656_` is the inactive over
     ([USDA L586](benchmark_zehao/full_scenarios_extracted/native_case003_official_solo_run_full_physics_scene/compiled_stages/native_case003_official_solo_run_full_physics.compiled.usda#L586)).
     Same for the destination: real cabinet = `Obj_683538_KitchenCabinetFactory`
     ([USDA L10318](benchmark_zehao/full_scenarios_extracted/native_case003_official_solo_run_full_physics_scene/compiled_stages/native_case003_official_solo_run_full_physics.compiled.usda#L10318)),
     task points at the over.

2. **Silent `[5,5]` fallback = the cardinal sin.** When the target prim has no bbox,
   [bench_runner.py:379-386](benchmark_zehao/bench_runner.py#L379) falls back to a
   hardcoded `[5,5]` target. The agent navigated to empty floor; logged `dist=7.68m`
   = exactly the distance from spawn (10.91,9.91) to (5,5). The run *looks* fine
   (frames, log, trajectory) but is meaningless. **A broken task that looks like it
   ran is the worst failure mode a benchmark can have.**

3. **Semantic dedup deactivated BOTH real targets.** Dedup matches targets by exact
   path against `target_prim_paths` ([bench_runner.py:519](benchmark_zehao/bench_runner.py#L519)).
   Because the recorded paths are the wrong (over) names, the real `Obj_<id>` bottle
   *and* cabinet were treated as "same-semantic-class non-target" and deactivated.
   Cascade ([bench_runner.py:543-560](benchmark_zehao/bench_runner.py#L543)) *skips*
   targets ([L545](benchmark_zehao/bench_runner.py#L545)), so a protected target
   resting on a deactivated support is left unsupported (falls in dynamic scenes).

4. **Validator ↔ runner resolve DIFFERENT prims (consistency bug).**
   [validate_all_spawns.py:634](benchmark_zehao/validate_all_spawns.py#L634) resolves
   targets via `find_prim_by_factory` → `stage.Traverse()` only sees *active* prims →
   finds the real `Obj_<id>` geometry → valid bbox, LOS/FOV pass.
   [bench_runner.py:355](benchmark_zehao/bench_runner.py#L355) trusts the JSON
   `__spawn_asset_` path (short-circuit) → inactive over → `[5,5]`. **The validator
   green-lit case003 against a scene the runner never runs.** (Also:
   `find_prim_by_factory` is first-match — with 8 BottleFactory instances it may not
   even validate the intended one.)

5. **Dual source of truth.** Manifest (`__spawn_asset_`, no geometry) vs compiled
   stage (`Obj_<id>`, real geometry). `scene_inventory.json` and `scene_catalog.json`
   are manifest-derived → no positions/bboxes, wrong paths. `scene_object_relations`
   (support) is empty (`physics_support_relation_source: missing_4d_world_physics_support_sidecar`).

6. **Dynamics scan:** 72/122 compiled scenes have real `RigidBodyAPI` (dynamic);
   case003 is static (so its failure is purely the path bug, not settling drift). In
   the 72 dynamic scenes, `resolved_targets` is snapshotted *before* the 100-frame
   warmup ([bench_runner.py:649](benchmark_zehao/bench_runner.py#L649)), so a target
   that settles/falls leaves the recorded coord stale.

7. **`shelves` is a mega-class** ([semantic_classes.py:38-50](benchmark_zehao/semantic_classes.py#L38)):
   countertop, kitchen island, cabinet, bookcase, desk, TV stand all collapse to
   `shelves`. So the canonical kitchen task "pick X off the counter → put in the
   cabinet" almost always makes dedup deactivate the counter (the pickup's support) →
   in dynamic scenes the pickup falls. **The merge is intentional and
   asset-author-confirmed** (cabinet ≈ shelf visually indistinguishable); the class
   MUST NOT be split — disambiguation is non-negotiable for task validity.

### Design Philosophy (north star: validated ⟺ executed ⟺ correct)

1. **Single source of truth = the compiled stage.** What is rendered & measured IS
   the spec's reference. Manifest-derived paths never enter task specs.
2. **Decide at generation, execute at runtime.** All fuzzy decisions (which prim,
   visible?, reachable?, what to deactivate, spawn) are made once at generation
   against the real stage and *baked* into a self-contained task. Runtime becomes a
   thin deterministic executor.
3. **Fail loud, never silently substitute.** Delete the `[5,5]` fallback. A task that
   cannot resolve to active geometry with a valid bbox is never emitted (quarantined
   with a reason). **300 rock-solid tasks ≫ 366 with silent corruption.**
4. **One shared `resolve_target(stage, phase)`** used by generator + validator +
   runner, keyed on the unique factory-instance id (not first-match factory, not the
   over path). Then "validated" provably equals "executed".
5. **Prefer naturally-unique-in-room targets.** Big furniture (sofa/bed/oven/fridge/
   dining table) is usually unique → zero deactivation → no cascade/fall *by
   construction*. Lean on it for L2 nav and L3/L4 destinations.
6. **Pickups are the only small-object targets** (the task requires them).
   Disambiguate a pickup by deactivating only *other small same-class props*
   (cascade-free — nothing rests on a bottle). Never deactivate a pickup's support;
   never let a destination share the support's semantic class (avoid the unresolvable
   counter↔cabinet conflict at generation, not at runtime).
7. **Floor-placed pickups ≤30%** — hard case: forces dynamic camera tilt-down, which
   the VLM handles poorly. Default pickups on furniture near camera height.
8. **Correctness > diversity.** Reducing per-scene task count to guarantee
   uniqueness/validity is the accepted trade.

### Target Architecture

```
compiled stages  ← single source of truth
   │  probe_stage.py   [the only Isaac-dependent step]
   │    traverse ACTIVE Obj_<id> geometry → real path / center+bbox /
   │    z→on_floor / factory→semantic / support via bbox-stack
   ▼
scene_facts.json   ← single derived truth (replaces inventory+catalog)
   │  generate_tasks.py   [pure Python, no Isaac]
   │    enforce ALL constraints: prefer-unique target, support-class
   │    exclusion, floor≤30%, reachable, visible; bake resolved target +
   │    deactivate_prims list + spawn
   ▼
tasks.json   ← one canonical artifact (collapses the ~20 benchmark_tasks_*.json forks)
   │  bench_runner.py   [thin executor: assert prim active+has-bbox, apply
   │    baked deactivate list, run nav; NO factory search, NO [5,5], NO
   │    runtime dedup policy — fail loud on assertion failure]
   ▼
results
```

**Keystone fix** that alone resolves case003 + the validator/runner mismatch: the
shared `resolve_target()` in `bench_helpers.py`.

### Helpful Files (exact locations — so we don't re-hunt)

| Path | Role / key lines |
|---|---|
| [bench_runner.py](benchmark_zehao/bench_runner.py) | runs ONE task. Target resolve L352-387 (explicit-prim short-circuit [L355](benchmark_zehao/bench_runner.py#L355) = bug; `[5,5]` fallback [L379](benchmark_zehao/bench_runner.py#L379)); dedup [L506-574](benchmark_zehao/bench_runner.py#L506); cascade [L543-560](benchmark_zehao/bench_runner.py#L543) (skips targets [L545](benchmark_zehao/bench_runner.py#L545)); warmup [L649](benchmark_zehao/bench_runner.py#L649); loop reads `resolved_targets[cur_phase]` (static) [L1265](benchmark_zehao/bench_runner.py#L1265) |
| [bench_helpers.py](benchmark_zehao/bench_helpers.py) | `get_prim_world_center` [L127](benchmark_zehao/bench_helpers.py#L127) (None on empty bbox); `find_prim_by_factory` [L92](benchmark_zehao/bench_helpers.py#L92) (active-only traverse, first-match) — the natural home for the shared `resolve_target()` |
| [semantic_classes.py](benchmark_zehao/semantic_classes.py) | `SEMANTIC_CLASS` map; `shelves` mega-class [L38-50](benchmark_zehao/semantic_classes.py#L38); `semantic_class_of()` |
| [validate_all_spawns.py](benchmark_zehao/validate_all_spawns.py) | gen-time correctness gate (V5): `find_floor_polygon` [L194](benchmark_zehao/validate_all_spawns.py#L194), `check_forward_clearance` [L376](benchmark_zehao/validate_all_spawns.py#L376), `check_line_of_sight` [L419](benchmark_zehao/validate_all_spawns.py#L419), `check_fov` [L486](benchmark_zehao/validate_all_spawns.py#L486) (L1/L3 visible, L2/L4 hidden), `fix_yaw_for_fov` [L529](benchmark_zehao/validate_all_spawns.py#L529), target resolve [L634](benchmark_zehao/validate_all_spawns.py#L634). **Reuse as the validation half of generation.** |
| [validate_full_spawns.py](benchmark_zehao/validate_full_spawns.py) | PhysX floodfill spawn validation across 122 scenes (overnight; OOM-fixed); writes `spawn_cache/` |
| [probe_and_generate_tasks.py](benchmark_zehao/probe_and_generate_tasks.py) | 05-31 systematic probe + L2/L3/L4 task gen (manifest-based → inherits path bug) |
| [scene_prober.py](benchmark_zehao/scene_prober.py) | loads stage, extracts prim+bbox — but currently grabs `__spawn_asset_` overs → null bbox; **must be fixed to traverse `Obj_<id>` wrappers** (basis for `probe_stage.py`) |
| `generate_validated_benchmark.py`, `generate_full_benchmark.py`, `convert_to_runner_format.py` | task-gen pipeline |
| `full_benchmark_0601.json`, `benchmark_tasks_full_runner.json` | the 366-task sets; **all 606 `target_prim`s use the wrong `__spawn_asset_` style** |
| `full_scenarios_extracted/<scene>/compiled_stages/*.compiled.usda` | **ground truth.** case003 anchors: `Obj_26523_BottleFactory` L9887; inactive over L586; dest `Obj_683538_KitchenCabinetFactory` L10318 |
| `full_scenarios_extracted/<scene>/scene_inventory.json` | manifest-derived asset list (`family_token`, paths); **no geometry; relations empty** |
| `scene_catalog.json` | older catalog; null bboxes; `__spawn_asset_` paths |
| `results/fullrun_235B_validated_0601_180_v2/L3/case003_official_solo_run-L3_20260602_125738/` | the broken example run (`run.log` shows `WARNING: no bbox`, `dist=7.68`) |

### Implementation Results (2026-06-02)

All Phase A/B code landed and verified on Isaac (GPU-180). **Two new findings surfaced
during implementation:**

1. **Targets sealed inside closed furniture.** The case003 bottle *loads fine* (12,010-pt
   mesh, active, visible) but sits **inside a closed `SingleCabinet`** — the occlusion ray
   from the spawn hits the cabinetry at 1.5m, and bbox-containment confirms the bottle is
   *nested within* the cabinet volume (not on-top). Cabinets don't open → it's an
   **intrinsically invalid pickup**, independent of the resolver bug. So case003-L3 had
   *two* defects: the resolver/[5,5]/dedup bug (fixed) **and** an unpickable target.

2. **The LOS gate was silently bypassed.** `validate_all_spawns.check_line_of_sight` (the
   multi-ray ∃ check) never caught the sealed bottle because the old code resolved the
   target via `find_prim_by_factory`/the over → `target_xy=None` → `check_fov` returns
   `"no_target"` = trivial PASS. The shared resolver **re-arms** it (and the validator now
   hard-FAILs on an unresolvable target).

**Verified end-to-end:** resolver → real `Obj_<id>` geometry for both case003 phases
(Step 0 dist 2.85m, not 7.68 to [5,5]); targets not deactivated; runner applies baked
`deactivate_prims` (e.g. 6/6, 15/15) and skips legacy dedup; generation drops enclosed
targets + prefers unique-in-room (deact=0 where possible) + bakes real target paths;
spawn-validation fills `agent_start` with clearance + LOS (11/12 tasks; 1 dropped = no
valid spawn); sanity batch renders the real target in-frame (case003 picks the **visible
can on the counter**, not the sealed bottle).

**New files:**

| File | Role |
|---|---|
| [probe_stage.py](benchmark_zehao/probe_stage.py) | Isaac: traverse ACTIVE Obj_ geometry → `scene_facts.json` (real paths, bboxes, `on_floor`, support). NOTE: floor-z detector must match `_floor` meshes and exclude `FloorLamp`. |
| [generate_tasks.py](benchmark_zehao/generate_tasks.py) | pure-Python: enclosure filter (drops targets sealed in closed containers) + prefer-unique + support-class exclusion + floor≤30% + bake real target / `deactivate_prims` |
| [validate_generated_spawns.py](benchmark_zehao/validate_generated_spawns.py) | Isaac: fill `agent_start`/`agent_yaw` with clearance + forward-clearance + LOS (drops no-LOS tasks) |
| `full_scenarios_extracted/<scene>/scene_facts.json` | the single derived source of truth (replaces manifest inventory/catalog for generation) |

**Pipeline:** `probe_stage.py` → `scene_facts.json` → `generate_tasks.py` →
`benchmark_tasks_generated.json` → `validate_generated_spawns.py` (or floodfill
`validate_full_spawns.py` for denser spawn search) → `benchmark_tasks_generated_spawned.json`
→ `bench_runner.py`.

**Update (2026-06-03) — reachability, fallback, parallelism:**
- **Reachability-at-generation:** `probe_stage.py` now floodfills once per scene (two-height
  sweep, aligned with `scratch_archive/validate_and_fix_spawns.py`) and bakes `reachable`/
  `reach_dist` per object; `generate_tasks.py` selects only reachable targets → tasks
  navigable by construction (e.g. case003 swaps the caged bottle for a reachable one).
- **`validate_all_spawns.py` is the authoritative gate** — I added floodfill reachability
  (it lacked a navigable-path check; LOS alone passed case003's narrow gap), `agent_start=None`
  handling (generates the spawn), and `VAL_VALID_OUT` (writes only PASS/FIXED, drops the rest).
  ALL pre-existing V1–V5 checks kept. Verified: case003-L3/L4 correctly dropped (UNREACHABLE
  / target outside floor polygon); case01-L3 spawns in-room (void fixed).
- **`place_at` surface fallback** (`generate_tasks.synth_pickup_on_surface`, NEEDS a validation
  run): when a scene has no valid existing pickup, relocate a distinct clear-noun object onto a
  **reachable furniture top at camera height (0.55–1.05m)** — never the floor (floor forces
  camera tilt-down, the VLM-hard case).
- **Known gap:** probe seeds floodfill from object centroid (not room-grounded) → over-reports
  reachable for other-room objects; validate (floor-polygon seeded) is authoritative. Fix: seed
  probe from the floor polygon, or fuse probe+generate+validate into one per-scene pass (load +
  floodfill once → ~2× faster, removes the duplicate floodfill).

**Remaining (production):** construction needs **no vLLM** (pure Isaac geometry) → close the 30B
on `:8300` to free GPU 0, run construction across **GPU 0 + GPU 3** (both ~80 GB) as **4–6
sharded Isaac workers** (probe via `SCENES=` subsets; validate by splitting the generated JSON;
merge). Then the model run. Cosmetic: fill-light overexposure on some spawns. **Nothing committed
yet** (branch `benchmark-multiaction`). Handoff details: memory `project_zehao_pipeline_v2`.

---

## Session 2026-06-03: Parallel Construction + Reachability Solved (support-edge)

### What was built
The corrected pipeline (Session 2026-06-02) was run **at scale, in parallel, across GPU 0 + GPU 3**,
and the reachability model was made physically correct. **All committed** on `benchmark-multiaction`:
`a2b30b0` (core pipeline) → `966d381` (room-grounded probe + matched thresholds) →
`1974e78` (fallback pickup-reachable surface) → `d85f297` (support-edge reach).

### Pipeline + how to run it (no vLLM needed for construction)
```
probe_stage.py (Isaac, 1 floodfill/scene)  → full_scenarios_extracted/<scene>/scene_facts.json
  (real Obj_<id> path, bbox/center, on_floor, support + support_he, reachable + reach_dist)
generate_tasks.py (pure Python)  → benchmark_tasks_generated.json  (+ dropped_tasks.json)
validate_all_spawns.py (Isaac, AUTHORITATIVE, VAL_FIX=1)  → benchmark_tasks_generated_validated.json
bench_runner.py  (shared resolve_target + baked deactivate_prims + support-edge success)
```
**6-way parallel** (the construction runs that produced the dataset):
- 2 Isaac containers: `vlm-jupyter-180` (GPU 0) + `vlm-isaac-g3` (GPU 3, spun from
  `nvcr.io/nvidia/isaac-sim:4.5.0`, `--network host --gpus device=3`, repo bind-mounted, default
  entrypoint). 3 worker procs each. `CUDA_VISIBLE_DEVICES=0` inside each (each sees only its GPU).
- Helper scripts (host, in `benchmark_zehao/`, gitignored): `run_build_v2.sh` (full),
  `run_incremental.sh` (re-process only failed scenes), `_split_tasks.py`, `_merge_stats.py`,
  `_merge_incremental.py`. Effective ~3–4× (3 Isaac/GPU contend; probe+validate are 2 passes).

### Reachability model (the core correctness work)
- **Single source of truth = compiled stage**; `resolve_target()` maps task `target_prim` → real
  active `Obj_<id>` geometry (shared by runner+validator). Fail-loud (no `[5,5]`).
- **Room-grounded floodfill**: probe seeds from the *primary (largest) floor mesh* center (not the
  object centroid) — matches validate; two-height sweep (aligned with `scratch_archive/validate_and_fix_spawns.py`).
- **Thresholds match validate radii**: pickup 1.0m, dest 1.5m (via baked `reach_dist`).
- **Support-edge reach (key):** a pickup's reach/reachability/success is measured to the EDGE of the
  furniture it rests on (`dist_to_center − support_he`) — you reach a tabletop object by walking to
  the table; LOS/FOV still gate on seeing the object. **Floor pickups have `support_he=0`, so it
  degenerates to the object's own edge** (walk right up to it). Implemented in probe (`support_he`),
  generate (`pickup_reach_ok`, baked `reach_half_extent`), validate (`first_target_half_ext` +
  expanded `rtol`), runner (PICK_UP edge-distance).
- **Enclosure filter**: pickups sealed in closed cabinets are dropped (can't see/reach).
- **Clear-noun pickups only** (no ambiguous "trinket"). **Floor pickups ≤30%** (hard tilt-down case).
- **place_at fallback**: scenes with no valid existing pickup relocate a distinct clear-noun object
  onto a reachable camera-height surface (0.55–1.05m), never the floor.

### Dataset state (as of handoff)
- v1 facts run: **235 valid tasks**, 39/52 scenes zero-pickup → exposed the probe↔validate
  reachability disagreement (now fixed).
- v1 facts → room-grounded + matched-threshold incremental: 268 valid.
- **Support-edge incremental (final): 272 valid** (103 L2 / 84 L3 / 85 L4). Recovered 15/52
  zero-pickup scenes (vs 14 before → +1 scene, +4 tasks); **37 still zero-pickup**. Support-edge was
  primarily a **correctness** win (success metric now physically sensible — reach the support, not
  the object center) rather than a count gain — residual scenes are limited by *object availability*
  (~15 have **no portable object at all**), not reach tolerance. Canonical dataset:
  `benchmark_tasks_generated_validated.json`.
- ~~Residual 37 zero-pickup are at the *selection* ceiling~~ — **this was WRONG** (corrected below
  in the 2026-06-03 cont. session): most of the 37 were *false drops* from a validate-side floodfill
  seed bug, not a true ceiling.

### Remaining
1. **Efficiency**: fuse `probe`+`validate` into one per-scene pass (load + floodfill **once** →
   ~2× and removes the double floodfill + the residual probe↔validate seam).
2. **Model run** (the eval): restart the 30B vLLM on `:8300`, run `bench_runner` over the validated
   dataset (batched on GPU 0+3).

## Session 2026-06-03 (cont.): validate-seed bug, 60/40 task mix, 333-task dataset

### What was investigated & fixed
User asked two questions that exposed real issues:

**1. "Why did the place_at fallback not recover all zero-pickup scenes?"**
Classifying the 37 zero-pickup scenes (after the support-edge incremental) by root cause showed the
fallback was *not* the problem — and the earlier "37 = selection ceiling" claim was **wrong**:
- **15** scenes: truly **no portable object at all** → fallback has nothing to relocate (real ceiling).
- **18** scenes: generate *did* emit a valid `[PICK_UP, STOP]` task, but **validate dropped it** as
  "target UNREACHABLE". Isolated Isaac re-run of `case33` (24 reachable portables, 10 cam-height
  surfaces) proved it: probe saw **429 reachable cells**, validate saw only **14** — validate's
  floodfill seed was trapped in a cluttered corner.
- 3 + 1: no reachable surface / no destination.

**FIX A — room-grounded validate floodfill seed (commit `aff85b7`).** `validate_all_spawns.py`
seeded its floodfill from the *first clear cell scanning from the floor-bbox corner*, which lands in
cluttered corners and traps the BFS in a tiny pocket. Now it seeds like `probe_stage.py` (commit
`966d381`): floor-polygon **centroid + offsets** first, then a coarse in-polygon grid, flood from each
clear candidate, **keep the largest reachable set**. Verified on case33: **14 → 429 cells, task now
PASSES** (was dropped). This is a *correctness* fix (validate now sees the same room the probe does),
**not** a relaxation — collision/buffer model is unchanged (agent-radius 0.40 two-height sphere-sweep,
360° spawn clearance, 1.2m forward raycast).

**Same-room semantics:** user requires agent + target in the **same room** (cross-room search deferred).
These scenes are single-room, so the probe's full-room reachable set is the *correct* answer; matching
validate to it is consistent with same-room. Multi-room layouts: only the largest connected region
yields tasks (the rest is implicitly dropped) — acceptable until cross-room is added.

**2. "What's the L3 phase-1 task-type distribution? All pickups?"**
Yes — L3 was 100% `[PICK_UP, STOP]` and the pickup noun skewed ~49% to "book stack".

**FIX B — L3/L4 phase-1 mix 60% nav / 40% pickup (commit `d77a6cd`).** Added a two-waypoint
navigation variant `[STOP, STOP]` ("first go to X, then go to Y") alongside the pickup variant, with a
global `mix_state` counter targeting ~60% nav / 40% pickup across scenes (whichever is feasible &
under quota). `task_type` is now `navigate` (L2) / `pick_place` / `two_nav`. (User decided: **no**
"other interaction" / TURN_ON for now — TURN_ON works & is visually verifiable, the runner spawns a
real SphereLight, but its difficulty ≈ PICK_UP and it adds light rather than toggling the fixture.)

### Final dataset (full re-run, Fix A + Fix B)
Full `generate` (345 tasks over 122 scenes, mix hit **exactly 60% nav / 40% pickup** = 68/46 scenes)
→ parallel `validate` (6 shards, GPU0+GPU3, ~10 min):
- **333 validated tasks** (113 L2 / 110 L3 / 110 L4), 333/345 = **96.5%** pass.
- **zero-pickup scenes: 37 → 7** (the 7 are the true no-portable ceiling).
- scenes with ≥1 valid task: **113/117**.
> NB: 272 (prior) was an incremental splice (235-baseline + failed subset); 333 is a clean **full**
> re-run of all 122 scenes under the latest code. Not a strict same-pipeline delta, but 333 is the
> self-consistent canonical dataset → `benchmark_tasks_generated_validated.json`.

### Sanity-check (5 spawn frames rendered, MAX_STEPS=1, dead VLLM_URL)
3 two_nav + 2 pickup L3 frames: all spawn in open floor (not corners), targets visible, geometry sound;
confirmed bench_runner uses the validate-assigned `agent_start`. **Known cosmetic issue:** scenes with
`MirrorFactory`/`WindowFactory` render **black panels** in FPV. Root cause located: the Infinigen
scenes carry a **MirroredBall-format environment map** which the **Isaac RTX renderer does not support**
(`[Error] [rtx.scenedb.plugin] MirroredBall environment format not supported yet` — fires **once per
scene**, in *every* scene, independent of mirror count → renderer-level, **NOT** a H100 issue, **NOT** a
spawn/geometry issue). Renderer discards the env map → mirrors have nothing to reflect and windows have
no backdrop → black. **Decision: record as a known eval input-noise variable, do not fix before eval.**

### Remaining (unchanged)
1. Efficiency: fuse probe+validate into one per-scene pass.
2. **Model run (the eval)**: restart 30B vLLM on `:8300`, run `bench_runner` over the **333** tasks on
   GPU 0+3.
3. Sanity-check a few validated L3 frames (target visible, agent at the support edge).

## Session 2026-06-03 (cont. 2): L3 visibility definition + the "invisible plant" investigation

### The question that drove this
During a pre-eval sanity-check, the phase-1 target of an L3 task (`case010_official_run_dance-L3`,
"First go to the large plant, then go to the shelf") was **not visible in the step-0 FPV frame** even
though `spawn_facing=face` and the validator passed it. This raised two questions: *(a) what exactly
defines L3 visibility?* and *(b) is there a rendering bug?*

### The "invisible plant" — diagnosed: **furniture occlusion** (an earlier "low-contrast" call was WRONG)
Geometry (agent `(5.93,3.93)` yaw `18.5°`, plant center `(11.01,5.64,0.43)`, dist 5.4 m):
- **Horizontal:** plant bearing rel-to-yaw = **0.0°** → dead center of the frame.
- **Vertical:** plant pitch rel-to-camera = −2.1° → inside the VFOV; projects to screen ≈(270,191) of
  540×360, ~23×31 px.
- **bbox-in-FOV:** all **8/8 bbox corners inside the view cone → 100%**.
- **BUT the plant is hidden behind a bed.** This scene has *two* `BedFactory` instances; the one at
  center `(10.1,4.1)` (top z **1.68 m**) sits at **4.2 m** — *in front of* the plant at 5.4 m. Ray-marching
  camera→plant-center passes **straight through that bed's bbox** at 3.35 m, height z≈0.86 m (and the
  sightline to the plant *top* is blocked too). On screen the bed's top edge is at +11.7° while the plant
  centre is at −2.1°, so the plant sits squarely in the screen band the bed body covers. The fresh eval
  step-0 frame confirms it: dead-centre you see the **bed**, no plant.
- **The VLM's own trace confirms occlusion, not faintness:** across steps 17–38 it repeatedly reasoned
  *"the plant isn't visible yet … I should turn to scan for the plant … turn left to reveal more of the
  room."* It only **finds the plant by ~step 21** after moving to `(8.34,4.96)`, turning yaw 18°→48°, and
  closing to 2.76 m — i.e. by **stepping around the bed**. (A small low-contrast speck would not require
  20 steps of exploration; a hidden target does.)
- **Conclusion: geometric occlusion by a bed — NOT low-contrast, NOT a render bug, NOT a stale-pose bug.**
  ⚠️ The previous commit's "size + low contrast, not occlusion" claim was a **diagnosis error**: that scan
  only searched for occluders within ±10–12° of the plant's bearing *using object centers*; the bed's
  *center* is at −16.3° offset so it was missed, but the bed *body* still crosses the sightline. Always
  test occlusion with **bbox extents**, not centers.

### Three *different* visibility notions (don't conflate them)
The `case010` saga showed that "is the target visible at spawn?" decomposes into **three independent**
gates — and our current L3 gate only implements the first:
- **(1) bbox-in-FOV (angular cone coverage)** → catches a target **clipped off the frame edge / yaw aimed
  wrong**. (e.g. `case008...-L3` wine glass: horiz rel **−105.7°** → 0% in FOV → a real yaw bug.)
- **(2) angular diameter / pixel size** → catches a target that is **in-frame but too far/small to read**
  (a target 100% in FOV can still be only ~20 px). Not gated.
- **(3) occlusion (line-of-sight)** → catches a target that is **in-frame and big enough but hidden
  behind furniture** (e.g. `case010` plant: 100% in FOV, but a bed sits on the sightline). Not gated.
  This is the one that actually explained `case010`.

A scan of all 220 L3/L4 phase-1 targets (gate 1 only):
- **bbox-in-FOV < 50%:** L3 = **1/110** (only `case008`, the yaw bug); L4 = 110/110 and L2 = 113/113
  (this is **correct** — L2/L4 are *designed* to face away from the goal). So the bbox gate is a clean
  near-no-op on L3 that only flags genuine yaw errors.
- **angular diameter:** median **6.9°**, and **24.5%** of phase-1 targets are < 4° (small portables
  like wine glass/cup at distance). Rendered at 540×360 (6 px/°) the *smallest* target is still
  **9.2 px** — i.e. **nothing is sub-pixel / degenerate**.
- **occlusion:** **not yet scanned** (a login-node raycast over all 110 L3 phase-1 targets is the
  outstanding to-do; `case010` is one confirmed case, count unknown).

### L3 definition (current) + open occlusion question
**L3 = a two-phase task whose phase-1 target is within the camera view frustum at spawn**
(`bbox ≥ 50% inside the ±45° H / ±29° V cone, eye 1.58 m, pitch −10°`), phase-2 is a navigation goal.
Rationale: *if it's in the frustum, the agent only has to nudge/rotate/step to bring it into clear view*
— that matches L3's intent ("start already oriented toward the goal"). We deliberately **do NOT** add an
angular-diameter / pixel-size gate (gate 2): far-but-small targets are **kept** as legitimately harder L3.
L2/L4 remain "phase-1 *not* in the frustum" (must explore). Phase-1 may be a navigation waypoint
(`two_nav`, ~60%) or a `PICK_UP` (~40%); both keep the same two-phase shape.

> **OPEN: occlusion gate (gate 3) is undecided.** `case010` proves an in-frustum target can be fully
> occluded by furniture at spawn (the VLM needed ~20 exploration steps to round the bed). Options:
> (a) keep occluded-but-in-frustum as legit hard L3 ("step aside, it's revealed") — matches the
> nudge-to-see rationale; (b) add a line-of-sight gate and re-spawn/drop occluded targets — stricter,
> truest to "starts facing a *visible* target", but **needs re-rendering which is currently blocked
> (GPU-180 reclaimed)**. Decision pending a login-node occlusion scan to size the impact.

> Wording fix TODO: "large plant" overstates a 0.5 m potted plant — rename the NL phrase to "plant" /
> "potted plant" (cosmetic, in `FACTORY_TO_NL`); does not affect geometry or validation.

### Eval launched (2026-06-03 18:01 UTC)
Both models over the 333-task set, in parallel, in detached tmux on GPU-180 (survives SSH drop):
- `tmux eval30b` → **Qwen3-VL-30B-A3B-Thinking** @ `:8300`, render container `vlm-jupyter-180` (GPU 0),
  log `eval_30b_v2.log`.
- `tmux eval235b` → **Qwen3-VL-235B-A22B-Thinking** @ `:8301`, render container `vlm-isaac-g3` (GPU 3),
  log `eval_235b_v2.log`.
- vLLMs are **host tmux processes** (conda `evo_llm`), NOT in Docker: 30B `CUDA_VISIBLE_DEVICES=0
  --tensor-parallel-size 1`, 235B `CUDA_VISIBLE_DEVICES=4,5,6,7 --tensor-parallel-size 4`.
- GPU 3 is protected from external grab by a resident VRAM warmer (`vlm_kv_cache_warmer.py`, ~40 GB) in
  tmux `query_vlm` — Isaac's render container holds only ~3.5 GB, so without it an external 70 GB job
  could seize GPU 3 during a container-restart window.
- Per-task isolation + crash recovery: `bench_batch.py` docker-exec's one task at a time and, on an
  Isaac segfault signature, `docker restart`s the container + restores `nvoptix.bin` + retries once
  (the known "vlm-jupyter degrades to segfault on long runs" issue).
- Smoke-tested first (1 task/model in parallel): both produced valid `results.json`, model name
  auto-detected from `/v1/models`, episodes terminated cleanly at the 50-VLM-call cap.

Expected wall-clock: a hard L3/L4 episode runs to the 50-VLM-call cap ≈ 6–10 min, so ~30–50 h/model;
the two models run on separate GPUs in parallel.

### Partial results snapshot (eval cut short — GPU-180 reclaimed 2026-06-04)
**GPU-180 was reclaimed before the run finished**, so the eval stopped at a partial sample and **no
further rendering is possible** (login-node / inspect-only from here). The per-task `results.json` files
that did complete are synced locally under `benchmark_zehao/results/eval_{30B,235B}_333_v2/`. Aggregated
from what finished (`success`, mean `subtask_progress`, mean `goal_distance_m`, mean collisions/episode):

**Qwen3-VL-30B-A3B-Thinking — 45 / 333 tasks done**

| level | n | SR | subtask | goalDist | timeout | col/ep |
|-------|---|------|---------|----------|---------|--------|
| L2 | 16 | 18.8% | 18.8% | 4.46 m | 0% | 42.9 |
| L3 | 15 | 6.7% | 13.3% | 3.79 m | 0% | 36.5 |
| L4 | 14 | 0.0% | 3.6% | 3.71 m | 0% | 36.2 |
| **ALL** | **45** | **8.9%** | 12.2% | | | |

**Qwen3-VL-235B-A22B-Thinking — 16 / 333 tasks done**

| level | n | SR | subtask | goalDist | timeout | col/ep |
|-------|---|------|---------|----------|---------|--------|
| L2 | 6 | 0.0% | 0.0% | 5.17 m | 33% | 45.5 |
| L3 | 5 | 0.0% | 20.0% | 2.65 m | 40% | 35.0 |
| L4 | 5 | 0.0% | 0.0% | 3.83 m | 60% | 38.4 |
| **ALL** | **16** | **0.0%** | 6.2% | | | |

⚠️ **Read these as preliminary only** — 30B covers 13.5% of the set, 235B only 5%, and both samples are
the *first* (alphabetical) tasks, not a random draw. Three caveats before drawing conclusions:
1. **Collisions/episode = 36–45 is anomalously high** — likely either real wall-hugging/stuck behaviour
   or a counter that tallies "sliding along a wall" every frame. **Un-audited; may be depressing SR.**
   (Next inspect-only task: decompose `collisions.json` — clustered-at-a-few-steps = stuck, vs
   evenly-spread = counting artifact.)
2. **Opposite failure modes:** 30B has 0% timeout yet low SR → it `DONE`s early without reaching the
   goal; 235B has 33–60% timeout → it burns steps without arriving (its L3 mean goalDist 2.65 m is
   actually the *closest* of any cell). So 235B's 0% is more "cautious-but-stuck" than "worse model."
3. Sample is too small and too front-loaded to compare the two models.

## Session 2026-06-04: clean sync to a standalone repo (`Haochen-Luo/synth`)

### Why
`zehao_task` lives *inside* the Puppeteer repo (`/home/qi/hc/Puppeteer`, origin
`4DSynthesis.git`, upstream `Seed3D/Puppeteer`) but is semantically a sibling of Puppeteer,
not a child. To deploy to a **new machine with no scp/rsync path**, we split `zehao_task` into
its own repo so the new machine clones a clean root with no Puppeteer 3D-asset baggage.

### Method — `git subtree split` (history-preserving, no Puppeteer code)
```
# on benchmark-multiaction (restore point: commit 93660b4)
git add <curated untracked files>           # the 333 dataset + launch/host scripts were UNTRACKED
git commit -m "track files needed for synth standalone sync"
git push origin benchmark-multiaction       # safety: old repo keeps everything
git subtree split --prefix=zehao_task -b zehao-standalone   # new branch, root = zehao_task/
git remote add synth https://github.com/Haochen-Luo/synth.git
git push synth zehao-standalone:main
```
- **All operations are reversible & non-destructive** — `add`/`commit`/`subtree split`/`push`
  never modify or delete working-tree file *content*. subtree split only *creates* a new branch
  off existing history; the `benchmark-multiaction` branch and working tree are untouched.
  Restore point: `git reset --soft 93660b4`. (Never `git reset --hard`.)
- **Data is auto-excluded:** `results/`, `full_scenarios_extracted/`, `native_case*/`, render
  frames, etc. were **never git-tracked**, so the split repo is clean by construction — no
  `.gitignore` surgery needed. Large data is re-downloaded from Google Drive on the new machine.

### What was curated INTO the sync (key finding: the canonical dataset was UNTRACKED)
`benchmark_tasks_generated_validated.json` (the 333-task canonical set), the v2 launch scripts,
the GPU-180 host scripts (`_split_tasks.py`/`_merge_*.py`/`run_build_v2.sh`), `scratch_archive/
validate_and_fix_spawns.py` (floodfill alignment reference, README §Session 2026-06-03), and
`extract_bev_annotation_data_blender.py` (concave-boundary logic source, README §V1) were all
**untracked** and had to be `git add`'ed before the split. Core runtime (`bench_runner`,
`bench_batch`, `bench_helpers`, `semantic_classes`, `probe_stage`, `generate_tasks`,
`validate_all_spawns`, `vlm_kv_cache_warmer`) was already tracked.

### New-machine landing checklist
1. `git clone https://github.com/Haochen-Luo/synth.git` (root == old `zehao_task/`).
2. Re-download `full_scenarios_extracted/` (+ per-scene `scene_facts.json`) from Google Drive.
3. **Fix 24 hardcoded `/home/qi/hc/Puppeteer/...` paths** (audited via `git grep`): runtime code
   (`bench_batch.py:14`, `bench_runner.py:1846`, `probe_sky.py`, `generate_*`/`probe_*` BASE/OUT_DIR,
   the `*.sh` WORKDIRs) + `scenes_base` in the `benchmark_tasks_*.json`. Recommend collapsing to a
   repo-root-relative base (`Path(__file__).parent`) or an env var. Stale `zehao_new_folder/...`
   refs (`check_dancer_bbox.py`, `vlm_nav_benchmark.py`, `vlm_nav_interactive.py`) point outside
   `zehao_task` and are legacy — drop or repoint.
4. Rebuild the conda/Isaac env; smoke-test one task before a batch.

## Session 2026-06-06: Cross-Machine Deployment (HK A100) + Repo Architecture Clarification

### Repo architecture

`synth` (this repo) is a **standalone sibling** of Puppeteer, NOT a child. History:

```
Puppeteer (4DSynthesis.git)
  └── zehao_task/  ← benchmark code lived here on branch `benchmark-multiaction`
        │
        │  git subtree split --prefix=zehao_task → synth-main branch
        ▼
synth (Haochen-Luo/synth.git)   ← THIS REPO, clean standalone
  ├── benchmark_zehao/           ← all benchmark code
  ├── README.md                  ← this file
  └── ...
```

- **`benchmark-multiaction`** (in Puppeteer): the original working branch. Contains full
  Puppeteer 3D-asset history + benchmark code. **Considered buggy / legacy** — do NOT
  develop on it further.
- **`synth-main`** (in Puppeteer): a clean extraction via `git subtree split`. Root = old
  `zehao_task/`. This was pushed to `Haochen-Luo/synth.git` as `main`.
- **`/home/qi/hc/synth`** (SG): a fresh `git clone` of `synth.git` — the canonical
  development copy. All new work happens here, NOT in Puppeteer.
- Commit `af74d48` made all runtime paths self-locating (`os.path.dirname(__file__)`) so
  the repo runs from **any clone location** with zero per-machine config edits.

### Cross-machine deployment

| Node | Role | synth repo | Scene data | Isaac Sim |
|------|------|-----------|-----------|-----------|
| **SG** (login) | Coding, git hub | `/home/qi/hc/synth` | N/A (no GPU) | N/A |
| **HK** (liuqi-g1) | Isaac rendering, eval | `/home/liuqi/hc/synth` | ✅ 122 scenes extracted | ✅ Docker (`vlm-jupyter`) |
| ~~GPU-180~~ | ~~was primary~~ | ~~reclaimed 2026-06-04~~ | — | — |

**HK node setup (done 2026-06-06):**
- Deployed `haochen` GitHub deploy key to `/home/liuqi/.ssh/haochen`
- Configured SSH to use port 443 (`ssh.github.com`) since GitHub port 22 is blocked on HK
- `git clone git@haochen:Haochen-Luo/synth.git` → verified at commit `c86a0c5`
- **Docker**: `isaac-sim:4.5.0` container (`vlm-jupyter`) running with GPU 0, `nvoptix.bin` copied, ffmpeg installed.
- **Data**: Extracted 122 `full_scenarios_extracted` scenes locally on HK from Google Drive archives.
- **VLM Server**: Created `vlm` conda env with `vllm==0.14.0` (aligned with SG) and generated `serve_vlm.sh` for GPU 1.
- **Verification**: Successful "dry-run" using `probe_sky.py` via Docker exec verified Isaac Sim runs headless and finds the stage without errors.

**Code sync workflow:**
```
SG: edit → git push
HK: git pull                    # instant, ~seconds
```

### Latest commits summary (post-split, on synth main)

The top 4 commits address **systematically-undercounted L3/L4 success rates** discovered
during the partial eval (45 + 16 tasks, GPU-180, before it was reclaimed):

| Commit | Fix | Impact |
|--------|-----|--------|
| `af74d48` | All runtime paths self-locating (`__file__`-relative) | Zero-config on any fresh clone |
| `d1bcc94` | **Arrival-rescue**: trailing DONE/PUT_DOWN in a plan queue was silently discarded on collision; DONE confirm gate was re-querying when already in-range; DONE/PUT_DOWN now interchangeable for arrival phases | Recovers false-failures where agent WAS within goal radius |
| `737a516` | **PUT_DOWN inventory gate**: a stray PUT_DOWN on an empty-handed `two_nav` task was falsely completing it | Prevents false-positive completions |
| `c86a0c5` | Comment-only: documents why `inventory` is the gate (not task-type) | Prevents future "fix" of a non-bug |

**Status: Dry-run successful, pending full pipeline test**. The Isaac Sim container operates successfully, but the interaction loop with VLM needs a final smoke test.

## Session 2026-06-07: Parallel Execution & Anomaly Analysis

### Parallel Benchmark Scaling
- Upgraded the HK evaluation from sequential to **5-worker parallel execution** (`parallel_launch.sh` / `parallel_split.py`). 
- 5 `isaac-sim` containers run concurrently on GPU 0.
- `docker run` uses `--network host` to allow all containers to reach the vLLM server at `localhost:8300` on GPU 1, fixing `Connection refused` errors.
- Added `os.umask(0o000)` inside `bench_runner.py` to prevent `root`-created Docker output files from locking out the host user.
- **Throughput**: Scaled from ~4.5 tasks/hour to ~9.4 tasks/hour. The bottleneck shifted from GPU 0 idle time to GPU 1 (VLM) batching saturation.

### 30B Model Anomaly Analysis (Camera Clipping)
During the parallel run, visual inspection of 140 tasks revealed ~7% contain **pure black** or **pure white (overexposed)** frames in `vlm_nav_frames_fpv`.

**The Problem (Camera Clipping):**
- Pure black frames occur when the agent's camera clips inside solid geometry (e.g., cabinets, walls).
- Pure white frames occur when the camera clips directly into a bright light source (e.g., vanity lamps).
- **Why this happens:** The collision capsule has a radius of `0.40m`, and the physical raycast sweep happens at `z=0.5m` and `z=1.0m`. However, the FPV camera is positioned at `z=1.6m` (Eye Height) with a `+0.01m` forward offset. If the agent hits an overhanging object (like a wall cabinet) that exists at `z=1.6m` but not at `0.5m-1.0m`, or if the wall is uneven, the camera physically breaches the mesh surface.

**Conclusion & Impact:**
- This is **not a benchmark bug**, but a standard 3D camera clipping phenomenon caused by the agent driving itself hard against an obstacle. 
- It does **not** cause false evaluations. The agent receives textual feedback (`MOVE_FORWARD blocked by an obstacle`) when this happens. A capable agent should use this feedback to turn around and escape the collision loop.
- The 30B model consistently fails to escape these loops (84% of tasks exhaust the 50 VLM call budget), highlighting a severe deficiency in spatial reasoning and recovery, particularly in L2 tasks (back-spawn) where SR is currently ~4.3%.

### Next Steps

1. **Wait for completion**: The 333-task parallel benchmark is currently running. Expected completion: ~24 hours.

> NOTE (superseded 2026-06-08): step 1 above was interrupted — the run was paused
> at 224/333 and resumed under the wall-clip fix into a separate folder. See the
> 2026-06-08 session below for the authoritative current state.

---

## Session 2026-06-08: Wall-clip ROOT CAUSE found + dataset SPLIT INTO TWO BATCHES

> ⚠️⚠️ READ THIS BEFORE TOUCHING ANY 30B RESULTS ⚠️⚠️
> The eval results now live in **THREE separate folders**: one BUGGY baseline
> (`eval_30B_333_v2`, 224) and two FIXED folders whose union is the real dataset
> (`eval_30B_333_remaining_fixed` 109 + `eval_30B_333_rerun_fixed` 224 = 333).
> The buggy baseline and the fixed folders use DIFFERENT collision physics and
> must NOT be merged. See "Dataset state — THREE folders" below.

### The 2026-06-07 "camera clipping, not a benchmark bug" conclusion was WRONG
The previous session blamed the black/white FPV frames on camera-height clipping
(camera at z=1.6 above the z=0.5/1.0 collision sweeps) and declared it "not a
benchmark bug". **That was incorrect.** A multi-round investigation (Isaac
geometry probes + deterministic replay of the exact failing poses) found the
true root cause. Full evidence + every wrong hypothesis along the way:
`benchmark_zehao/docs/bw_samples_analysis/ANALYSIS.md`.

### TRUE root cause — WALKABLE substring false-match (a real benchmark bug)
The collision-ignore list used **substring** matching:
```python
WALKABLE = ("floor","ground","rug","blanket","towel","mat")
if any(w in hit_path for w in WALKABLE): continue   # BUG
```
- `"floor"` is a substring of **`FloorLampFactory`** → a floor lamp was treated
  as walkable.
- `"mat"` is a substring of **`MattressFactory`** → a mattress was treated as
  walkable.
So the agent walked straight THROUGH floor lamps and mattresses. Worse:
`sweep_sphere_closest` returns only the *closest* hit, and once the agent sphere
overlapped the lamp (dist=0), that degenerate lamp hit **occluded the wall right
behind it** — so the wall was never reported either. Net effect: the agent's
center physically walked through the lamp AND the wall (case12-L4: center reached
y=0.021, which is 0.12 m past the wall face at y=0.14), the camera followed it
into the wall → pure-black/white FPV. The 33 logged "collisions" were all
dist=0.000 degenerate hits recorded only AFTER it was already inside the wall.

This is a genuine benchmark bug: it changes the agent's trajectory, collision
counts, and success rate (e.g. an agent can cut through a mattress to reach a
goal it should have had to walk around).

### The fix (commit `03cadbe`)
Replaced substring matching with a precise `is_walkable_hit(path)` that matches
on the prim **basename**:
- structural floor: basename endswith `_floor` or == `floor`/`ground`
  (NOT `_wall`/`_exterior`/`_ceiling`/`skirtingboard`);
- soft textiles only: exact factory whitelist
  `{Rug, Blanket, Towel, Comforter, BoxComforter}Factory`, derived from the
  asset author's `Puppeteer/native_capability_manifest.md` (verified: all 73
  Factory types that appear across the 122 scenes are covered; these 5 are the
  only floor-level soft textiles).
Now FloorLamp / Mattress / all furniture correctly count as obstacles.
Deterministic verification: replaying the original crossing poses, the OLD rule
PASSED steps 55-59 (walked through) while the NEW rule BLOCKS them (agent stops
at the lamp, y=0.517) → the wall-through can no longer happen.

Related commits:
- `a046e72` — EYE_H camera-height sweep. **DEFENSIVE ONLY, NOT the root cause.**
  (Its git message says so explicitly. Kept as cheap extra coverage against
  genuine overhanging-geometry clips; do not mistake it for the fix.)
- `03cadbe` — the real fix (WALKABLE precise whitelist) + `[CLIP?]` dist=0
  embedding warning log + `SWEEP_DEBUG=1` env flag (off by default).
- `77a8b89` — `parallel_launch_remaining.sh` + `parallel_split.py --completed-from`.

### Diagnostic tooling added
- `bench_runner.py`: `SWEEP_DEBUG=1` env → logs every MOVE_FORWARD sweep
  (hit/dist/path per height). Off by default → zero impact on normal eval runs.
- `bench_runner.py`: `[CLIP?]` line logged whenever a blocking hit has dist≈0
  (agent sphere already embedded in a collider — early clip warning).
- `repro_badcase.sh [TASK_ID]`: re-runs ONE task with SWEEP_DEBUG into an
  isolated batch dir `results/_repro_sweep_debug/` (never touches eval stats).

### ⭐ Dataset state — THREE folders, DO NOT CONFUSE THEM ⭐
The original parallel run was **paused partway** (224/333 done) under the buggy
physics. Recovery plan: run *everything* again under the FIXED code, but reuse
the 224-already-done split point so we don't redo work twice. This produces
**three** result folders:

| Folder | Tasks | Code / physics | Role |
|---|---|---|---|
| `results/eval_30B_333_v2/` | **224** | OLD code, **BUGGY** WALKABLE-substring physics (agents clipped through lamps/mattresses/walls) | **Baseline only.** Pre-fix run, kept untouched. SR/trajectories here are contaminated. |
| `results/eval_30B_333_remaining_fixed/` | **109** | NEW code (`03cadbe`+), **FIXED** physics | Batch-1: the tasks the original run never reached. Running now. |
| `results/eval_30B_333_rerun_fixed/` | **224** | NEW code, **FIXED** physics | Batch-2: re-run of the tasks the buggy run did. Auto-starts after batch-1 (see watcher). |

**The clean, self-consistent fixed dataset = `remaining_fixed` (109) +
`rerun_fixed` (224) = all 333 tasks under fixed physics.** Keep them in two
folders or merge them — either way that union is the dataset to report.
`eval_30B_333_v2` is **NOT** part of it; it is only the buggy baseline for a
v1(buggy)-vs-v2(fixed) comparison that quantifies the clip bug's effect on SR.

- Why split, not one fresh 333 run: avoids re-rendering the 109 already done
  under the fix. By construction the two fixed folders never overlap
  (batch-2 uses `--completed-from eval_30B_333_remaining_fixed`).
- All fixed runs use **4 workers** (5 showed no 5× speedup; GPU0 render + GPU1
  VLM contention).

### Automation: `watch_then_rerun.sh`
A watcher polls batch-1 until it reaches its target (= 333 − orig_done = 109),
then auto-launches batch-2 into `eval_30B_333_rerun_fixed`. Launched detached on
HK; log at `/home/liuqi/hc/watch_rerun.log`.

### How to relaunch / monitor (HK)
```bash
ssh hk
cd /home/liuqi/hc/synth/benchmark_zehao

# (already running) batch-1: remaining tasks, fixed code, 4 workers
bash parallel_launch_remaining.sh 4 eval_30B_333_remaining_fixed eval_30B_333_v2

# (already running, detached) auto-start batch-2 when batch-1 finishes
nohup bash watch_then_rerun.sh > /home/liuqi/hc/watch_rerun.log 2>&1 &

# manual batch-2 (if not using the watcher): re-run the orig-done tasks, fixed,
# skipping whatever batch-1 already did, into the third folder
bash parallel_launch_remaining.sh 4 eval_30B_333_rerun_fixed eval_30B_333_remaining_fixed

# monitor
tail -f /home/liuqi/hc/eval_remaining_worker_*.log    # workers
tail -f /home/liuqi/hc/watch_rerun.log                # watcher / batch-2 trigger
tmux ls            # worker_0..3 ; NEVER kill vlm_serve (the vLLM server)
```

> Launcher args: `parallel_launch_remaining.sh <N_WORKERS> <OUTPUT_BATCH> <SKIP_FROM_BATCH(es)>`.
> `<SKIP_FROM>` accepts a comma-separated list, so a final clean-up pass could use
> `eval_30B_333_remaining_fixed,eval_30B_333_rerun_fixed` to fill any stragglers.

---

## Session 2026-06-13/14: Black-frame root-cause (3 classes) + room-boundary fix + render guard

### What drove this
User reviewing the concatenated FPV videos (`fpv_concat_L2_10x.mp4` etc.) flagged
**blue/purple tint**, **black windows**, and **fully black "walked into a dark room"**
segments. Tracing the worst one (×10 @6:40 → `case18_dining_push_lift-L2`, rerun_fixed)
opened a full black-frame audit.

### Findings

**1. Blue/purple tint + black windows = MirroredBall env-map (known, NOT fixed by design).**
Isaac RTX can't load the Infinigen MirroredBall HDR env map (`MirroredBall environment
format not supported yet`, once per scene) → sky/GI lost, only the spawn fill-lights +
DomeLight remain → blue/violet wash; windows/mirrors have no backdrop → black. Recorded
as eval input-noise, not fixed pre-eval.

**2. Black-frame episodes decompose into THREE classes** (per-task audit
`blackframe_audit.py`, threshold-free `fpv_black_frac` + auto class from fpv-vs-bird
sync & displacement). Of 620 audited tasks, only ~2.1% have ≥20% black frames — **not
systemic**, but the flagged ones are FALSE failures (agent blind → SR=0):

| Class | How it looks | Root cause | Examples |
|---|---|---|---|
| **CAMERA** (mislabel; really "walked out") | only FPV black, **bird stays lit**, sync=0, agent moved 2.4–9 m | agent walked OUT of the room through a **missing-wall opening into unlit void** — collision sweep correctly returns NO hit there (no geometry), 0 CLIP, 0 wall-collision. NOT camera-clip/穿模. | case18, case069-L2/L4, case076, case055, case012 |
| **RENDER** | **FPV+bird black together**, sync=1.0, independent of motion (case075 blacks at disp=0.03 m) | RTX renderer faults and emits empty frames; Python is unaware (`gen_media rc=0`, `All done!`). Common trait: scenes with extreme original light intensity (2–8e8 capped to 1e5; case06 real2sim). Cap→1e5 doesn't fully prevent it → needs Isaac diagnosis. | case06, case064, case075, case024 |
| **DARK** | only FPV black, low displacement | spawn/area dim — fill-lights anchored at spawn don't cover it | case055 (partial) |

**KEY correction:** the earlier "camera near-clip 穿模" hypothesis was WRONG. All flagged
CAMERA cases have **0 `[CLIP?]`, 0 wall collisions, bird fully lit** — nothing clips into
geometry. The agent legitimately walks through an opening into an unlit, possibly
wall-less space. So `near_clip` was deliberately NOT changed (it would only swap black for
a wall-texture smear, not fix anything).

**User manual-inspection log (per-case, drove the classification):**
- `case18-L2` — ×10@6:40. FPV frames 45→48 show a black block progressively eating
  the view (looked like 穿模); but bird stays lit, 0 CLIP. Walked x 5→−5.9 (AWAY from
  goal at x=14.5) ~11 m into a dark space. Frame 115+ the agent turns back, FPV dimly
  re-lights (a far light spot) → confirms it walked out and partway back. → CAMERA(walk-out).
- `case06-L4` — **bird ALSO goes black at 0:46 (step6)**, FPV+bird both 0.0 from step6 on;
  agent stuck at a wall since step4 (didn't move). decision-render 1.0 s/frame (vs ~6.8
  normal) → renderer emitting empty frames. → RENDER.
- `case069-L2` — user: "0:11 entered black wall / 穿模, whole scene also very dim". Bird
  preview shows the room as a lit island in black void with a white opening; agent walked
  y 8→12.7 toward it, 0 collision. Scene baseline dim (no strong light, fpv head ~85 vs
  case06's 245). → CAMERA(walk-out) + globally dim.
- `case075-L4` — full black after 0:30, fpv+bird synced, agent disp 0.03 m (never moved).
  → RENDER.
- `case064-L4` — bird black after 0:46, fpv+bird synced. → RENDER.
- `case076-L3` — user: "0:40 穿模, can see them in another room". Only fpv black, bird lit,
  walked into an adjacent room. → CAMERA(walk-out).
- `case055-L3` — user: "no problem, just dim lighting". Only fpv, low/var. → DARK.
- `case024-L3` — fpv=bird=100% black from step0 (worst), sync=1.0. → RENDER.

**Black-frame stats (`blackframe_audit.py`, 620 tasks across 1frame + 2 fixed folders):**
threshold-free `fpv_black_frac`: median 0, p95 0.009, p99 0.587. ≥20% black = 13 tasks
(2.1%), ≥50% = 9 (1.5%). Class counts: 1frame 287→ 5 CAMERA; rerun_fixed 224→ 5 CAMERA +
4 RENDER; remaining_fixed 109→ 0 (fully clean). Not偏向 a single level. CSV at
`/home/liuqi/blackframe_audit.csv` on HK. → integral SR should report N valid / M total
with `render_invalid` excluded.

### Fixes implemented (committed, pushed; need Isaac re-render to verify)

- **`feat(bench): N_FRAMES temporal-context switch`** (`a90388b`) — ends the HK/SG git
  divergence: the 1frame-vs-3frame switch the HK eval actually ran on was never pushed.
  Default `N_FRAMES=3` = byte-identical baseline.
- **`feat(audit): blackframe_audit.py`** (`aacb3f4`) — per-task threshold-free black-frame
  audit + RENDER/CAMERA/DARK class. Pure python/PIL, no GPU.
- **`feat(bench): render-quality guard`** (`246f202`) — every episode writes
  `metrics.render_quality` (`fpv_black_frac`, `bird_black_frac`, `render_black_sync`,
  `render_invalid` = fpv_black_frac ≥ `RENDER_INVALID_FRAC` default 0.20). Marks (does NOT
  fix) false-failure episodes so they can be excluded from SR. `sync≥0.7` → RENDERER-FAULT.
- **`fix(bench): soft room-boundary gate`** (`3d9413b`) — THE real fix for the CAMERA class.
  Extracts each room's floor convex-hull polygon from the USD floor meshes at runtime
  (self-contained, mirrors `validate_all_spawns`) and blocks any MOVE_FORWARD whose landing
  point leaves EVERY room polygon, like a wall (`hit=room_boundary`, gives the agent a
  'blocked' cue). **Fail-open**: no polygons / spawn outside them → gate self-disables
  (never traps a valid agent). Env: `ROOM_BOUNDARY` (default 1), `ROOM_BOUNDARY_MARGIN`
  (0.5 m). Does NOT touch lighting/rendering. Smoke-tested on case18: gate loaded ("1 room
  polygon"), nav ran, render_quality written, 0 Traceback.
- **`fix(launch): mkdir /usr/share/nvidia before docker cp nvoptix.bin`** (`4f81672`) — fresh
  isaac-sim containers lack the dir → nvoptix copy silently failed → Optix denoiser error →
  noisier renders. Both launchers now mkdir first.

### Tonight's run (setting)
Orchestrator `run_overnight.sh` (nohup, HK), 3 Isaac workers reused serially across 2 batches:
- **Batch 1 — blackfix-verify**: re-render the 10 audit-flagged tasks with the fixes,
  **N_FRAMES=3 (baseline), ROOM_BOUNDARY=1**, into NEW folder `eval_30B_blackfix_verify`
  (`launch_blackfix_verify.sh`). Purpose: confirm CAMERA cases no longer walk into the void;
  confirm RENDER cases still flag `render_invalid` (determinism check).
- **Batch 2 — 1frame backfill**: the 46 tasks lost to the disk-full event, **N_FRAMES=1,
  ROOM_BOUNDARY=1**, into NEW folder `eval_30B_1frame_backfill` (`launch_1frame_backfill.sh`,
  skip-from old `eval_30B_333_1frame`+self, **ABORTs if remaining>60** — guard against the
  accidental full-333 sweep that happened once and was killed before writing any results).
  Kept in a separate folder so new-code results never mix with the old 287 (old code, no
  room-boundary).

`wait_tmux` fixed to wait for sessions to APPEAR then DISAPPEAR (the first launch skipped
batch1 because it checked for `verify_` before tmux had created it).

### Infra note (HK)
Root disk `/` (295 G) filled to 100% from docker container writable layers
(`/var/lib/docker`), 4 bench containers ballooned to 58–67 G each → containers crashed →
the original 1frame run silently lost 46 tasks (287/333 on disk; "328" = tasks touched).
Removed the 5 dead bench containers → root back to 17 %. Root fix (not yet done): migrate
docker data-root to `/home` (1.5 T). See memory `project_synth_hk_docker_rootdisk`.

### Next steps
1. **Verify the fixes** (after tonight): diff `eval_30B_blackfix_verify` vs the old flagged
   runs — CAMERA cases should have ~0 black frames + `hit=room_boundary` blocks; RENDER cases
   should still be black (boundary can't fix them) and carry `render_invalid=true`.
2. **RENDER class** (case06/064/075/024): Isaac diagnosis — drop the extreme scene lights
   (e.g. real2sim / 2–8e8 PointLamps) to a normal level, re-render one frame, see if the
   black disappears. Then decide the real fix (tighter light cap vs renderer workaround).
3. **DARK / off-spawn dimness**: make fill-lights follow the agent, or a ceiling RectLight
   sized to the Infinigen floor bbox (replaces the spawn-anchored 5-light cross).
4. **Migrate docker data-root to `/home`** on HK (root fix for the disk-full failure mode).
5. **Aggregate SR excluding `render_invalid`**, report as N valid / M total.
