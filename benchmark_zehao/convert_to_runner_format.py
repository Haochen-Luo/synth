#!/usr/bin/env python3
"""
Convert benchmark_tasks_full.json into runner-compatible format.

For spawn points: initially set to null. The bench_runner will auto-resolve
spawn points at runtime using the floodfill-based spawn validator.
"""
import json, os

IN = '/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/benchmark_tasks_full.json'
OUT = '/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/benchmark_tasks_full_runner.json'

data = json.load(open(IN))
runner_tasks = []

for t in data['tasks']:
    task_type = t['task_type']
    level = t['level']
    
    phases = []
    
    if task_type == 'pick_place':
        # Phase 1: navigate to pickup target and PICK_UP
        phases.append({
            'name': f'pick_{t.get("pickup_factory","obj")}',
            'target_object': t.get('pickup_factory', ''),
            'target_prim': t.get('pickup_prim', ''),
            'radius': 1.0,
            'action': 'PICK_UP',
            'desc': f'the {t.get("pickup_semantic", "object")}',
            'place_at': None,
        })
        # Phase 2: navigate to destination and STOP
        phases.append({
            'name': f'go_{t.get("dest_factory","dest")}',
            'target_object': t.get('dest_factory', ''),
            'target_prim': t.get('dest_prim', ''),
            'radius': 1.5,
            'action': 'STOP',
            'desc': f'the {t.get("dest_semantic", "destination")}',
            'place_at': None,
        })
    
    elif task_type == 'turn_on':
        # Phase 1: navigate to target
        phases.append({
            'name': f'go_{t.get("target_factory","obj")}',
            'target_object': t.get('target_factory', ''),
            'target_prim': t.get('target_prim', ''),
            'radius': 1.5,
            'action': 'STOP',
            'desc': f'the {t.get("target_semantic", "object")}',
            'place_at': None,
        })
        # Phase 2: turn on
        phases.append({
            'name': f'turnon_{t.get("target_factory","obj")}',
            'target_object': t.get('target_factory', ''),
            'target_prim': t.get('target_prim', ''),
            'radius': 1.5,
            'action': 'TURN_ON',
            'desc': f'the {t.get("target_semantic", "object")}',
            'place_at': None,
        })
    
    elif task_type == 'navigate':
        # Phase 1: navigate to target and STOP
        phases.append({
            'name': f'go_{t.get("target_factory","obj")}',
            'target_object': t.get('target_factory', ''),
            'target_prim': t.get('target_prim', ''),
            'radius': 1.5,
            'action': 'STOP',
            'desc': f'the {t.get("target_semantic", "object")}',
            'place_at': None,
        })
    
    runner_task = {
        'id': t['task_id'],
        'level': level,
        'scene_dir': t['scene_dir'],
        'instruction': t['instruction'],
        'agent_start': None,    # auto-resolve at runtime
        'agent_yaw': None,      # auto-resolve at runtime
        'spawn_facing': t.get('spawn_facing', 'back'),
        'phases': phases,
        # Metadata (not used by runner, but useful for analysis)
        'category': t.get('category', ''),
        'room_type': t.get('room_type', ''),
        'human_motion': t.get('human_motion', ''),
        'task_type': task_type,
    }
    runner_tasks.append(runner_task)

with open(OUT, 'w') as f:
    json.dump({'tasks': runner_tasks}, f, indent=2)

print(f"Converted {len(runner_tasks)} tasks")
print(f"  with agent_start=null (auto-spawn at runtime)")
print(f"Saved to: {OUT}")

# Summary
from collections import Counter
levels = Counter(t['level'] for t in runner_tasks)
types = Counter(t['task_type'] for t in runner_tasks)
phase_counts = Counter(len(t['phases']) for t in runner_tasks)
print(f"\nLevels: {dict(levels)}")
print(f"Types: {dict(types)}")
print(f"Phase counts: {dict(phase_counts)}")
