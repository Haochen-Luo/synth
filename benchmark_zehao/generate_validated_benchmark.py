#!/usr/bin/env python3
"""Generate validated benchmark tasks from spawn_cache.

Reads spawn_cache/{scene}.json files produced by validate_full_spawns.py
and generates benchmark_tasks_full_runner.json with verified spawn points.

Run this AFTER validate_full_spawns.py completes.
"""
import json, os, re, glob, math, random

random.seed(42)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, "spawn_cache")
OUT = os.path.join(SCRIPT_DIR, "benchmark_tasks_full_runner.json")

FACTORY_NL = {
    'BookFactory': 'book', 'BookStackFactory': 'stack of books',
    'BookColumnFactory': 'book column', 'SupportBookStackFactory': 'book stack',
    'CupFactory': 'cup', 'PlateFactory': 'plate', 'BowlFactory': 'bowl',
    'PotFactory': 'pot', 'JarFactory': 'jar', 'SpoonFactory': 'spoon',
    'WineglassFactory': 'wine glass', 'MugFactory': 'mug', 'GlassFactory': 'glass',
    'CanFactory': 'can', 'BottleFactory': 'bottle', 'FoodBagFactory': 'food bag',
    'FruitContainerFactory': 'fruit basket', 'FruitFactory': 'fruit',
    'NatureShelfTrinketsFactory': 'decorative trinket', 'VaseFactory': 'vase',
    'PillowFactory': 'pillow', 'TowelFactory': 'towel',
    'FloorLampFactory': 'floor lamp', 'DeskLampFactory': 'desk lamp',
    'TableLampFactory': 'table lamp',
    'OvenFactory': 'oven', 'MonitorFactory': 'monitor',
    'TVStandFactory': 'TV', 'TVFactory': 'TV',
    'SimpleBookcaseFactory': 'bookshelf', 'LargeShelfFactory': 'shelf',
    'CellShelfFactory': 'shelf', 'SimpleDeskFactory': 'desk',
    'KitchenIslandFactory': 'kitchen island', 'KitchenSpaceFactory': 'kitchen counter',
    'KitchenCabinetFactory': 'kitchen cabinet',
    'SingleCabinetFactory': 'cabinet', 'SideTableFactory': 'side table',
    'DiningTableFactory': 'dining table', 'BarChairFactory': 'bar stool',
    'SinkFactory': 'sink', 'BedFactory': 'bed',
    'LargePlantContainerFactory': 'large plant', 'ChairFactory': 'chair',
}

PORTABLE = {
    'BookFactory','BookStackFactory','BookColumnFactory','SupportBookStackFactory',
    'CupFactory','PlateFactory','BowlFactory','PotFactory','JarFactory',
    'WineglassFactory','MugFactory','GlassFactory','SpoonFactory',
    'CanFactory','BottleFactory','FoodBagFactory','FruitContainerFactory',
    'VaseFactory','PillowFactory','TowelFactory','NatureShelfTrinketsFactory',
}
SWITCHABLE = {
    'FloorLampFactory','DeskLampFactory','TableLampFactory',
    'OvenFactory','MonitorFactory','TVStandFactory','TVFactory',
}
DESTINATION = {
    'SimpleBookcaseFactory','LargeShelfFactory','CellShelfFactory',
    'SimpleDeskFactory','KitchenIslandFactory','KitchenSpaceFactory',
    'KitchenCabinetFactory','SingleCabinetFactory','SideTableFactory',
    'DiningTableFactory','BedFactory','SinkFactory','ChairFactory',
    'LargePlantContainerFactory',
}

def infer_room(factories, scene_name):
    sn = scene_name.lower()
    for kw, room in [('kitchen','kitchen'),('cooking','kitchen'),('counter','kitchen'),
                     ('island','kitchen'),('breakfast','kitchen'),
                     ('bathroom','bathroom'),('vanity','bathroom'),('tub','bathroom'),
                     ('compact_storage','bathroom'),
                     ('bedroom','bedroom'),('guest_room','bedroom'),('study','bedroom'),
                     ('decor_storage','bedroom'),
                     ('dining','dining'),('formal_center','dining'),('gallery_table','dining'),
                     ('living','living'),('media','living'),('lounge','living'),
                     ('bar_corner','living'),('social','living'),('reading','living')]:
        if kw in sn: return room
    fset = set(factories)
    has = lambda kw: any(kw in f for f in fset)
    if has('Toilet'): return 'bathroom'
    if has('Oven') or has('Kitchen') or has('Sink'): return 'kitchen'
    if has('Bed') or has('Mattress') or has('Pillow'): return 'bedroom'
    if has('DiningTable'): return 'dining'
    return 'living'

def make_instruction(task_type, obj_nl, dest_nl=None):
    templates = {
        'pick_place': [
            f"Pick up the {obj_nl} and bring it to the {dest_nl}.",
            f"Grab the {obj_nl} and place it on the {dest_nl}.",
            f"Take the {obj_nl} to the {dest_nl}.",
        ],
        'turn_on': [
            f"Find and turn on the {obj_nl}.",
            f"Navigate to the {obj_nl} and turn it on.",
            f"Go to the {obj_nl} and switch it on.",
        ],
        'navigate': [
            f"Navigate to the {obj_nl}.",
            f"Find and approach the {obj_nl}.",
            f"Go to the {obj_nl}.",
        ],
    }
    return random.choice(templates[task_type])


# ── Read all scene caches ──
all_tasks = []
scenes_processed = 0
scenes_skipped = 0

# Map scene dirs
scenes_base = os.path.join(SCRIPT_DIR, "full_scenarios_extracted")
for scene_dir in sorted(glob.glob(os.path.join(scenes_base, "native_*_full_physics_scene"))):
    scene_name = os.path.basename(scene_dir)
    short = scene_name.replace("native_","").replace("_full_physics_scene","")
    cache_file = os.path.join(CACHE_DIR, f"{short}.json")

    if not os.path.exists(cache_file):
        print(f"SKIP {short}: no spawn cache")
        scenes_skipped += 1
        continue

    cache = json.load(open(cache_file))
    reachable_props = cache.get("reachable_props", {})
    spawn_candidates = cache.get("spawn_candidates", [])

    if not spawn_candidates:
        print(f"SKIP {short}: no valid spawn candidates")
        scenes_skipped += 1
        continue

    # Classify reachable props
    portables = {}   # factory -> (prim_path, pos)
    switchables = {}
    destinations = {}
    all_factories = set()

    for factory, entries in reachable_props.items():
        all_factories.add(factory)
        if factory in PORTABLE:
            portables[factory] = entries[0]  # take first
        if factory in SWITCHABLE:
            switchables[factory] = entries[0]
        if factory in DESTINATION:
            destinations[factory] = entries[0]

    room = infer_room(all_factories, short)

    # Infer category
    if '_official_' in scene_name: category = '80_official'
    elif '_text_' in scene_name or '_mask_' in scene_name: category = '40_textmask'
    elif '_scene_gen_v5_' in scene_name: category = '15_real2sim'
    else: category = '20_native'

    # Infer human motion
    sn_low = scene_name.lower()
    if 'two_runners' in sn_low or 'two_people' in sn_low: human_motion = 'two_runners'
    elif 'run_dance' in sn_low: human_motion = 'run_dance'
    elif 'run_jump' in sn_low: human_motion = 'run_jump'
    else: human_motion = 'solo_run'

    # ── Determine task type ──
    task_type = None
    pickup = dest = switch = nav_target = None

    if portables and destinations:
        task_type = 'pick_place'
        pk = list(portables.keys()); random.shuffle(pk)
        pickup_factory = pk[0]
        pickup = portables[pickup_factory]
        dk = list(destinations.keys()); random.shuffle(dk)
        dest_factory = dk[0]
        dest = destinations[dest_factory]
    elif switchables:
        task_type = 'turn_on'
        sk = list(switchables.keys()); random.shuffle(sk)
        switch_factory = sk[0]
        switch = switchables[switch_factory]
    elif destinations:
        task_type = 'navigate'
        dk = list(destinations.keys()); random.shuffle(dk)
        nav_target = destinations[dk[0]]
        nav_factory = dk[0]
    elif switchables:
        task_type = 'navigate'
        sk = list(switchables.keys()); random.shuffle(sk)
        nav_target = switchables[sk[0]]
        nav_factory = sk[0]
    else:
        print(f"SKIP {short}: no actionable reachable props")
        scenes_skipped += 1
        continue

    # ── Pick spawn point ──
    # Use deterministic seed per scene
    _rng = random.Random(hash(short) & 0xFFFFFFFF)

    def pick_spawn(target_pos, facing, candidates):
        """Pick spawn 3-7m from target, return (x, y, yaw)."""
        # Sort by distance to target, prefer 3-7m
        scored = []
        for sx, sy in candidates:
            d = math.sqrt((sx-target_pos[0])**2 + (sy-target_pos[1])**2)
            if d >= 2.0:
                score = abs(d - 5.0)  # prefer ~5m
                scored.append((score, sx, sy, d))
        scored.sort()
        if not scored:
            # Fallback: any candidate
            sx, sy = candidates[0]
            d = math.sqrt((sx-target_pos[0])**2 + (sy-target_pos[1])**2)
        else:
            # Pick from top 10 randomly
            pick = _rng.choice(scored[:min(10, len(scored))])
            _, sx, sy, d = pick
        face_yaw = math.degrees(math.atan2(target_pos[1]-sy, target_pos[0]-sx))
        if facing == 'back':
            yaw = face_yaw + 180
        else:
            yaw = face_yaw
        yaw = ((yaw + 180) % 360) - 180
        return round(sx, 2), round(sy, 2), round(yaw, 1)

    # Get first target position
    if task_type == 'pick_place':
        first_target_pos = pickup[1]
    elif task_type == 'turn_on':
        first_target_pos = switch[1]
    else:
        first_target_pos = nav_target[1]

    # ── Generate L2, L3, L4 ──
    for level, facing in [('L2', 'back'), ('L3', 'face'), ('L4', 'back')]:
        sx, sy, yaw = pick_spawn(first_target_pos, facing, spawn_candidates)

        if level == 'L2':
            # Navigate only
            if switchables:
                t_factory = list(switchables.keys())[0]
                t_entry = switchables[t_factory]
            elif destinations:
                t_factory = list(destinations.keys())[0]
                t_entry = destinations[t_factory]
            else:
                t_factory = list(portables.keys())[0]
                t_entry = portables[t_factory]
            t_nl = FACTORY_NL.get(t_factory, t_factory.replace('Factory','').lower())
            instr = make_instruction('navigate', t_nl)
            phases = [{
                'name': f'go_{t_factory}',
                'target_object': t_factory,
                'target_prim': t_entry[0],
                'radius': 1.5,
                'action': 'STOP',
                'desc': f'the {t_nl}',
                'place_at': None,
            }]
            task_t = 'navigate'
        else:
            # L3/L4: interaction task
            if task_type == 'pick_place':
                obj_nl = FACTORY_NL.get(pickup_factory, pickup_factory.replace('Factory','').lower())
                dest_nl = FACTORY_NL.get(dest_factory, dest_factory.replace('Factory','').lower())
                instr = make_instruction('pick_place', obj_nl, dest_nl)
                phases = [
                    {
                        'name': f'pick_{pickup_factory}',
                        'target_object': pickup_factory,
                        'target_prim': pickup[0],
                        'radius': 1.0,
                        'action': 'PICK_UP',
                        'desc': f'the {obj_nl}',
                        'place_at': None,
                    },
                    {
                        'name': f'go_{dest_factory}',
                        'target_object': dest_factory,
                        'target_prim': dest[0],
                        'radius': 1.5,
                        'action': 'STOP',
                        'desc': f'the {dest_nl}',
                        'place_at': None,
                    },
                ]
                task_t = 'pick_place'
            elif task_type == 'turn_on':
                obj_nl = FACTORY_NL.get(switch_factory, switch_factory.replace('Factory','').lower())
                instr = make_instruction('turn_on', obj_nl)
                phases = [
                    {
                        'name': f'go_{switch_factory}',
                        'target_object': switch_factory,
                        'target_prim': switch[0],
                        'radius': 1.5,
                        'action': 'STOP',
                        'desc': f'the {obj_nl}',
                        'place_at': None,
                    },
                    {
                        'name': f'turnon_{switch_factory}',
                        'target_object': switch_factory,
                        'target_prim': switch[0],
                        'radius': 1.5,
                        'action': 'TURN_ON',
                        'desc': f'the {obj_nl}',
                        'place_at': None,
                    },
                ]
                task_t = 'turn_on'
            else:
                n_nl = FACTORY_NL.get(nav_factory, nav_factory.replace('Factory','').lower())
                instr = make_instruction('navigate', n_nl)
                phases = [{
                    'name': f'go_{nav_factory}',
                    'target_object': nav_factory,
                    'target_prim': nav_target[0],
                    'radius': 1.5,
                    'action': 'STOP',
                    'desc': f'the {n_nl}',
                    'place_at': None,
                }]
                task_t = 'navigate'

        all_tasks.append({
            'id': f'{short}-{level}',
            'level': level,
            'scene_dir': f'full_scenarios_extracted/{scene_name}',
            'instruction': instr,
            'agent_start': [sx, sy],
            'agent_yaw': yaw,
            'phases': phases,
            'category': category,
            'room_type': room,
            'human_motion': human_motion,
            'task_type': task_t,
        })

    scenes_processed += 1

# Save
with open(OUT, 'w') as f:
    json.dump({'tasks': all_tasks}, f, indent=2)

from collections import Counter
levels = Counter(t['level'] for t in all_tasks)
types = Counter(t['task_type'] for t in all_tasks)

print(f"\n{'='*60}")
print(f"Generated {len(all_tasks)} validated tasks from {scenes_processed} scenes")
print(f"Skipped: {scenes_skipped}")
print(f"Levels: {dict(levels)}")
print(f"Types: {dict(types)}")
print(f"Saved to: {OUT}")
