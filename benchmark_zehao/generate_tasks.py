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
REACH_PICKUP_M = 1.0   # pickup radius (must match validate_all_spawns phase radius)
REACH_DEST_M   = 1.5   # destination/nav radius

def reach_ok(o, thr):
    """Reachable within `thr` of a walkable cell (matches validate's per-radius gate).
    Pre-reachability scene_facts (no reach_dist key) fall back to the old bool."""
    if "reach_dist" not in o:
        return o.get("reachable", True)
    rd = o["reach_dist"]
    return rd is not None and rd <= thr

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


def make_phase(obj, action, desc, radius, place_at=None, reach_half_extent=0.0):
    # When place_at is set (fallback relocation), the target center IS the placed
    # position (validator + runner use it), not the object's authored center.
    # reach_half_extent = half-extent of the SUPPORT furniture for pickups: reach/success
    # is measured to the support's EDGE (you reach a tabletop object by walking to the
    # table), while LOS/FOV stay on the object. validator + runner use it via edge-distance.
    center = list(place_at[:3]) if place_at else obj["center"]
    return {"name": f"{action.lower()}_{obj['factory']}", "target_object": obj["factory"],
            "target_prim": obj["prim_path"], "radius": radius, "action": action,
            "desc": desc, "place_at": place_at, "reach_half_extent": round(reach_half_extent, 4),
            "_center": center}


def pickup_reach_ok(o):
    """Pickup reachable if the agent can get within the pickup radius of the EDGE of the
    furniture it rests on (reach across the surface), not its exact center. Floor objects
    (support_he 0) reduce to the plain distance check."""
    if "reach_dist" not in o:
        return o.get("reachable", True)
    rd = o["reach_dist"]
    if rd is None:
        return False
    return (rd - o.get("support_he", 0.0)) <= REACH_PICKUP_M


# ── Fallback placement (when a scene has no valid existing pickup) ──
# Prefer a REACHABLE furniture top at camera-view height (no tilt-down) over the floor.
CAM_SURFACE_Z = (0.55, 1.05)  # furniture-top z visible from eye 1.58m / pitch -10 w/o tilt

def synth_pickup_on_surface(objects):
    """Pick a reachable camera-height furniture surface + a distinct clear-noun portable
    to RELOCATE onto it (place_at). Returns (obj, place_at_xyz, surface) or None.
    Floor is intentionally NOT used (forces tilt-down, VLM-hard)."""
    # Surface must be reachable within the PICKUP radius (1.0m) AND small enough that its
    # CENTER is within pickup reach of a walkable cell — otherwise placing at the center
    # (e.g. the middle of a bed/large table) is unreachable. reach_dist is measured to the
    # object center, so reach_ok(o, REACH_PICKUP_M) already guarantees center-reachability.
    surfaces = [o for o in objects if o["factory"] in DESTINATION_FACTORIES
                and reach_ok(o, REACH_PICKUP_M)
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


def gen_scene(facts, scene_dir_name, floor_state, dropped, mix_state):
    scene = facts["scene_name"]
    objects = facts["objects"]
    by_path = {o["prim_path"]: o for o in objects}
    counts = facts["semantic_class_counts"]
    room = infer_room_type(scene)
    cat = infer_category(scene)
    sd = f"full_scenarios_extracted/{scene_dir_name}"

    def uniq(o):  # semantic class unique in this room
        return counts.get(o["semantic"], 0) == 1

    # Candidate pools — clear nouns (no "trinket"), and reachable within the SAME
    # radius the validator uses (pickup 1.0m, destination 1.5m). Using reach_dist with
    # per-purpose thresholds (not the lenient 1.5m bool) makes probe agree with validate
    # → fewer validation drops. reach_ok() keeps backward-compat with pre-reachability facts.
    pickups = [o for o in objects
               if o["factory"] in CLEAR_PICKUP_FACTORIES and pickup_reach_ok(o)]
    dests = [o for o in objects
             if o["factory"] in DESTINATION_FACTORIES and reach_ok(o, REACH_DEST_M)]
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
        elif phases[0]["action"] == "PICK_UP":
            instr = f"Pick up the {phases[0]['desc']} and bring it to the {phases[1]['desc']}."
        else:
            # two-waypoint navigation (phase-1 is a plain STOP, not a pickup)
            instr = f"First go to the {phases[0]['desc']}, then go to the {phases[1]['desc']}."
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
            "task_type": ("navigate" if len(phases) == 1
                          else "pick_place" if phases[0]["action"] == "PICK_UP"
                          else "two_nav"),
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

    # ── L3 / L4: two-phase tasks. phase-1 is EITHER a pickup ("pick up X, bring to Y")
    # OR a plain navigation waypoint ("first go to X, then go to Y"). We target a global
    # ~60% nav / ~40% pickup split across scenes (mix_state = [nav_used, pickup_used]),
    # picking whichever variant is feasible AND currently under quota. Both variants keep
    # the same two-phase shape and the same phase-2 destination semantics. ──
    NAV_FRACTION = 0.6

    def build_pickup_variant():
        """Return (phases_template_fn, on_floor_obj) or None.
        phases_template_fn(level, facing) emits a [PICK_UP, STOP] pair."""
        pick = valid_pickup()
        pick_place_at = None
        synthetic = False
        surf = None
        if pick is None:
            syn = synth_pickup_on_surface(objects)
            if syn:
                pick, pick_place_at, surf = syn
                synthetic = True
                psup_sem = surf["semantic"]
                psup_path = None
                dropped.append({"id": f"{scene}-pickup",
                                "reason": "no_existing_pickup_used_place_at_fallback",
                                "object": pick["prim_path"], "surface": surf["prim_path"],
                                "place_at": pick_place_at})
            else:
                return None
        else:
            psup_sem = support_semantic(pick, by_path)
            psup_path = pick.get("support")
        # destination of a DIFFERENT semantic class than the pickup's support
        d = None
        for cand in dests:
            if cand["prim_path"] == pick["prim_path"]:
                continue
            if psup_sem and cand["semantic"] == psup_sem:
                continue
            d = cand; break
        if d is None:
            return None
        reach_he = surf["half_extent_xy"] if synthetic else pick.get("support_he", 0.0)

        def emit(level, facing):
            p1 = make_phase(pick, "PICK_UP", nl(pick["factory"]), 1.0,
                            place_at=pick_place_at, reach_half_extent=reach_he)
            p1["target_object_semantic"] = pick["semantic"]
            p1["_support_path"] = psup_path
            p1["_pickup_support_sem"] = psup_sem
            p2 = make_phase(d, "STOP", nl(d["factory"]), 1.5)
            p2["target_object_semantic"] = d["semantic"]
            add(level, [p1, p2], facing)
        return emit, (pick if (not synthetic and pick["on_floor"]) else None)

    def build_nav_variant():
        """Two distinct reachable destinations (waypoint -> final). Returns emit fn or None."""
        if len(dests) < 2:
            return None
        # distinct prims AND (preferably) distinct semantic classes for an unambiguous route
        wp = dests[0]
        final = None
        for cand in dests[1:]:
            if cand["prim_path"] == wp["prim_path"]:
                continue
            final = cand; break
        if final is None:
            return None

        def emit(level, facing):
            p1 = make_phase(wp, "STOP", nl(wp["factory"]), 1.5)
            p1["target_object_semantic"] = wp["semantic"]
            p2 = make_phase(final, "STOP", nl(final["factory"]), 1.5)
            p2["target_object_semantic"] = final["semantic"]
            add(level, [p1, p2], facing)
        return emit

    nav_emit = build_nav_variant()
    pick_built = build_pickup_variant()
    pick_emit = pick_built[0] if pick_built else None

    # choose variant: honor global ratio when both feasible, else take whichever exists
    chosen = None
    if nav_emit and pick_emit:
        nav_used, pick_used = mix_state
        total = nav_used + pick_used
        cur_nav_frac = (nav_used / total) if total else 0.0
        chosen = "nav" if cur_nav_frac < NAV_FRACTION else "pickup"
    elif nav_emit:
        chosen = "nav"
    elif pick_emit:
        chosen = "pickup"

    if chosen == "nav":
        mix_state[0] += 1
        for level, facing in (("L3", "face"), ("L4", "back")):
            nav_emit(level, facing)
    elif chosen == "pickup":
        mix_state[1] += 1
        on_floor_obj = pick_built[1]
        if on_floor_obj is not None:
            floor_state[0] += 1
        for level, facing in (("L3", "face"), ("L4", "back")):
            pick_emit(level, facing)
    return tasks


def main():
    sel = os.environ.get("SCENES", "").strip()
    if sel:
        fact_files = [os.path.join(BASE, s, "scene_facts.json") for s in sel.split(",")]
    else:
        fact_files = sorted(glob.glob(os.path.join(BASE, "*", "scene_facts.json")))

    all_tasks, dropped = [], []
    floor_state = [0, 0]  # [floor_pickups_used, total_pickups] (running cap)
    mix_state = [0, 0]    # [nav_variants_used, pickup_variants_used] (~60/40 target)
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
        ts = gen_scene(facts, scene_dir_name, floor_state, dropped, mix_state)
        all_tasks.extend(ts)
        print(f"[GEN] {facts['scene_name']}: {len(ts)} tasks")

    with open(OUT_TASKS, "w") as f:
        json.dump({"tasks": all_tasks}, f, indent=2)
    with open(OUT_DROPPED, "w") as f:
        json.dump({"dropped": dropped}, f, indent=2)
    npick = sum(1 for t in all_tasks for p in t["phases"] if p["action"] == "PICK_UP")
    enc = sum(1 for d in dropped if d["reason"] == "enclosed_in_closed_container")
    nav_v, pick_v = mix_state
    tot_v = nav_v + pick_v
    print(f"[GEN] wrote {len(all_tasks)} tasks ({npick} pickup phases, "
          f"{floor_state[0]} on-floor pickups) -> {OUT_TASKS}")
    print(f"[GEN] L3/L4 phase-1 mix: nav={nav_v} pickup={pick_v} "
          f"({(nav_v/tot_v*100 if tot_v else 0):.0f}% nav target 60%)")
    print(f"[GEN] dropped {len(dropped)} ({enc} enclosed-in-closed-container) -> {OUT_DROPPED}")


if __name__ == "__main__":
    main()
