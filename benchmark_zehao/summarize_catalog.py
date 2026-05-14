#!/usr/bin/env python3
"""Summarize scene catalog by object CATEGORY (not instance) across scenes."""
import json, os, re
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
catalog = json.load(open(os.path.join(BASE, "scene_catalog.json")))

def clean_type(t):
    """Remove numeric prefix from type like '139222_Book Column' -> 'Book Column'"""
    m = re.match(r'^\d+_(.+)$', t)
    return m.group(1) if m else t

# Per-scene object categories
scene_summaries = {}
category_scene_map = defaultdict(set)

for sname, sinfo in sorted(catalog.items()):
    room_type = "Living" if "living" in sname else "Dining"
    cats = defaultdict(list)
    for obj in sinfo["objects"]:
        cat = clean_type(obj["type"])
        if cat in ("Area", "window", "room_structure"):
            continue
        cats[cat].append(obj)
        category_scene_map[cat].add(sname)
    scene_summaries[sname] = {"room": room_type, "humans": len(sinfo["humans"]), "categories": cats}

print("=" * 80)
print("OBJECT CATEGORIES ACROSS ALL SCENES")
print("=" * 80)

# Sort by frequency
for cat, scenes_with in sorted(category_scene_map.items(), key=lambda x: (-len(x[1]), x[0])):
    print(f"  {cat:30s}  in {len(scenes_with):2d}/10 scenes: {', '.join(sorted(scenes_with)[:5])}")

print()
print("=" * 80)
print("PER-SCENE FURNITURE SUMMARY")
print("=" * 80)

nav_targets = []  # Collect good navigation targets

for sname, summary in sorted(scene_summaries.items()):
    room = summary["room"]
    n_humans = summary["humans"]
    print(f"\n--- {sname} ({room} room, {n_humans} humans) ---")
    for cat, objs in sorted(summary["categories"].items()):
        positions = []
        for obj in objs:
            c = obj.get("center")
            s = obj.get("size")
            if c and s:
                # Compute a reasonable interaction radius from max horizontal extent
                max_horiz = max(s[0], s[1]) / 2 + 1.0  # half-extent + 1m margin
                positions.append({
                    "center": c[:2],
                    "size": s,
                    "radius": round(min(max_horiz, 3.0), 1),
                })
        if positions:
            for p in positions:
                print(f"  {cat:25s}  center=({p['center'][0]:6.1f},{p['center'][1]:6.1f})  "
                      f"size={p['size'][0]:.1f}x{p['size'][1]:.1f}x{p['size'][2]:.1f}m  "
                      f"radius≈{p['radius']}m")
        else:
            print(f"  {cat:25s}  (no bbox)")

# Identify "large navigable" objects good for targets
print()
print("=" * 80)
print("CANDIDATE NAVIGATION TARGETS (large objects, good for 'go to X')")
print("=" * 80)

TARGET_CATEGORIES = {
    "Sofa", "Coffee Table", "Bookcase", "Simple Bookcase", "Large Shelf",
    "TV", "TVStand", "Table Dining", "Chair", "Single Cabinet", 
    "Simple Desk", "Kitchen Cabinet", "Rug", "Plant Container",
    "Large Plant Container", "Mirror", "Desk Lamp", "Cell Shelf",
    "Book Column", "Book Stack",
}

for sname, summary in sorted(scene_summaries.items()):
    room = summary["room"]
    print(f"\n  {sname} ({room}):")
    for cat, objs in sorted(summary["categories"].items()):
        if cat not in TARGET_CATEGORIES:
            continue
        for obj in objs:
            c = obj.get("center")
            s = obj.get("size")
            if c and s:
                max_horiz = max(s[0], s[1])
                area = s[0] * s[1]
                if area > 0.3:  # Minimum size for a meaningful target
                    radius = round(min(max_horiz/2 + 1.0, 3.0), 1)
                    desc = cat.lower()
                    print(f"    {cat:25s}  ({c[0]:5.1f},{c[1]:5.1f})  "
                          f"{s[0]:.1f}x{s[1]:.1f}x{s[2]:.1f}m  r={radius}m")

print()
print("=" * 80)
print("CANDIDATE PICKUP/INTERACT OBJECTS (small objects)")
print("=" * 80)

SMALL_CATEGORIES = {
    "Book Stack", "Book Column", "Cup", "Wineglass", "Plate", "Bowl",
    "Fork", "Knife", "Pot", "Desk Lamp", "Nature Shelf Trinkets",
    "Support Book Stack",
}

for sname, summary in sorted(scene_summaries.items()):
    room = summary["room"]
    items = []
    for cat, objs in sorted(summary["categories"].items()):
        if cat not in SMALL_CATEGORIES:
            continue
        for obj in objs:
            c = obj.get("center")
            s = obj.get("size")
            if c and s:
                items.append((cat, c, s))
    if items:
        print(f"\n  {sname} ({room}):")
        for cat, c, s in items:
            print(f"    {cat:25s}  ({c[0]:5.1f},{c[1]:5.1f})  {s[0]:.1f}x{s[1]:.1f}x{s[2]:.1f}m")

print()
print("=" * 80)
print("HUMAN POSITIONS")
print("=" * 80)
for sname, sinfo in sorted(catalog.items()):
    print(f"\n  {sname}:")
    for h in sinfo["humans"]:
        pos = h["position"]
        if len(pos) >= 2:
            print(f"    {h['name']:40s}  ({pos[0]:5.1f},{pos[1]:5.1f})  keyframes={h['trajectory_frames']}")
