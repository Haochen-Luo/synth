"""generate_tasks.py — pure-Python task generation over scene_facts.json.

The non-Isaac half of the corrected pipeline (README "Session 2026-06-02"):
  probe_stage.py (Isaac) -> scene_facts.json -> [THIS] -> candidate tasks
  -> validate_full_spawns/validate_all_spawns (Isaac: spawn + LOS/FOV gate) -> tasks.json

Decides everything against the REAL geometry captured in scene_facts.json and bakes
self-contained tasks. Correctness invariants enforced here:
  * resolved target = real Obj_<id> prim path (from scene_facts) — never an over.
  * ENCLOSURE filter: a pickup sealed inside a CLOSED container (cabinet/kitchen
    counter) is rejected — it cannot be seen or picked (doors don't open). This is the
    case003 failure mode (bottle inside SingleCabinet).
  * prefer-unique target: pick targets whose semantic class is unique-in-room so NO
    deactivation is needed (zero cascade/fall risk by construction).
  * support-class exclusion: no phase target shares a semantic class with the support
    instance of any pickup in the task (else dedup would strip the pickup's support).
  * floor <= 30%: cap floor-resting pickups (hard case: forces camera tilt-down).
  * baked deactivate_prims: same-semantic-class non-target props + their resting
    clutter, NEVER a pickup's support.

Output: benchmark_tasks_generated.json (candidate tasks; agent_start/agent_yaw filled
by the spawn-validation step). Tasks that cannot satisfy the invariants are written to
dropped_tasks.json with a reason.

Usage:  python3 generate_tasks.py            # all scenes with scene_facts.json
        SCENES=a,b python3 generate_tasks.py # subset (scene_facts dir names)
"""
import json, os, glob, re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(SCRIPT_DIR, "full_scenarios_extracted")
OUT_TASKS = os.path.join(SCRIPT_DIR, "benchmark_tasks_generated.json")
OUT_DROPPED = os.path.join(SCRIPT_DIR, "dropped_tasks.json")
FLOOR_PICKUP_CAP = 0.30

# ── Factory classification (kept in sync with probe_and_generate_tasks.py) ──
PORTABLE_FACTORIES = {
    'BookFactory', 'BookStackFactory', 'BookColumnFactory', 'SupportBookStackFactory',
    'CupFactory', 'PlateFactory', 'BowlFactory', 'BottleFactory', 'CanFactory',
    'JarFactory', 'WineglassFactory', 'FoodBagFactory', 'FruitContainerFactory',
    'PotFactory', 'VaseFactory', 'MugFactory', 'GlassFactory',
    'NatureShelfTrinketsFactory',
}
# Only these are allowed as PICKUP targets: unambiguous everyday nouns a VLM can name.
# Excludes vague nouns (e.g. NatureShelfTrinkets="trinket", SupportBookStack) — the VLM
# cannot reliably identify them, so they make invalid pick targets even when unique.
CLEAR_PICKUP_FACTORIES = {
    'BottleFactory', 'CanFactory', 'JarFactory', 'CupFactory', 'MugFactory',
    'GlassFactory', 'WineglassFactory', 'BowlFactory', 'PlateFactory', 'PotFactory',
    'VaseFactory', 'BookFactory', 'BookStackFactory',
}
DESTINATION_FACTORIES = {
    'SimpleBookcaseFactory', 'LargeShelfFactory', 'CellShelfFactory',
    'SimpleDeskFactory', 'KitchenIslandFactory', 'KitchenSpaceFactory',
    'SingleCabinetFactory', 'SideTableFactory', 'DiningTableFactory',
    'BarChairFactory', 'SinkFactory', 'ToiletFactory', 'LargePlantContainerFactory',
}
# Closed storage: items inside these are sealed (no door-opening) -> invalid pickups.
CLOSED_CONTAINERS = {
    'SingleCabinetFactory', 'CabinetFactory', 'KitchenCabinetFactory',
    'KitchenSpaceFactory',
}
FACTORY_TO_NL = {
    'BookFactory': 'book', 'BookStackFactory': 'book stack', 'BookColumnFactory': 'book column',
    'SupportBookStackFactory': 'book stack', 'CupFactory': 'cup', 'PlateFactory': 'plate',
    'BowlFactory': 'bowl', 'BottleFactory': 'bottle', 'CanFactory': 'can', 'JarFactory': 'jar',
    'WineglassFactory': 'wine glass', 'FoodBagFactory': 'food bag',
    'FruitContainerFactory': 'fruit basket', 'PotFactory': 'pot', 'VaseFactory': 'vase',
    'MugFactory': 'mug', 'GlassFactory': 'glass', 'NatureShelfTrinketsFactory': 'decorative trinket',
    'SimpleBookcaseFactory': 'bookshelf', 'LargeShelfFactory': 'shelf', 'CellShelfFactory': 'shelf',
    'SimpleDeskFactory': 'desk', 'KitchenIslandFactory': 'kitchen island',
    'KitchenSpaceFactory': 'kitchen counter', 'SingleCabinetFactory': 'cabinet',
    'SideTableFactory': 'side table', 'DiningTableFactory': 'dining table',
    'BarChairFactory': 'bar stool', 'SinkFactory': 'sink', 'ToiletFactory': 'toilet',
    'LargePlantContainerFactory': 'large plant',
}

ENCLOSE_PAD = 0.05
ON_TOP_TOL = 0.12


def infer_room_type(sn):
    sn = sn.lower()
    if any(t in sn for t in ('living', 'media', 'lounge', 'bar', 'social')): return 'living'
    if any(t in sn for t in ('bedroom', 'guest_room', 'study', 'decor')): return 'bedroom'
    if any(t in sn for t in ('kitchen', 'cooking', 'counter', 'island', 'breakfast')): return 'kitchen'
    if any(t in sn for t in ('dining', 'gallery_table', 'formal_center')): return 'dining'
    if any(t in sn for t in ('bathroom', 'vanity', 'tub', 'compact_storage')): return 'bathroom'
    if 'office' in sn: return 'office'
    return 'unknown'


def infer_category(sn):
    sn = sn.lower()
    if '_official_' in sn: return '80_official'
    if '_text_' in sn or '_mask_' in sn: return '40_textmask'
    if '_scene_gen_v5_' in sn: return '15_real2sim'
    return '20_native'


def nl(factory):
    return FACTORY_TO_NL.get(factory, factory.replace('Factory', '').lower())


def enclosing_closed_container(obj, objects):
    """Return the prim_path of a CLOSED container whose volume encloses `obj`
    (xy inside, within z-span, not resting on top), else None."""
    c = obj["center"]; amin = obj["bbox_min"]; amax = obj["bbox_max"]
    for b in objects:
        if b is obj or b["factory"] not in CLOSED_CONTAINERS:
            continue
        bmin, bmax = b["bbox_min"], b["bbox_max"]
        xy_in = (bmin[0] - ENCLOSE_PAD <= c[0] <= bmax[0] + ENCLOSE_PAD and
                 bmin[1] - ENCLOSE_PAD <= c[1] <= bmax[1] + ENCLOSE_PAD)
        if not xy_in:
            continue
        within_z = (bmin[2] - ENCLOSE_PAD <= amin[2] and amax[2] <= bmax[2] + ENCLOSE_PAD)
        on_top = abs(amin[2] - bmax[2]) <= ON_TOP_TOL
        if within_z and not on_top:
            return b["prim_path"]
    return None


def support_semantic(obj, by_path):
    s = obj.get("support")
    if s and s in by_path:
        return by_path[s]["semantic"]
    return None


def bake_deactivate(target_paths, target_semantics, pickup_support_paths, objects):
    """Same-semantic-class non-target props + their resting clutter, minus any pickup
    support. Mirrors the runner's dedup+cascade, decided here once against real geometry."""
    deact = set()
    for o in objects:
        if o["semantic"] in target_semantics and o["prim_path"] not in target_paths \
           and o["prim_path"] not in pickup_support_paths:
            deact.add(o["prim_path"])
    # cascade: clutter resting on a deactivated furniture
    changed = True
    while changed:
        changed = False
        for o in objects:
            if o["prim_path"] in deact or o["prim_path"] in target_paths \
               or o["prim_path"] in pickup_support_paths:
                continue
            if o.get("support") in deact:
                deact.add(o["prim_path"]); changed = True
    return sorted(deact)


def make_phase(obj, action, desc, radius, place_at=None):
    # When place_at is set (fallback relocation), the target center IS the placed
    # position (validator + runner use it), not the object's authored center.
    center = list(place_at[:3]) if place_at else obj["center"]
    return {"name": f"{action.lower()}_{obj['factory']}", "target_object": obj["factory"],
            "target_prim": obj["prim_path"], "radius": radius, "action": action,
            "desc": desc, "place_at": place_at, "_center": center}


# ── Fallback placement (when a scene has no valid existing pickup) ──
# Prefer a REACHABLE furniture top at camera-view height (no tilt-down) over the floor.
CAM_SURFACE_Z = (0.55, 1.05)  # furniture-top z visible from eye 1.58m / pitch -10 w/o tilt

def synth_pickup_on_surface(objects):
    """Pick a reachable camera-height furniture surface + a distinct clear-noun portable
    to RELOCATE onto it (place_at). Returns (obj, place_at_xyz, surface) or None.
    Floor is intentionally NOT used (forces tilt-down, VLM-hard)."""
    surfaces = [o for o in objects if o["factory"] in DESTINATION_FACTORIES
                and o.get("reachable", True)
                and CAM_SURFACE_Z[0] <= o["bbox_max"][2] <= CAM_SURFACE_Z[1]]
    if not surfaces:
        return None
    surfaces.sort(key=lambda o: -o["half_extent_xy"])   # widest top first
    surf = surfaces[0]
    portables = [o for o in objects if o["factory"] in CLEAR_PICKUP_FACTORIES]
    if not portables:
        return None
    obj = portables[0]
    place_at = [round(surf["center"][0], 3), round(surf["center"][1], 3),
                round(surf["bbox_max"][2] + 0.03, 3)]
    return obj, place_at, surf


def gen_scene(facts, scene_dir_name, floor_state, dropped):
    scene = facts["scene_name"]
    objects = facts["objects"]
    by_path = {o["prim_path"]: o for o in objects}
    counts = facts["semantic_class_counts"]
    room = infer_room_type(scene)
    cat = infer_category(scene)
    sd = f"full_scenarios_extracted/{scene_dir_name}"

    def uniq(o):  # semantic class unique in this room
        return counts.get(o["semantic"], 0) == 1

    # Candidate pools — pickups restricted to clear, nameable nouns (no "trinket"),
    # and to REACHABLE objects (baked by probe_stage) so tasks are navigable by
    # construction. .get(...,True) keeps backward-compat with pre-reachability facts.
    pickups = [o for o in objects
               if o["factory"] in CLEAR_PICKUP_FACTORIES and o.get("reachable", True)]
    dests = [o for o in objects
             if o["factory"] in DESTINATION_FACTORIES and o.get("reachable", True)]
    # prefer unique-in-room, larger, lower (easier to see) destinations
    dests.sort(key=lambda o: (not uniq(o), -o["half_extent_xy"]))

    tasks = []

    def add(level, phases, facing):
        base = f"{scene.replace('native_','')}-{level}"
        # support-class exclusion: pickup supports must not share class with any target
        tgt_sems = {p["target_object_semantic"] for p in phases}
        for p in phases:
            if p.get("_pickup_support_sem") and p["_pickup_support_sem"] in \
               (tgt_sems - {p["target_object_semantic"]}):
                dropped.append({"id": base, "reason": "support_class_collision",
                                "detail": p["_pickup_support_sem"]})
                return
        # build instruction
        if len(phases) == 1:
            instr = f"Go to the {phases[0]['desc']}."
        else:
            instr = f"Pick up the {phases[0]['desc']} and bring it to the {phases[1]['desc']}."
        target_paths = {p["target_prim"] for p in phases}
        target_sems = {p["target_object_semantic"] for p in phases}
        pickup_support_paths = {p["_support_path"] for p in phases if p.get("_support_path")}
        deact = bake_deactivate(target_paths, target_sems, pickup_support_paths, objects)
        clean = []
        for p in phases:
            q = {k: v for k, v in p.items() if not k.startswith("_") and
                 k != "target_object_semantic"}
            q["target_center"] = p["_center"]
            clean.append(q)
        tasks.append({
            "id": base, "level": level, "scene_dir": sd,
            "instruction": instr, "agent_start": None, "agent_yaw": None,
            "spawn_facing": facing, "phases": clean, "deactivate_prims": deact,
            "category": cat, "room_type": room,
            "task_type": "navigate" if len(phases) == 1 else "pick_place",
        })

    # ── pick a valid pickup (not enclosed in a closed container) ──
    def valid_pickup():
        for o in pickups:
            enc = enclosing_closed_container(o, objects)
            if enc:
                dropped.append({"id": f"{scene}-pickup", "reason": "enclosed_in_closed_container",
                                "object": o["prim_path"], "container": enc})
                continue
            # floor cap
            if o["on_floor"]:
                used, total = floor_state
                if total > 0 and (used + 1) / (total + 1) > FLOOR_PICKUP_CAP:
                    continue
            return o
        return None

    def dest_for(pick):
        psup = support_semantic(pick, by_path) if pick else None
        for d in dests:
            if pick and d["prim_path"] == pick["prim_path"]:
                continue
            if psup and d["semantic"] == psup:   # support-class exclusion
                continue
            return d
        return None

    # L2: navigate to a (preferably unique) destination
    if dests:
        d = dests[0]
        ph = make_phase(d, "STOP", nl(d["factory"]), 1.5)
        ph["target_object_semantic"] = d["semantic"]
        add("L2", [ph], "back")

    # L3 / L4: pick + place
    pick = valid_pickup()
    pick_place_at = None
    synthetic = False
    if pick is None:
        # Fallback: relocate a distinct clear-noun object onto a reachable camera-height
        # surface (never the floor). Validation still gates the placed position.
        syn = synth_pickup_on_surface(objects)
        if syn:
            pick, pick_place_at, surf = syn
            synthetic = True
            psup_sem = surf["semantic"]            # rests on the chosen surface now
            psup_path = None                       # surface kept (class != dest class below)
            dropped.append({"id": f"{scene}-pickup",
                            "reason": "no_existing_pickup_used_place_at_fallback",
                            "object": pick["prim_path"], "surface": surf["prim_path"],
                            "place_at": pick_place_at})
    else:
        psup_sem = support_semantic(pick, by_path)
        psup_path = pick.get("support")
    if pick:
        # destination of a DIFFERENT semantic class than the pickup's support
        d = None
        for cand in dests:
            if cand["prim_path"] == pick["prim_path"]:
                continue
            if psup_sem and cand["semantic"] == psup_sem:
                continue
            d = cand; break
        if d:
            if (not synthetic) and pick["on_floor"]:
                u, t = floor_state; floor_state[0] = u + 1
            for level, facing in (("L3", "face"), ("L4", "back")):
                p1 = make_phase(pick, "PICK_UP", nl(pick["factory"]), 1.0, place_at=pick_place_at)
                p1["target_object_semantic"] = pick["semantic"]
                p1["_support_path"] = psup_path
                p1["_pickup_support_sem"] = psup_sem
                p2 = make_phase(d, "STOP", nl(d["factory"]), 1.5)
                p2["target_object_semantic"] = d["semantic"]
                add(level, [p1, p2], facing)
    return tasks


def main():
    sel = os.environ.get("SCENES", "").strip()
    if sel:
        fact_files = [os.path.join(BASE, s, "scene_facts.json") for s in sel.split(",")]
    else:
        fact_files = sorted(glob.glob(os.path.join(BASE, "*", "scene_facts.json")))

    all_tasks, dropped = [], []
    floor_state = [0, 0]  # [floor_pickups_used, total_pickups] (running cap)
    # first pass to estimate total pickups for the cap denominator
    nfiles = 0
    for ff in fact_files:
        if os.path.exists(ff):
            nfiles += 1
    floor_state[1] = max(1, nfiles)  # ~1 pick task/scene; rough denominator

    for ff in fact_files:
        if not os.path.exists(ff):
            print(f"[GEN] missing {ff}")
            continue
        facts = json.load(open(ff))
        scene_dir_name = os.path.basename(os.path.dirname(ff))
        ts = gen_scene(facts, scene_dir_name, floor_state, dropped)
        all_tasks.extend(ts)
        print(f"[GEN] {facts['scene_name']}: {len(ts)} tasks")

    with open(OUT_TASKS, "w") as f:
        json.dump({"tasks": all_tasks}, f, indent=2)
    with open(OUT_DROPPED, "w") as f:
        json.dump({"dropped": dropped}, f, indent=2)
    npick = sum(1 for t in all_tasks for p in t["phases"] if p["action"] == "PICK_UP")
    enc = sum(1 for d in dropped if d["reason"] == "enclosed_in_closed_container")
    print(f"[GEN] wrote {len(all_tasks)} tasks ({npick} pickup phases, "
          f"{floor_state[0]} on-floor pickups) -> {OUT_TASKS}")
    print(f"[GEN] dropped {len(dropped)} ({enc} enclosed-in-closed-container) -> {OUT_DROPPED}")


if __name__ == "__main__":
    main()
