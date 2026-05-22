"""Verify that the judge's target coordinate matches the ONE prim that
survives semantic-class deduplication. Runs the same target-resolution +
dedup logic as bench_runner, then cross-checks. Writes coord_verify_result.txt.

Usage (inside vlm-jupyter container):
  TASK_ID=case01-L2 /isaac-sim/python.sh coord_verify.py
"""
import sys, os, json, traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
RESULT = os.path.join(SCRIPT_DIR, "coord_verify_result.txt")
def w(m):
    with open(RESULT, "a") as f: f.write(m + "\n")
    print(m, flush=True)
open(RESULT, "w").close()

try:
    from bench_helpers import (discover_scene_files, find_prim_by_factory,
                               find_all_prims_by_factory, get_prim_world_center)
    from semantic_classes import semantic_class_of

    task_id = os.environ.get("TASK_ID", "case01-L2")
    bench = json.load(open(os.path.join(SCRIPT_DIR, "benchmark_tasks.json")))
    task = {t["id"]: t for t in bench["tasks"]}[task_id]
    scene_dir = os.path.join(SCRIPT_DIR, task["scene_dir"])
    w(f"TASK {task_id}: instruction={task['instruction']!r}")
    for ph in task["phases"]:
        w(f"  phase {ph['name']}: target_object={ph['target_object']} "
          f"action={ph['action']} desc={ph['desc']!r}")

    from isaacsim import SimulationApp
    app = SimulationApp({"headless": True})
    import omni.usd
    from omni.isaac.core.utils.stage import open_stage, is_stage_loading
    from pxr import UsdGeom

    sf = discover_scene_files(scene_dir)
    open_stage(sf["stage"])
    while is_stage_loading(): app.update()
    stage = omni.usd.get_context().get_stage()
    w(f"stage loaded: {sf['stage']}")

    # ── Resolve targets exactly like bench_runner ──
    target_classes = set()
    target_prim_paths = set()
    resolved = []
    for ph in task["phases"]:
        tobj = ph["target_object"]
        target_classes.add(tobj)
        if tobj.startswith("__human_") or tobj == "door":
            w(f"  (skipping non-factory target {tobj})")
            continue
        # Show ALL prims of this factory — exposes the "first match" ambiguity.
        all_pp = find_all_prims_by_factory(stage, tobj)
        w(f"\n  factory {tobj}: {len(all_pp)} prim(s) in scene:")
        for pp in all_pp:
            c = get_prim_world_center(stage, pp)
            w(f"    {pp}  center={[round(x,3) for x in c[:3]] if c else None}")
        chosen = find_prim_by_factory(stage, tobj)
        c = get_prim_world_center(stage, chosen) if chosen else None
        target_prim_paths.add(chosen)
        resolved.append((tobj, chosen, c[:2] if c else None))
        w(f"  -> find_prim_by_factory CHOSE: {chosen}")
        w(f"  -> JUDGE target_xy = {[round(x,3) for x in c[:2]] if c else None}")

    # ── Run semantic-class dedup ──
    target_semantic = {semantic_class_of(tc) for tc in target_classes}
    w(f"\ntarget semantic class(es): {sorted(target_semantic)}")
    props = stage.GetPrimAtPath("/World/InteractiveProps")
    deactivated, kept = [], []
    if props and props.IsValid():
        for child in props.GetChildren():
            c_path = child.GetPath().pathString
            sem = semantic_class_of(child.GetName())
            if sem in target_semantic:
                if c_path in target_prim_paths:
                    kept.append((c_path, sem))
                else:
                    deactivated.append((c_path, sem))

    w(f"\nsame-semantic-class prims: {len(kept)+len(deactivated)} total")
    w(f"  KEPT (target):")
    for p, s in kept: w(f"    [{s}] {p}")
    w(f"  DEACTIVATED (non-target):")
    for p, s in deactivated: w(f"    [{s}] {p}")

    # ── Cross-check ──
    w("\n=== CROSS-CHECK ===")
    ok = True
    if len(kept) != 1:
        w(f"  ✗ FAIL: expected exactly 1 target prim kept, got {len(kept)}")
        ok = False
    else:
        kept_path = kept[0][0]
        for tobj, chosen, xy in resolved:
            if chosen != kept_path:
                w(f"  ✗ FAIL: judge target prim {chosen} != surviving prim {kept_path}")
                ok = False
            else:
                # Re-fetch center after dedup to confirm it is unchanged.
                c2 = get_prim_world_center(stage, kept_path)
                w(f"  judge target prim survives dedup: {kept_path}")
                w(f"  judge target_xy        = {[round(x,3) for x in xy]}")
                w(f"  prim center after dedup= {[round(x,3) for x in c2[:2]] if c2 else None}")
                if c2 and xy and (abs(c2[0]-xy[0]) > 0.01 or abs(c2[1]-xy[1]) > 0.01):
                    w(f"  ✗ FAIL: coordinate drifted after dedup")
                    ok = False
                else:
                    w(f"  ✓ coordinate stable, judge and scene agree")
    w(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    app.close()
except Exception as e:
    w("ERROR:\n" + traceback.format_exc())
