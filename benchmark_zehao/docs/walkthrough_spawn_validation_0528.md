# Walkthrough: Spawn Validation & Wall-Clipping Fix

## Changes Made

### 1. Wall-Clipping Prevention — `bench_runner.py`
**Root cause**: Runner push could move agent within 0.05m of walls. Camera (0.1m forward offset) would clip through thin geometry (window frames).

**Fix**: Increased `_sweep_clear` wall buffer from `0.05` to `0.15`m (lines 644-666):
```diff
-carb.Float3(dx, dy, 0), dist + 0.05)
+carb.Float3(dx, dy, 0), dist + 0.15)
...
-return max(float(h.get("distance", 0)) - 0.05, 0.0)
+return max(float(h.get("distance", 0)) - 0.15, 0.0)
```
- Agent center stays ≥0.55m from walls → camera at ≥0.45m → no clipping
- When wall too close: existing wall-slide → freeze cascade handles it

### 2. Runtime Spawn Nudge — `bench_runner.py`
Added at step 0 before `prime_render()` (lines 892-960):
- Sweeps 8 directions at z=0.5/1.0 to detect furniture overlaps
- Spiral-searches (0.25m steps, max 2m) for nearest clear position
- Saves `spawn_adjustment.json` alongside results
- Adds `spawn_adjusted` + `effective_start` to `results.json`

### 3. Validation Script — `validate_and_fix_spawns.py` (NEW)
Isaac Sim PhysX script that validates all 38 tasks:
- **Overlap check**: sweep 8 dirs at 0.05m, detect inside-obstacle
- **Clearance check**: min clearance in ≥2 directions
- **BFS flood-fill reachability**: 0.25m grid, 0.40m collision sphere, 5000 max cells
- **L2/L4 FOV gate**: target must be outside ±45° FOV at step 0

### 4. README Known Issues
- Frozen runner visual mismatch (position frozen, animation continues)
- Camera near-clip through thin geometry (mitigated by buffer increase)

## Validation Results

| Metric | Count |
|--------|-------|
| Spawns OK | 38/38 |
| FOV violations fixed | 5 |
| Unreachable targets | 3 |

**FOV violations** (yaw auto-corrected in `benchmark_tasks_validated.json`):
- case03-L4, case04-L4, case06-L2, case09-L2, case09-L4

**Unreachable targets** (need user review):
- case02-L3 phase 1: SimpleBookcase at (17.7, 1.1) — possibly behind walls
- case04-L2 phase 0: SimpleBookcase at (1.8, 9.9) — different room section
- case04-L3 phase 1: SimpleBookcase at (1.8, 9.9) — same issue

## Files Changed
render_diffs(file:///home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/bench_runner.py)
render_diffs(file:///home/qi/hc/Puppeteer/zehao_task/README.md)

## Next Steps
1. User reviews 3 unreachable targets — may need to swap target objects or adjust spawn
2. Use `benchmark_tasks_validated.json` for next overnight run
3. Run `MAX_STEPS=1` test to verify spawn nudge works in practice
