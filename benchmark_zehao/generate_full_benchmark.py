#!/usr/bin/env python3
"""
Generate the FULL benchmark task set (L2/L3/L4) across all 122 scenes.

Design principles:
  1. NO L1 (too easy, 90%+ SR with Qwen 30B, no discrimination)
  2. Task diversity: pick_place, turn_on, navigate_to, pick_deliver
  3. Object diversity: different graspable objects per scene (not just books)
  4. Room-aware instructions: kitchen→pot/plate, bedroom→pillow, bathroom→towel
  5. Destination diversity: different furniture targets per room type
  6. Avoid repetitive instructions: randomize object/dest pairings
"""
import json, os, re, glob, random
from collections import Counter

random.seed(42)  # reproducible

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(_HERE, 'full_scenarios_extracted')
OUT_DIR = _HERE

# ══════════════════════════════════════════════════════════════
# Factory classification — comprehensive list
# ══════════════════════════════════════════════════════════════

PORTABLE_FACTORIES = {
    # Books
    'BookFactory', 'BookStackFactory', 'BookColumnFactory', 'SupportBookStackFactory',
    # Kitchen items
    'CupFactory', 'PlateFactory', 'BowlFactory', 'PotFactory', 'JarFactory',
    'WineglassFactory', 'MugFactory', 'GlassFactory', 'SpoonFactory',
    'CanFactory', 'BottleFactory', 'FoodBagFactory',
    # Fruit / food
    'FruitContainerFactory', 'FruitFactory',
    # Decor
    'NatureShelfTrinketsFactory', 'VaseFactory',
    # Bedroom
    'PillowFactory', 'TowelFactory',
}

SWITCHABLE_FACTORIES = {
    'FloorLampFactory', 'DeskLampFactory', 'TableLampFactory',
    'OvenFactory', 'MonitorFactory', 'TVStandFactory', 'TVFactory',
}

DESTINATION_FACTORIES = {
    'SimpleBookcaseFactory', 'LargeShelfFactory', 'CellShelfFactory',
    'SimpleDeskFactory', 'KitchenIslandFactory', 'KitchenSpaceFactory',
    'KitchenCabinetFactory',
    'SingleCabinetFactory', 'SideTableFactory',
    'DiningTableFactory', 'BarChairFactory',
    'SinkFactory', 'BedFactory',
    'LargePlantContainerFactory',
    'ChairFactory',
}

# ── Human-readable names ──
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

# ── Room inference from props ──
def infer_room(factories, scene_name):
    """Infer room type from factory list and scene name."""
    sn = scene_name.lower()
    # Explicit from scene name
    for kw, room in [('kitchen','kitchen'), ('cooking','kitchen'), ('counter','kitchen'),
                     ('island','kitchen'), ('breakfast','kitchen'),
                     ('bathroom','bathroom'), ('vanity','bathroom'), ('tub','bathroom'),
                     ('compact_storage','bathroom'),
                     ('bedroom','bedroom'), ('guest_room','bedroom'), ('study','bedroom'),
                     ('decor_storage','bedroom'),
                     ('dining','dining'), ('formal_center','dining'), ('gallery_table','dining'),
                     ('living','living'), ('media','living'), ('lounge','living'),
                     ('bar_corner','living'), ('social','living'), ('reading','living')]:
        if kw in sn:
            return room
    # Infer from props
    fset = set(factories)
    has = lambda kw: any(kw in f for f in fset)
    if has('Toilet'): return 'bathroom'
    if has('Oven') or has('Kitchen') or has('Sink'): return 'kitchen'
    if has('Bed') or has('Mattress') or has('Pillow'): return 'bedroom'
    if has('DiningTable'): return 'dining'
    return 'living'

# ── Extract base factory from prop name ──
def get_factory(name):
    """Extract factory type from names like '123456_BookFactory' or
    'PillowFactory_123_spawn_456'."""
    # Pattern 1: 123456_BookFactory
    m = re.match(r'\d+_(.*Factory)', name)
    if m: return m.group(1)
    # Pattern 2: BookFactory_123_spawn_456 or BookFactory_123_spawn_456__001
    m = re.match(r'([A-Z][a-zA-Z]+Factory)', name)
    if m: return m.group(1)
    return name

# ── Instruction templates ──
def make_pick_place_instruction(obj_nl, dest_nl, room):
    templates = [
        f"Pick up the {obj_nl} and bring it to the {dest_nl}.",
        f"Grab the {obj_nl} and place it on the {dest_nl}.",
        f"Take the {obj_nl} to the {dest_nl}.",
    ]
    return random.choice(templates)

def make_turn_on_instruction(obj_nl, room):
    templates = [
        f"Find and turn on the {obj_nl}.",
        f"Navigate to the {obj_nl} and turn it on.",
        f"Go to the {obj_nl} and switch it on.",
    ]
    return random.choice(templates)

def make_navigate_instruction(obj_nl, room):
    templates = [
        f"Navigate to the {obj_nl}.",
        f"Find and approach the {obj_nl}.",
        f"Go to the {obj_nl}.",
    ]
    return random.choice(templates)

# ══════════════════════════════════════════════════════════════
# Main generation
# ══════════════════════════════════════════════════════════════

scene_dirs = sorted(glob.glob(os.path.join(BASE, '*')))
all_tasks = []
stats = Counter()
skipped = []

for scene_dir in scene_dirs:
    if not os.path.isdir(scene_dir): continue
    scene_name = os.path.basename(scene_dir)
    short = scene_name.replace('native_','').replace('_full_physics_scene','')
    
    # Read spec
    spec_files = glob.glob(os.path.join(scene_dir, 'compiled_specs', '*.json'))
    if not spec_files:
        skipped.append((short, 'no_spec')); continue
    spec = json.load(open(spec_files[0]))
    
    # Find stage
    stage_files = glob.glob(os.path.join(scene_dir, 'compiled_stages', '*.usda'))
    if not stage_files:
        skipped.append((short, 'no_stage')); continue
    stage_rel = os.path.relpath(stage_files[0], _HERE)
    
    props = spec.get('interactive_props', [])
    humans = spec.get('active_humans', [])
    human_names = [h.get('name','?') for h in humans]
    
    # Classify props
    portables = []  # (original_name, factory, prim_path, sim_role)
    switchables = []
    destinations = []
    all_factories_list = []
    
    for p in props:
        name = p.get('name', '')
        factory = get_factory(name)
        prim = p.get('target_prim_path', '')
        role = p.get('sim_role', 'static')
        all_factories_list.append(factory)
        
        if factory in PORTABLE_FACTORIES:
            portables.append((name, factory, prim, role))
        if factory in SWITCHABLE_FACTORIES:
            switchables.append((name, factory, prim, role))
        if factory in DESTINATION_FACTORIES:
            destinations.append((name, factory, prim, role))
    
    room = infer_room(all_factories_list, short)
    
    # Infer category
    if '_official_' in scene_name: category = '80_official'
    elif '_text_' in scene_name or '_mask_' in scene_name: category = '40_textmask'
    elif '_scene_gen_v5_' in scene_name: category = '15_real2sim'
    else: category = '20_native'
    
    # Infer human motion type
    human_type = 'solo_run'
    sn_low = scene_name.lower()
    if 'two_runners' in sn_low or 'two_people' in sn_low: human_type = 'two_runners'
    elif 'run_dance' in sn_low: human_type = 'run_dance'
    elif 'run_jump' in sn_low: human_type = 'run_jump'
    elif any('obj_2' in h for h in human_names): human_type = 'two_runners'
    
    # ── De-duplicate factories (pick one representative per type) ──
    seen_portable_factories = set()
    unique_portables = []
    for p in portables:
        if p[1] not in seen_portable_factories:
            seen_portable_factories.add(p[1])
            unique_portables.append(p)
    
    seen_switch_factories = set()
    unique_switchables = []
    for s in switchables:
        if s[1] not in seen_switch_factories:
            seen_switch_factories.add(s[1])
            unique_switchables.append(s)
    
    seen_dest_factories = set()
    unique_destinations = []
    for d in destinations:
        if d[1] not in seen_dest_factories:
            seen_dest_factories.add(d[1])
            unique_destinations.append(d)
    
    # Shuffle for variety
    random.shuffle(unique_portables)
    random.shuffle(unique_switchables)
    random.shuffle(unique_destinations)
    
    # ── Determine best task type for this scene ──
    # Priority: pick_place > turn_on > navigate_only
    task_type = None
    pickup_obj = None
    dest_obj = None
    switch_obj = None
    nav_target = None
    
    if unique_portables and unique_destinations:
        task_type = 'pick_place'
        pickup_obj = unique_portables[0]
        # Pick a destination that's different from the pickup type
        dest_obj = unique_destinations[0]
    elif unique_switchables:
        task_type = 'turn_on'
        switch_obj = unique_switchables[0]
    elif unique_destinations:
        task_type = 'navigate'
        nav_target = unique_destinations[0]
    elif unique_portables:
        task_type = 'navigate'
        nav_target = unique_portables[0]
    elif unique_switchables:
        task_type = 'navigate'
        nav_target = unique_switchables[0]
    else:
        skipped.append((short, 'no_actionable_props')); continue
    
    # ── L2: Navigate task (always) ──
    # Pick the most interesting nav target
    l2_target = None
    for candidates in [unique_switchables, unique_destinations, unique_portables]:
        if candidates:
            l2_target = candidates[0]
            break
    
    if l2_target:
        t_name, t_factory, t_prim, _ = l2_target
        nl = FACTORY_NL.get(t_factory, t_factory.replace('Factory','').lower())
        instr = make_navigate_instruction(nl, room)
        all_tasks.append({
            'task_id': f"{short}-L2",
            'level': 'L2',
            'scene_dir': scene_name,
            'stage_path': stage_rel,
            'category': category,
            'room_type': room,
            'human_motion': human_type,
            'task_type': 'navigate',
            'instruction': instr,
            'target_prim': t_prim,
            'target_factory': t_factory,
            'target_semantic': nl,
            'spawn_facing': 'back',
            'needs_spawn_validation': True,
        })
        stats['L2'] += 1
    
    # ── L3/L4: Interaction tasks ──
    if task_type == 'pick_place':
        p_name, p_factory, p_prim, _ = pickup_obj
        d_name, d_factory, d_prim, _ = dest_obj
        obj_nl = FACTORY_NL.get(p_factory, p_factory.replace('Factory','').lower())
        dest_nl = FACTORY_NL.get(d_factory, d_factory.replace('Factory','').lower())
        instr = make_pick_place_instruction(obj_nl, dest_nl, room)
        
        for level, facing in [('L3', 'face'), ('L4', 'back')]:
            all_tasks.append({
                'task_id': f"{short}-{level}",
                'level': level,
                'scene_dir': scene_name,
                'stage_path': stage_rel,
                'category': category,
                'room_type': room,
                'human_motion': human_type,
                'task_type': 'pick_place',
                'instruction': instr,
                'pickup_prim': p_prim,
                'pickup_factory': p_factory,
                'pickup_semantic': obj_nl,
                'dest_prim': d_prim,
                'dest_factory': d_factory,
                'dest_semantic': dest_nl,
                'spawn_facing': facing,
                'needs_spawn_validation': True,
            })
            stats[level] += 1
    
    elif task_type == 'turn_on':
        s_name, s_factory, s_prim, _ = switch_obj
        obj_nl = FACTORY_NL.get(s_factory, s_factory.replace('Factory','').lower())
        instr = make_turn_on_instruction(obj_nl, room)
        
        for level, facing in [('L3', 'face'), ('L4', 'back')]:
            all_tasks.append({
                'task_id': f"{short}-{level}",
                'level': level,
                'scene_dir': scene_name,
                'stage_path': stage_rel,
                'category': category,
                'room_type': room,
                'human_motion': human_type,
                'task_type': 'turn_on',
                'instruction': instr,
                'target_prim': s_prim,
                'target_factory': s_factory,
                'target_semantic': obj_nl,
                'spawn_facing': facing,
                'needs_spawn_validation': True,
            })
            stats[level] += 1
    
    elif task_type == 'navigate':
        n_name, n_factory, n_prim, _ = nav_target
        obj_nl = FACTORY_NL.get(n_factory, n_factory.replace('Factory','').lower())
        instr = make_navigate_instruction(obj_nl, room)
        
        for level, facing in [('L3', 'face'), ('L4', 'back')]:
            all_tasks.append({
                'task_id': f"{short}-{level}",
                'level': level,
                'scene_dir': scene_name,
                'stage_path': stage_rel,
                'category': category,
                'room_type': room,
                'human_motion': human_type,
                'task_type': 'navigate',
                'instruction': instr,
                'target_prim': n_prim,
                'target_factory': n_factory,
                'target_semantic': obj_nl,
                'spawn_facing': facing,
                'needs_spawn_validation': True,
            })
            stats[level] += 1

# ══════════════════════════════════════════════════════════════
# Save & Report
# ══════════════════════════════════════════════════════════════

out_path = os.path.join(OUT_DIR, 'benchmark_tasks_full.json')
with open(out_path, 'w') as f:
    json.dump({'tasks': all_tasks, 'metadata': {
        'version': '2.0',
        'description': '4DSynth-Nav full benchmark: L2/L3/L4 across 122 scenes',
        'levels': {
            'L2': 'Navigate to target (agent starts BACK to target)',
            'L3': 'Interact with target (agent starts FACE target)',
            'L4': 'Interact with target (agent starts BACK to target)',
        },
        'task_types': ['navigate', 'pick_place', 'turn_on'],
        'seed': 42,
    }}, f, indent=2)

print(f"{'='*70}")
print(f"FULL BENCHMARK GENERATED")
print(f"{'='*70}")
print(f"Total tasks: {len(all_tasks)}")
print(f"  L2: {stats['L2']}")
print(f"  L3: {stats['L3']}")
print(f"  L4: {stats['L4']}")
print()

# Task type distribution
type_counts = Counter(t['task_type'] for t in all_tasks)
print(f"Task types:")
for tt, c in type_counts.most_common():
    print(f"  {tt:20s} {c:4d} ({100*c/len(all_tasks):.1f}%)")

# Room distribution
room_counts = Counter(t['room_type'] for t in all_tasks)
print(f"\nRoom types:")
for rt, c in room_counts.most_common():
    print(f"  {rt:20s} {c:4d} ({100*c/len(all_tasks):.1f}%)")

# Category distribution
cat_counts = Counter(t['category'] for t in all_tasks)
print(f"\nScene categories:")
for cc, c in cat_counts.most_common():
    print(f"  {cc:20s} {c:4d} ({100*c/len(all_tasks):.1f}%)")

# Human motion distribution
motion_counts = Counter(t['human_motion'] for t in all_tasks)
print(f"\nHuman motion types:")
for hm, c in motion_counts.most_common():
    print(f"  {hm:20s} {c:4d} ({100*c/len(all_tasks):.1f}%)")

# Instruction diversity
instrs = Counter(t['instruction'] for t in all_tasks)
print(f"\nInstruction diversity:")
print(f"  Unique instructions: {len(instrs)}")
print(f"  Top 10:")
for inst, c in instrs.most_common(10):
    print(f"    [{c:2d}x] {inst}")

# Object diversity (pickup + turn_on targets)
obj_counts = Counter()
for t in all_tasks:
    if t['task_type'] == 'pick_place':
        obj_counts[t.get('pickup_semantic','')] += 1
    elif t['task_type'] == 'turn_on':
        obj_counts[t.get('target_semantic','')] += 1
    else:
        obj_counts[t.get('target_semantic','')] += 1
print(f"\nTarget object distribution:")
for obj, c in obj_counts.most_common(15):
    print(f"  {obj:25s} {c:4d}")

if skipped:
    print(f"\nSkipped {len(skipped)} scenes:")
    for s, reason in skipped:
        print(f"  {s}: {reason}")

print(f"\nSaved to: {out_path}")
