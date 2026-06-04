# 4DSynth-Nav Benchmark Utilities

This directory contains several highly reusable, generalized utility scripts developed during the stabilization and validation phases of the 4DSynth-Nav benchmark. 

These utilities encapsulate our solutions to common challenges encountered in Isaac Sim, such as coordinate probing, FOV/Line-of-Sight validation, scene semantic inspection, and physical trajectory diagnostics.

---

## 🟢 Core Reusable Utilities

### 1. `validate_all_spawns.py`
**Background:** 
Initially, we struggled with the VLM agent being spawned out of bounds, inside obstacles (spawn-win/clipping), or facing entirely the wrong direction (FOV occlusion). We needed a robust, physics-aware validator to pre-check all starting coordinates.

**What it does:**
- Validates that the agent starts within the defined floor polygon (`floor_bbox`).
- Raycasts Line of Sight (LoS) to verify that the target object is not physically occluded by walls or large furniture.
- Calculates horizontal field of view (FOV) to ensure the target is within the agent's initial camera frame.
- Uses `edge-to-edge` distance calculation to confirm the agent is not spawning directly on top of the target (preventing trivial spawn-wins).
- Outputs a detailed `spawn_validation_report.json` with pass/fail metrics.

**Usage:**
```bash
# Run inside the vlm-jupyter docker container
/isaac-sim/python.sh validate_all_spawns.py
```

### 2. `probe_target_bbox.py`
**Background:**
Calculating the distance between the agent and a target requires knowing the target's physical dimensions (Bounding Box), rather than just its center point. Hardcoding sizes was unscalable.

**What it does:**
- Programmatically probes any given USD prim (by its Factory name or Prim path) and extracts its world-space Bounding Box (AABB) using `omni.usd.get_context().compute_path_world_bounding_box()`.
- Calculates the true center and the `half_extent` (radius) of the object for accurate edge-to-edge distance thresholds.

**Usage:**
```bash
/isaac-sim/python.sh probe_target_bbox.py
```

### 3. `probe_lights.py` & `scene_prober.py`
**Background:**
During visual testing, several benchmark frames suffered from severe overexposure or pitch-black rendering. We needed a way to programmatically inspect the hierarchy, find all light sources (SphereLights, DomeLights), and read their intensity/exposure values.

**What it does:**
- Traverses the active USD stage to locate all `UsdLux` lighting prims.
- Dumps the current lighting hierarchy and intensity settings to standard output, making it easy to identify clipping lights in narrow corridors.
- `scene_prober.py` additionally scans for semantic classes and object factories.

**Usage:**
```bash
/isaac-sim/python.sh probe_lights.py
```

### 4. `diag_runner_loop.py` & `bev_auto_trajectory.py`
**Background:**
When the benchmark fails due to physics collisions (e.g., the agent getting stuck or pushed by a dynamic runner), debugging from text logs or First-Person View (FPV) frames alone is extremely difficult. We needed a top-down, global perspective.

**What it does:**
- Instantiates a high-altitude orthographic camera (Bird's Eye View - BEV).
- Records the full trajectory of the agent and dynamic human runners.
- Converts the rendered frames into a continuous GIF or MP4 video for visual debugging.

**Usage:**
```bash
/isaac-sim/python.sh diag_runner_loop_3fps.py
```

### 5. `collect_spawn_images.py`
**Background:**
To quickly audit the initial visual quality of the 40 benchmark tasks without running the full pipeline, we needed a rapid frame-extraction tool.

**What it does:**
- Cycles through all tasks defined in `benchmark_tasks.json`.
- Spawns the agent at the exact start coordinates, renders frame 0, and saves the initial FPV image to a consolidated folder.
- Essential for visually verifying that targets are visible before kicking off overnight runs.

**Usage:**
```bash
/isaac-sim/python.sh collect_spawn_images.py
```

---

## 🔴 Archived Scripts (`scratch_archive/`)
All one-off, ad-hoc, or experimental scripts have been safely moved to `scratch_archive/` to keep the root directory clean. This includes temporary lighting tests (`test_domelight.py`), specific mesh collision tests (`test_bookstack.py`), and raw debug logs (`dry_run_*.log`). Nothing was deleted, so you can always recover past test logic if needed.
