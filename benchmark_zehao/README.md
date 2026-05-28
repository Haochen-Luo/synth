# 4DSynth-Nav Benchmark Spawn Validation
*Last Updated: 2026-05-28*

This README documents the spawn point validation system implemented in `validate_all_spawns.py`, which is responsible for ensuring that all agent starting coordinates and yaw angles in `benchmark_tasks.json` are physically and logically valid.

## Core Objective
The agent must spawn:
1. Inside the correct room (not in the external void).
2. Physically collision-free (not clipping into furniture or walls).
3. With a correct Field of View (FOV) constraint relative to the task level (L1/L3 must see target; L2/L4 must NOT see target).
4. Without staring into a blank wall (Forward Clearance).

## Historical Bugs & Fixes (V1 -> V4)

During testing, we discovered several edge cases that caused the agent to spawn in the "black void", clip through walls, or stare blankly at a wall. The following structural fixes were applied:

### 1. The L-Shaped Room Void Issue
**Bug:** We initially used a Bounding Box (BBox) and later a Convex Hull to define the "floor". In L-shaped rooms, the Convex Hull encloses the empty "inner corner" void, causing agents to spawn outside the house.
**Fix:** We migrated to a **Precise Concave Boundary** extraction logic (ported from `extract_bev_annotation_data_blender.py`). By traversing USD mesh face indices and isolating single-occurrence boundary edges, we reconstruct the exact, non-convex 2D footprint of the floor.

### 2. The `shrink_polygon` Bulge Bug
**Bug:** To prevent the agent from spawning right against a wall, an artificial `shrink_polygon` function pushed all vertices toward the centroid. In concave polygons, this inadvertently pushed the "inner corner" vertices *outward* into the void, causing void spawns.
**Fix:** We completely deleted `shrink_polygon`. We now rely on the exact 2D polygon combined with the strict 3D PhysX collision sphere sweep to keep the agent naturally away from walls.

### 3. The `WALKABLE` Whitelist Exploit
**Bug:** The PhysX collision sweep ignores objects in the `WALKABLE` whitelist. Because `"wall"` and `"exterior"` were mistakenly added to this list, an agent spawning in the void just outside an exterior wall would hit the wall with its sweep sphere, see it was "walkable", and pass the validation.
**Fix:** We removed structural objects from the whitelist. The whitelist is now strictly limited to true floor surfaces (`"floor", "ground", "rug", "blanket", "towel", "mat"`). Any physical touch of a wall now correctly invalidates the spawn point.

### 4. The "Staring at a Wall" Issue (V4)
**Bug:** For L2/L4 tasks (where the agent must look away from the target), the script mathematically rotated the agent exactly 180°. If there was a wall immediately behind the agent, the FPV camera ended up clipped into the wall. For L1/L3 tasks, the agent would mathematically face the target, but if the target was behind a wall (in another room), the agent would just stare at the wall.
**Fix:** Implemented `check_forward_clearance`. A ray is cast 1.2m forward from the camera. 
- For L2/L4: The script smartly tests multiple angles (180, 150, 210, etc.) until it finds a yaw that both hides the target and has clear forward space.
- For L1/L3: If the line of sight to the target is blocked by a wall within 1.2m, the script rejects the coordinate entirely and searches the room for a spot with clear visibility.

## Running the Validation

To re-run the auto-fix pipeline on the current `benchmark_tasks.json`:
```bash
docker exec -w /home/qi/hc/Puppeteer/zehao_task/benchmark_zehao vlm-jupyter /isaac-sim/python.sh validate_all_spawns.py --fix
```

To run a single step dry-run to generate validation frames (Bird + FPV):
```bash
./dryrun_v4.sh
```
This will collect the visual validation frames into the `review_v4` folder on the host.
