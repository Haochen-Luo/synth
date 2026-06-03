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