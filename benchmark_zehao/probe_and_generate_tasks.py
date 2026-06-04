#!/usr/bin/env python3
"""
Probe all extracted scenes and auto-generate benchmark tasks (L2, L3, L4).

For each scene, reads the compiled spec JSON to identify:
  - Interactive props (by Factory type and sim_role)
  - Active humans (solo_run, two_runners, etc.)
  - Room type (inferred from scene name)

Then generates candidate tasks:
  - L2: Navigate to target (agent starts BACK to target)
  - L3: Pick & place or Turn-on (agent starts FACE target)
  - L4: Pick & place or Turn-on (agent starts BACK to target)

Output: A JSON file compatible with benchmark_tasks.json
"""
import json, os, re, glob, sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(_HERE, 'full_scenarios_extracted')
OUT_DIR = _HERE

# ── Factory classification ──
# Portable objects (can be PICK_UP'd) — dynamic or low mass
PORTABLE_FACTORIES = {
    'BookFactory', 'BookStackFactory', 'BookColumnFactory', 'SupportBookStackFactory',
    'CupFactory', 'PlateFactory', 'BowlFactory', 'BottleFactory', 'CanFactory',
    'JarFactory', 'WineglassFactory', 'FoodBagFactory', 'FruitContainerFactory',
    'PotFactory', 'VaseFactory', 'MugFactory', 'GlassFactory',
    'NatureShelfTrinketsFactory',  # small decorative items
}

# Switchable objects (can be TURN_ON'd)
SWITCHABLE_FACTORIES = {
    'FloorLampFactory', 'DeskLampFactory', 'TableLampFactory',
    'OvenFactory', 'MonitorFactory', 'TVStandFactory', 'TVFactory',
}

# Destination objects (navigate to / PUT_DOWN onto)
DESTINATION_FACTORIES = {
    'SimpleBookcaseFactory', 'LargeShelfFactory', 'CellShelfFactory',
    'SimpleDeskFactory', 'KitchenIslandFactory', 'KitchenSpaceFactory',
    'SingleCabinetFactory', 'SideTableFactory', 'DiningTableFactory',
    'BarChairFactory', 'SinkFactory', 'ToiletFactory',
    'LargePlantContainerFactory',  # navigate target
}

# ── Room type inference ──
def infer_room_type(scene_name):
    sn = scene_name.lower()
    if 'living' in sn or 'media' in sn or 'lounge' in sn or 'bar' in sn or 'social' in sn:
        return 'living'
    if 'bedroom' in sn or 'guest_room' in sn or 'study' in sn or 'decor' in sn:
        return 'bedroom'
    if 'kitchen' in sn or 'cooking' in sn or 'counter' in sn or 'island' in sn or 'breakfast' in sn:
        return 'kitchen'
    if 'dining' in sn or 'gallery_table' in sn or 'formal_center' in sn:
        return 'dining'
    if 'bathroom' in sn or 'vanity' in sn or 'tub' in sn or 'compact_storage' in sn:
        return 'bathroom'
    if 'office' in sn:
        return 'office'
    return 'unknown'

# ── Scene category from path ──
def infer_category(scene_name):
    sn = scene_name.lower()
    if '_official_' in sn:
        return '80_official'
    if '_text_' in sn or '_mask_' in sn:
        return '40_textmask'
    if '_scene_gen_v5_' in sn:
        return '15_real2sim'
    return '20_native'

# ── Extract Factory type from name like "123456_BookFactory" ──
def get_factory(name):
    m = re.match(r'\d+_(.*)', name)
    return m.group(1) if m else name

# ── Natural language for objects ──
FACTORY_TO_NL = {
    'BookFactory': 'book', 'BookStackFactory': 'book stack',
    'BookColumnFactory': 'book column', 'SupportBookStackFactory': 'book stack',
    'CupFactory': 'cup', 'PlateFactory': 'plate', 'BowlFactory': 'bowl',
    'BottleFactory': 'bottle', 'CanFactory': 'can',
    'JarFactory': 'jar', 'WineglassFactory': 'wine glass',
    'FoodBagFactory': 'food bag', 'FruitContainerFactory': 'fruit basket',
    'PotFactory': 'pot', 'VaseFactory': 'vase', 'MugFactory': 'mug',
    'GlassFactory': 'glass', 'NatureShelfTrinketsFactory': 'decorative trinket',
    'FloorLampFactory': 'floor lamp', 'DeskLampFactory': 'desk lamp',
    'TableLampFactory': 'table lamp',
    'OvenFactory': 'oven', 'MonitorFactory': 'monitor',
    'TVStandFactory': 'TV', 'TVFactory': 'TV',
    'SimpleBookcaseFactory': 'bookshelf', 'LargeShelfFactory': 'shelf',
    'CellShelfFactory': 'shelf', 'SimpleDeskFactory': 'desk',
    'KitchenIslandFactory': 'kitchen island', 'KitchenSpaceFactory': 'kitchen counter',
    'SingleCabinetFactory': 'cabinet', 'SideTableFactory': 'side table',
    'DiningTableFactory': 'dining table', 'BarChairFactory': 'bar stool',
    'SinkFactory': 'sink', 'ToiletFactory': 'toilet',
    'LargePlantContainerFactory': 'large plant',
}

# ── Destination preference by room ──
ROOM_DESTINATIONS = {
    'living': ['SimpleBookcaseFactory', 'SimpleDeskFactory', 'SideTableFactory',
               'LargeShelfFactory', 'CellShelfFactory', 'SingleCabinetFactory'],
    'bedroom': ['SimpleDeskFactory', 'SimpleBookcaseFactory', 'SideTableFactory',
                'SingleCabinetFactory', 'CellShelfFactory'],
    'kitchen': ['KitchenIslandFactory', 'KitchenSpaceFactory', 'SingleCabinetFactory',
                'SimpleDeskFactory'],
    'dining': ['SimpleDeskFactory', 'SingleCabinetFactory', 'LargeShelfFactory',
               'SideTableFactory'],
    'bathroom': ['SimpleDeskFactory', 'SingleCabinetFactory', 'SinkFactory'],
    'office': ['SimpleDeskFactory', 'SimpleBookcaseFactory', 'SingleCabinetFactory'],
    'unknown': ['SimpleDeskFactory', 'SimpleBookcaseFactory', 'SingleCabinetFactory'],
}

# ══════════════════════════════════════════════════════════════
# Main probe
# ══════════════════════════════════════════════════════════════

scene_dirs = sorted(glob.glob(os.path.join(BASE, '*')))
all_tasks = []
stats = Counter()
scene_summaries = []

for scene_dir in scene_dirs:
    if not os.path.isdir(scene_dir):
        continue
    scene_name = os.path.basename(scene_dir)
    short_name = scene_name.replace('native_', '').replace('_full_physics_scene', '')
    
    # Read compiled spec
    spec_files = glob.glob(os.path.join(scene_dir, 'compiled_specs', '*.json'))
    if not spec_files:
        stats['no_spec'] += 1
        continue
    
    try:
        spec = json.load(open(spec_files[0]))
    except Exception as e:
        stats['bad_spec'] += 1
        continue
    
    room_type = infer_room_type(short_name)
    category = infer_category(short_name)
    
    # Classify props
    props = spec.get('interactive_props', [])
    humans = spec.get('active_humans', [])
    
    portables = []  # (name, factory, prim_path)
    switchables = []
    destinations = []
    
    for p in props:
        name = p.get('name', '')
        factory = get_factory(name)
        prim = p.get('target_prim_path', '')
        role = p.get('sim_role', '')
        
        if factory in PORTABLE_FACTORIES:
            portables.append((name, factory, prim))
        if factory in SWITCHABLE_FACTORIES:
            switchables.append((name, factory, prim))
        if factory in DESTINATION_FACTORIES:
            destinations.append((name, factory, prim))
    
    human_names = [h.get('name', '?') for h in humans]
    human_count = len(humans)
    
    summary = {
        'scene': short_name,
        'scene_dir': scene_name,
        'category': category,
        'room_type': room_type,
        'total_props': len(props),
        'portables': [(n, f) for n, f, _ in portables],
        'switchables': [(n, f) for n, f, _ in switchables],
        'destinations': [(n, f) for n, f, _ in destinations],
        'humans': human_names,
    }
    scene_summaries.append(summary)
    
    # ── Generate tasks ──
    # Need at least one target to generate L2 (navigate only)
    nav_targets = portables + switchables + destinations
    if not nav_targets:
        stats['no_targets'] += 1
        continue
    
    # Pick best target for each task type
    # L2: Navigate to a visible landmark
    l2_target = None
    # Prefer: switchable > destination > portable (bigger objects easier to see)
    for candidates in [switchables, destinations, portables]:
        if candidates:
            l2_target = candidates[0]
            break
    
    # L3/L4: Pick & place OR turn on
    l3l4_type = None
    l3l4_pickup = None
    l3l4_dest = None
    l3l4_switch = None
    
    if portables and destinations:
        # Pick & place task
        l3l4_type = 'pick_place'
        l3l4_pickup = portables[0]
        # Find best destination
        room_dests = ROOM_DESTINATIONS.get(room_type, ROOM_DESTINATIONS['unknown'])
        for rd in room_dests:
            match = [d for d in destinations if d[1] == rd]
            if match:
                l3l4_dest = match[0]
                break
        if not l3l4_dest:
            l3l4_dest = destinations[0]
    elif switchables:
        # Turn on task
        l3l4_type = 'turn_on'
        l3l4_switch = switchables[0]
    elif portables:
        # Pick up only (no destination)
        l3l4_type = 'pick_only'
        l3l4_pickup = portables[0]
    
    # --- Build task entries ---
    # Compile stage path (relative)
    stage_files = glob.glob(os.path.join(scene_dir, 'compiled_stages', '*.usda'))
    if not stage_files:
        stats['no_stage'] += 1
        continue
    stage_rel = os.path.relpath(stage_files[0], _HERE)
    
    # L2: Navigate to target (back to target)
    if l2_target:
        t_name, t_factory, t_prim = l2_target
        nl_target = FACTORY_TO_NL.get(t_factory, t_factory.replace('Factory', '').lower())
        task_id = f"{short_name}-L2"
        all_tasks.append({
            'task_id': task_id,
            'level': 'L2',
            'scene_dir': scene_name,
            'stage_path': stage_rel,
            'category': category,
            'room_type': room_type,
            'task_type': 'navigate',
            'instruction': f"Navigate to the {nl_target}.",
            'target_prim': t_prim,
            'target_factory': t_factory,
            'spawn': {'facing': 'back'},
            'humans': human_names,
            'needs_validation': True,
        })
        stats['L2'] += 1
    
    # L3: Interaction task (face target)
    if l3l4_type == 'pick_place':
        p_name, p_factory, p_prim = l3l4_pickup
        d_name, d_factory, d_prim = l3l4_dest
        nl_obj = FACTORY_TO_NL.get(p_factory, p_factory.replace('Factory', '').lower())
        nl_dest = FACTORY_TO_NL.get(d_factory, d_factory.replace('Factory', '').lower())
        task_id = f"{short_name}-L3"
        all_tasks.append({
            'task_id': task_id,
            'level': 'L3',
            'scene_dir': scene_name,
            'stage_path': stage_rel,
            'category': category,
            'room_type': room_type,
            'task_type': 'pick_place',
            'instruction': f"Pick up the {nl_obj} and bring it to the {nl_dest}.",
            'pickup_prim': p_prim,
            'pickup_factory': p_factory,
            'dest_prim': d_prim,
            'dest_factory': d_factory,
            'spawn': {'facing': 'face'},
            'humans': human_names,
            'needs_validation': True,
        })
        stats['L3'] += 1
        
        # L4: Same but back to target
        task_id = f"{short_name}-L4"
        all_tasks.append({
            'task_id': task_id,
            'level': 'L4',
            'scene_dir': scene_name,
            'stage_path': stage_rel,
            'category': category,
            'room_type': room_type,
            'task_type': 'pick_place',
            'instruction': f"Pick up the {nl_obj} and bring it to the {nl_dest}.",
            'pickup_prim': p_prim,
            'pickup_factory': p_factory,
            'dest_prim': d_prim,
            'dest_factory': d_factory,
            'spawn': {'facing': 'back'},
            'humans': human_names,
            'needs_validation': True,
        })
        stats['L4'] += 1
    
    elif l3l4_type == 'turn_on':
        s_name, s_factory, s_prim = l3l4_switch
        nl_switch = FACTORY_TO_NL.get(s_factory, s_factory.replace('Factory', '').lower())
        task_id = f"{short_name}-L3"
        all_tasks.append({
            'task_id': task_id,
            'level': 'L3',
            'scene_dir': scene_name,
            'stage_path': stage_rel,
            'category': category,
            'room_type': room_type,
            'task_type': 'turn_on',
            'instruction': f"Find and turn on the {nl_switch}.",
            'target_prim': s_prim,
            'target_factory': s_factory,
            'spawn': {'facing': 'face'},
            'humans': human_names,
            'needs_validation': True,
        })
        stats['L3'] += 1
        
        task_id = f"{short_name}-L4"
        all_tasks.append({
            'task_id': task_id,
            'level': 'L4',
            'scene_dir': scene_name,
            'stage_path': stage_rel,
            'category': category,
            'room_type': room_type,
            'task_type': 'turn_on',
            'instruction': f"Find and turn on the {nl_switch}.",
            'target_prim': s_prim,
            'target_factory': s_factory,
            'spawn': {'facing': 'back'},
            'humans': human_names,
            'needs_validation': True,
        })
        stats['L4'] += 1

# ══════════════════════════════════════════════════════════════
# Output
# ══════════════════════════════════════════════════════════════

# Save probe summary
probe_out = os.path.join(OUT_DIR, 'scene_probe_summary.json')
with open(probe_out, 'w') as f:
    json.dump({
        'total_scenes': len(scene_summaries),
        'stats': dict(stats),
        'scenes': scene_summaries,
    }, f, indent=2)

# Save generated tasks
tasks_out = os.path.join(OUT_DIR, 'benchmark_tasks_full_draft.json')
with open(tasks_out, 'w') as f:
    json.dump({'tasks': all_tasks}, f, indent=2)

# Print summary
print(f"{'='*60}")
print(f"PROBE COMPLETE")
print(f"{'='*60}")
print(f"Scenes probed: {len(scene_summaries)}")
print(f"Scenes skipped: no_spec={stats.get('no_spec',0)}, bad_spec={stats.get('bad_spec',0)}, no_targets={stats.get('no_targets',0)}, no_stage={stats.get('no_stage',0)}")
print()
print(f"Tasks generated:")
print(f"  L2 (navigate, back): {stats.get('L2', 0)}")
print(f"  L3 (interact, face): {stats.get('L3', 0)}")
print(f"  L4 (interact, back): {stats.get('L4', 0)}")
print(f"  TOTAL: {sum(stats.get(l,0) for l in ['L2','L3','L4'])}")
print()

# Task type distribution
type_counts = Counter(t['task_type'] for t in all_tasks)
print(f"Task type distribution:")
for tt, c in type_counts.most_common():
    print(f"  {tt}: {c}")

# Room type distribution
room_counts = Counter(t['room_type'] for t in all_tasks)
print(f"\nRoom type distribution:")
for rt, c in room_counts.most_common():
    print(f"  {rt}: {c}")

# Category distribution
cat_counts = Counter(t['category'] for t in all_tasks)
print(f"\nCategory distribution:")
for cc, c in cat_counts.most_common():
    print(f"  {cc}: {c}")

# Instruction diversity
instrs = Counter(t['instruction'] for t in all_tasks)
print(f"\nUnique instructions: {len(instrs)}")
print(f"Top 5 most common:")
for inst, c in instrs.most_common(5):
    print(f"  [{c}x] {inst}")

print(f"\nSaved:")
print(f"  Probe: {probe_out}")
print(f"  Tasks: {tasks_out}")
