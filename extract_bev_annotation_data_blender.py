#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import bpy
from mathutils import Vector


USE_FAST_BBOX = False


TABLE_TOKENS = (
    "CoffeeTableFactory",
    "TableDiningFactory",
    "TableCocktailFactory",
    "SideTableFactory",
    "SimpleDeskFactory",
    "DeskFactory",
)

STORAGE_TOKENS = (
    "TVStandFactory",
    "SingleCabinetFactory",
    "KitchenCabinetFactory",
    "CabinetFactory",
    "SimpleBookcaseFactory",
    "LargeShelfFactory",
    "CellShelfFactory",
    "ShelfFactory",
    "BookcaseFactory",
)

FURNITURE_TOKENS = TABLE_TOKENS + STORAGE_TOKENS + (
    "ArmchairFactory",
    "ChairFactory",
    "DiningChairFactory",
    "OfficeChairFactory",
    "StoolFactory",
    "BedFactory",
    "MattressFactory",
    "BlanketFactory",
    "DresserFactory",
    "WardrobeFactory",
    "NightstandFactory",
    "SofaFactory",
    "RugFactory",
    "FloorLampFactory",
    "LargePlantContainerFactory",
    "PlantContainerFactory",
)

WALKABLE_ELEVATION_TOKENS = (
    "RugFactory",
    "Carpet",
    "MatFactory",
    "Doormat",
    "DoorMat",
    "Bathmat",
    "FloorMat",
)

DOOR_TOKENS = (
    "DoorFactory",
    "PanelDoorFactory",
)

# Keep this close to scripts/07_layout/place_base.py:is_walk_collision_ignored_env_object.
# BEV annotation must show every environment mesh that can block a walking actor;
# low floor coverings are drawn separately as walkable elevation surfaces.
NON_COLLISION_DECOR_TOKENS = (
    "WallArtFactory",
    "MirrorFactory",
    "WindowFactory",
    "CeilingLightFactory",
    "skirtingboard",
    "baseboard",
)

ROOM_KIND_PRIORITY = (
    "living-room",
    "dining-room",
    "bedroom",
    "kitchen",
    "hallway",
    "bathroom",
)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Extract BEV annotation geometry from a .blend.")
    ap.add_argument("--out_json", required=True)
    ap.add_argument(
        "--fast_bbox",
        action="store_true",
        help="Use transformed object bound_box rectangles for obstacle footprints. This is faster for dense generated scenes.",
    )
    ap.add_argument(
        "--safe_proxy_radius_m",
        type=float,
        default=0.40,
        help="Character cylinder radius used for place-compatible BEV safe-area sampling.",
    )
    ap.add_argument(
        "--safe_proxy_height_m",
        type=float,
        default=1.70,
        help="Character cylinder height used for place-compatible BEV safe-area sampling.",
    )
    ap.add_argument(
        "--safe_grid_step_m",
        type=float,
        default=0.15,
        help="XY grid step for place-compatible BEV safe-area sampling.",
    )
    ap.add_argument(
        "--safe_collision_margin_m",
        type=float,
        default=0.03,
        help="Broad-phase margin matching placement collision_margin.",
    )
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return ap.parse_args(argv)


def load_place_base():
    scripts_root = Path(__file__).resolve().parents[1]
    layout_dir = scripts_root / "07_layout"
    if str(layout_dir) not in sys.path:
        sys.path.insert(0, str(layout_dir))
    import place_base as place_base_mod  # type: ignore

    return place_base_mod


def bbox_world_from_bound_box(obj: bpy.types.Object) -> dict[str, Any] | None:
    if obj.type != "MESH":
        return None
    try:
        pts = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        if not pts:
            return None
        mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
        cen = (mn + mx) * 0.5
        return {
            "min": [float(mn.x), float(mn.y), float(mn.z)],
            "max": [float(mx.x), float(mx.y), float(mx.z)],
            "center": [float(cen.x), float(cen.y), float(cen.z)],
            "size": [float(mx.x - mn.x), float(mx.y - mn.y), float(mx.z - mn.z)],
        }
    except Exception:
        return None


def bbox_world(obj: bpy.types.Object) -> dict[str, Any] | None:
    if obj.type != "MESH":
        return None
    if USE_FAST_BBOX:
        return bbox_world_from_bound_box(obj)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    try:
        obj_eval = obj.evaluated_get(depsgraph)
        mesh = obj_eval.to_mesh()
        if mesh is None or not mesh.vertices:
            return None
        pts = [obj.matrix_world @ v.co for v in mesh.vertices]
        mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
        cen = (mn + mx) * 0.5
        return {
            "min": [float(mn.x), float(mn.y), float(mn.z)],
            "max": [float(mx.x), float(mx.y), float(mx.z)],
            "center": [float(cen.x), float(cen.y), float(cen.z)],
            "size": [float(mx.x - mn.x), float(mx.y - mn.y), float(mx.z - mn.z)],
        }
    except Exception:
        return None
    finally:
        try:
            obj_eval.to_mesh_clear()
        except Exception:
            pass


def polygon_area(poly: list[list[float]]) -> float:
    if len(poly) < 3:
        return 0.0
    acc = 0.0
    for idx, p0 in enumerate(poly):
        p1 = poly[(idx + 1) % len(poly)]
        acc += float(p0[0]) * float(p1[1]) - float(p1[0]) * float(p0[1])
    return acc * 0.5


def convex_hull(points: list[list[float]]) -> list[list[float]]:
    uniq = sorted({(round(float(p[0]), 5), round(float(p[1]), 5)) for p in points})
    if len(uniq) <= 2:
        return [[float(x), float(y)] for x, y in uniq]

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in uniq:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(uniq):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [[float(x), float(y)] for x, y in lower[:-1] + upper[:-1]]


def order_boundary_loop(edges: list[tuple[int, int]], coords: dict[int, list[float]]) -> list[list[float]]:
    if not edges:
        return []
    adj: dict[int, list[int]] = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    start = min(adj, key=lambda idx: (coords[idx][0], coords[idx][1]))
    loop = [start]
    prev: int | None = None
    cur = start
    for _ in range(max(4, len(edges) + 4)):
        nxts = [n for n in adj.get(cur, []) if n != prev]
        if not nxts:
            break
        if len(nxts) > 1:
            nxts.sort(key=lambda n: (coords[n][0], coords[n][1]))
        nxt = nxts[0]
        if nxt == start:
            break
        loop.append(nxt)
        prev, cur = cur, nxt
    poly = [coords[idx] for idx in loop]
    if polygon_area(poly) < 0:
        poly.reverse()
    return poly


def boundary_edge_components(edges: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    adj: dict[int, list[tuple[int, int]]] = {}
    for edge in edges:
        a, b = edge
        adj.setdefault(a, []).append(edge)
        adj.setdefault(b, []).append(edge)
    visited: set[tuple[int, int]] = set()
    components: list[list[tuple[int, int]]] = []
    for edge in edges:
        key = (min(edge[0], edge[1]), max(edge[0], edge[1]))
        if key in visited:
            continue
        stack = [edge]
        comp: list[tuple[int, int]] = []
        while stack:
            cur = stack.pop()
            cur_key = (min(cur[0], cur[1]), max(cur[0], cur[1]))
            if cur_key in visited:
                continue
            visited.add(cur_key)
            comp.append(cur)
            for v in cur:
                for nxt in adj.get(v, []):
                    nxt_key = (min(nxt[0], nxt[1]), max(nxt[0], nxt[1]))
                    if nxt_key not in visited:
                        stack.append(nxt)
        if comp:
            components.append(comp)
    return components


def candidate_boundary_loops(edges: list[tuple[int, int]], coords: dict[int, list[float]]) -> list[list[list[float]]]:
    loops: list[list[list[float]]] = []
    for comp in boundary_edge_components(edges):
        loop = order_boundary_loop(comp, coords)
        if len(loop) >= 3 and abs(polygon_area(loop)) > 1e-5:
            loops.append(loop)
    loops.sort(key=lambda poly: -abs(polygon_area(poly)))
    return loops


def point_in_polygon_xy(point: list[float], poly: list[list[float]]) -> bool:
    if len(poly) < 3:
        return True
    x = float(point[0])
    y = float(point[1])
    inside = False
    j = len(poly) - 1
    for i, pi in enumerate(poly):
        pj = poly[j]
        xi, yi = float(pi[0]), float(pi[1])
        xj, yj = float(pj[0]), float(pj[1])
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def bbox_touches_floor_polygons(bb: dict[str, Any], polygons: list[dict[str, Any]]) -> bool:
    raw_polys = [poly.get("points", []) for poly in polygons if isinstance(poly, dict)]
    polys = [poly for poly in raw_polys if isinstance(poly, list) and len(poly) >= 3]
    if not polys:
        return True
    mn = bb.get("min", [0.0, 0.0])
    mx = bb.get("max", [0.0, 0.0])
    pts = [
        [float((mn[0] + mx[0]) * 0.5), float((mn[1] + mx[1]) * 0.5)],
        [float(mn[0]), float(mn[1])],
        [float(mn[0]), float(mx[1])],
        [float(mx[0]), float(mn[1])],
        [float(mx[0]), float(mx[1])],
    ]
    return any(point_in_polygon_xy(pt, poly) for pt in pts for poly in polys)


def _floor_polygon_points(primary_room: dict[str, Any] | None) -> list[list[list[float]]]:
    floor_polygons = primary_room.get("floor_polygons", []) if isinstance(primary_room, dict) else []
    out: list[list[list[float]]] = []
    for row in floor_polygons:
        if not isinstance(row, dict):
            continue
        pts = row.get("points")
        if not isinstance(pts, list) or len(pts) < 3:
            continue
        poly: list[list[float]] = []
        for p in pts:
            if isinstance(p, list) and len(p) >= 2:
                try:
                    poly.append([float(p[0]), float(p[1])])
                except Exception:
                    pass
        if len(poly) >= 3 and abs(polygon_area(poly)) > 1e-5:
            out.append(poly)
    return out


def _polygons_bounds(polygons: list[list[list[float]]]) -> tuple[float, float, float, float] | None:
    pts = [p for poly in polygons for p in poly if isinstance(p, list) and len(p) >= 2]
    if not pts:
        return None
    return (
        float(min(p[0] for p in pts)),
        float(min(p[1] for p in pts)),
        float(max(p[0] for p in pts)),
        float(max(p[1] for p in pts)),
    )


def _point_in_any_polygon(x: float, y: float, polygons: list[list[list[float]]]) -> bool:
    return any(point_in_polygon_xy([float(x), float(y)], poly) for poly in polygons)


def _rect_poly(cx: float, cy: float, half: float) -> list[list[float]]:
    x0, y0 = float(cx - half), float(cy - half)
    x1, y1 = float(cx + half), float(cy + half)
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def collect_precise_safe_area(
    primary_room: dict[str, Any] | None,
    floor_z: float,
    *,
    proxy_radius_m: float,
    proxy_height_m: float,
    grid_step_m: float,
    collision_margin_m: float,
) -> dict[str, Any]:
    """Sample BEV-valid character centers with the same env BVH predicate used by place."""
    floor_polys = _floor_polygon_points(primary_room)
    bounds = _polygons_bounds(floor_polys)
    summary: dict[str, Any] = {
        "source": "place_base_env_bvh_cylinder_grid",
        "proxy_radius_m": float(proxy_radius_m),
        "proxy_height_m": float(proxy_height_m),
        "grid_step_m": float(grid_step_m),
        "collision_margin_m": float(collision_margin_m),
        "sample_count": 0,
        "safe_count": 0,
        "blocked_count": 0,
        "rect_half_extent_m": 0.0,
        "status": "not_run",
        "error": "",
    }
    if not floor_polys or bounds is None:
        summary["status"] = "missing_floor_polygon"
        return {"summary": summary, "rects": []}

    try:
        pb = load_place_base()
    except Exception as exc:
        summary["status"] = "place_base_unavailable"
        summary["error"] = str(exc)
        return {"summary": summary, "rects": []}

    step = max(0.05, float(grid_step_m))
    radius = max(0.05, float(proxy_radius_m))
    height = max(0.30, float(proxy_height_m))
    half = step * 0.5
    margin = max(0.0, float(collision_margin_m))
    summary["grid_step_m"] = float(step)
    summary["proxy_radius_m"] = float(radius)
    summary["proxy_height_m"] = float(height)
    summary["rect_half_extent_m"] = float(half)

    depsgraph = bpy.context.evaluated_depsgraph_get()
    try:
        pb.ensure_updated(depsgraph)
    except Exception:
        pass

    env_meshes = [
        obj
        for obj in bpy.context.scene.objects
        if getattr(obj, "type", None) == "MESH" and pb.is_environment_collision_candidate(obj)
    ]
    env_colliders = [pb.EnvCollider(obj, depsgraph) for obj in env_meshes]
    try:
        env_index = pb.EnvColliderIndex2D(env_colliders, cell_size=max(0.50, step * 8.0), large_object_max_cells=400)
    except Exception:
        env_index = None

    proxy_cache = pb._cylinder_cache(radius, 0.0, height, segments=32)
    xmin, ymin, xmax, ymax = bounds
    x0 = math.floor(xmin / step) * step
    y0 = math.floor(ymin / step) * step
    rects: list[list[list[float]]] = []
    sample_count = 0
    blocked_count = 0
    safe_count = 0
    floor_miss_count = 0
    floor_hit_z_min: float | None = None
    floor_hit_z_max: float | None = None

    x = x0
    while x <= xmax + 1e-9:
        y = y0
        while y <= ymax + 1e-9:
            if _point_in_any_polygon(x, y, floor_polys):
                sample_count += 1
                floor_hit, floor_loc, floor_norm, _floor_obj = pb.ray_cast_floor_ignore(
                    bpy.context.scene,
                    depsgraph,
                    Vector((float(x), float(y), float(floor_z) + height + 2.0)),
                    Vector((0.0, 0.0, -1.0)),
                    ignore_set=set(),
                    distance=height + 220.0,
                    max_bounces=40,
                    eps=1e-4,
                )
                if (not floor_hit) or floor_loc is None or floor_norm is None or abs(float(floor_norm.z)) < 0.1:
                    floor_miss_count += 1
                    blocked_count += 1
                    y += step
                    continue
                hit_z = float(floor_loc.z)
                floor_hit_z_min = hit_z if floor_hit_z_min is None else min(float(floor_hit_z_min), hit_z)
                floor_hit_z_max = hit_z if floor_hit_z_max is None else max(float(floor_hit_z_max), hit_z)
                translation = Vector((float(x), float(y), hit_z))
                cand_min = proxy_cache["bounds_min"] + translation
                cand_max = proxy_cache["bounds_max"] + translation
                idxs = env_index.query(cand_min, cand_max) if env_index is not None else range(len(env_colliders))
                obstacles: list[dict[str, Any]] = []
                for idx in idxs:
                    col = env_colliders[idx]
                    if pb.is_walk_collision_ignored_env_object(col.obj):
                        continue
                    obstacles.append({"aabb_min": col.aabb_min, "aabb_max": col.aabb_max, "collider": col})
                collided = False
                if obstacles:
                    cand_bvh = pb.bvh_from_cache(proxy_cache, translation)
                    collided = bool(
                        pb.collision_check_with_obstacles(
                            proxy_cache,
                            translation,
                            cand_bvh,
                            obstacles,
                            margin,
                            depsgraph,
                        )
                    )
                if collided:
                    blocked_count += 1
                else:
                    safe_count += 1
                    rects.append(_rect_poly(float(x), float(y), half))
            y += step
        x += step

    summary.update(
        {
            "sample_count": int(sample_count),
            "safe_count": int(safe_count),
            "blocked_count": int(blocked_count),
            "floor_miss_count": int(floor_miss_count),
            "status": "ok" if safe_count > 0 else "empty",
            "env_collider_count": int(len(env_colliders)),
            "floor_hit_z_min": float(floor_hit_z_min) if floor_hit_z_min is not None else None,
            "floor_hit_z_max": float(floor_hit_z_max) if floor_hit_z_max is not None else None,
        }
    )
    return {"summary": summary, "rects": rects}


def floor_polygons_world(obj: bpy.types.Object) -> list[dict[str, Any]]:
    if obj.type != "MESH":
        return []
    if USE_FAST_BBOX:
        bb = bbox_world_from_bound_box(obj)
        if bb is None:
            return []
        mn = bb["min"]
        mx = bb["max"]
        poly = [
            [float(mn[0]), float(mn[1])],
            [float(mx[0]), float(mn[1])],
            [float(mx[0]), float(mx[1])],
            [float(mn[0]), float(mx[1])],
        ]
        return [{"points": poly, "area": float(abs((mx[0] - mn[0]) * (mx[1] - mn[1]))), "source": obj.name}]
    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = None
    try:
        obj_eval = obj.evaluated_get(depsgraph)
        mesh = obj_eval.to_mesh()
        if mesh is None or not mesh.vertices:
            return []
        coords: dict[int, list[float]] = {}
        all_points: list[list[float]] = []
        for idx, vert in enumerate(mesh.vertices):
            p = obj.matrix_world @ vert.co
            xy = [float(p.x), float(p.y)]
            coords[idx] = xy
            all_points.append(xy)

        edge_counts: dict[tuple[int, int], int] = {}
        for poly in mesh.polygons:
            verts = list(poly.vertices)
            for i, a in enumerate(verts):
                b = verts[(i + 1) % len(verts)]
                key = (min(a, b), max(a, b))
                edge_counts[key] = edge_counts.get(key, 0) + 1
        boundary_edges = [edge for edge, count in edge_counts.items() if count == 1]
        loops = candidate_boundary_loops(boundary_edges, coords)
        boundary = loops[0] if loops else []
        hull = convex_hull(all_points)
        hull_area = abs(polygon_area(hull))
        boundary_area = abs(polygon_area(boundary)) if len(boundary) >= 3 else 0.0
        if len(boundary) < 3 or (hull_area > 0.5 and boundary_area < hull_area * 0.2):
            boundary = hull
        if len(boundary) < 3:
            return []
        area = abs(polygon_area(boundary))
        return [{"points": boundary, "area": float(area), "source": obj.name}]
    except Exception:
        return []
    finally:
        try:
            if obj_eval is not None:
                obj_eval.to_mesh_clear()
        except Exception:
            pass


def mesh_footprint_world(obj: bpy.types.Object) -> list[list[float]]:
    if obj.type != "MESH":
        return []
    if USE_FAST_BBOX:
        bb = bbox_world_from_bound_box(obj)
        if bb is None:
            return []
        mn = bb["min"]
        mx = bb["max"]
        return [
            [float(mn[0]), float(mn[1])],
            [float(mx[0]), float(mn[1])],
            [float(mx[0]), float(mx[1])],
            [float(mn[0]), float(mx[1])],
        ]
    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = None
    try:
        obj_eval = obj.evaluated_get(depsgraph)
        mesh = obj_eval.to_mesh()
        if mesh is None or not mesh.vertices:
            return []
        points: list[list[float]] = []
        for vert in mesh.vertices:
            p = obj.matrix_world @ vert.co
            points.append([float(p.x), float(p.y)])
        hull = convex_hull(points)
        return hull if len(hull) >= 3 else []
    except Exception:
        return []
    finally:
        try:
            if obj_eval is not None:
                obj_eval.to_mesh_clear()
        except Exception:
            pass


def is_annotation_visible(obj: bpy.types.Object) -> bool:
    if obj.hide_viewport or obj.hide_render:
        return False
    try:
        return bool(obj.visible_get())
    except Exception:
        return False


def is_placeholder(name: str) -> bool:
    return ".spawn_placeholder(" in name or ".bbox_placeholder(" in name


def support_type(name: str) -> str:
    if any(tok in name for tok in TABLE_TOKENS):
        return "tabletop"
    if any(tok in name for tok in STORAGE_TOKENS):
        return "cabinet_or_shelf_top"
    return ""


def room_key(name: str) -> str:
    if "/" not in name:
        return name.split(".")[0]
    return name.split("/", 1)[0]


def collect_rooms() -> dict[str, dict[str, Any]]:
    rooms: dict[str, dict[str, Any]] = {}
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if not is_annotation_visible(obj):
            continue
        low = obj.name.lower()
        if not any(tok in low for tok in ("living-room", "bedroom", "bathroom", "kitchen", "dining-room", "hallway")):
            continue
        if not any(tok in low for tok in (".floor", ".wall", ".ceiling", ".exterior", ".meshed")):
            continue
        bb = bbox_world(obj)
        if bb is None:
            continue
        key = room_key(obj.name)
        row = rooms.setdefault(key, {"name": key, "parts": [], "bbox": None, "floor_z": None, "floor_polygons": []})
        row["parts"].append({"name": obj.name, "bbox": bb})
        if ".floor" in low:
            z = float(bb["max"][2])
            if row["floor_z"] is None or z < float(row["floor_z"]):
                row["floor_z"] = z
            row["floor_polygons"].extend(floor_polygons_world(obj))
        old = row.get("bbox")
        if old is None:
            row["bbox"] = bb
        else:
            row["bbox"] = {
                "min": [min(float(old["min"][i]), float(bb["min"][i])) for i in range(3)],
                "max": [max(float(old["max"][i]), float(bb["max"][i])) for i in range(3)],
            }
            mn = row["bbox"]["min"]
            mx = row["bbox"]["max"]
            row["bbox"]["center"] = [(mn[i] + mx[i]) * 0.5 for i in range(3)]
            row["bbox"]["size"] = [mx[i] - mn[i] for i in range(3)]
    return rooms


def bbox_area_xy(bb: dict[str, Any] | None) -> float:
    if not isinstance(bb, dict):
        return 0.0
    size = bb.get("size")
    if not isinstance(size, list) or len(size) < 2:
        return 0.0
    try:
        return abs(float(size[0]) * float(size[1]))
    except Exception:
        return 0.0


def room_kind(name: str) -> str:
    low = str(name or "").lower()
    for kind in ROOM_KIND_PRIORITY:
        if low.startswith(kind) or kind in low:
            return kind
    return "room"


def select_primary_room(rooms: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [row for row in rooms.values() if isinstance(row, dict)]
    if not candidates:
        return None
    for kind in ROOM_KIND_PRIORITY:
        matching = [row for row in candidates if room_kind(str(row.get("name", ""))) == kind]
        if matching:
            matching.sort(key=lambda row: -bbox_area_xy(row.get("bbox")))
            return matching[0]
    candidates.sort(key=lambda row: -bbox_area_xy(row.get("bbox")))
    return candidates[0]


def collect_supports() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if not is_annotation_visible(obj):
            continue
        st = support_type(obj.name)
        if not st:
            continue
        if is_placeholder(obj.name):
            continue
        bb = bbox_world(obj)
        if bb is None:
            continue
        sx, sy = float(bb["size"][0]), float(bb["size"][1])
        if abs(sx * sy) < 0.03:
            continue
        out.append(
            {
                "name": obj.name,
                "surface": st,
                "bbox": bb,
                "area_xy": abs(sx * sy),
            }
        )
    out.sort(key=lambda r: (r["surface"], -float(r["area_xy"]), r["name"]))
    return out


def collect_furniture() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if not is_annotation_visible(obj):
            continue
        if is_placeholder(obj.name):
            continue
        if not any(tok in obj.name for tok in FURNITURE_TOKENS):
            continue
        bb = bbox_world(obj)
        if bb is None:
            continue
        out.append({"name": obj.name, "bbox": bb, "surface": support_type(obj.name)})
    out.sort(key=lambda r: r["name"])
    return out


def obstacle_kind(name: str) -> str:
    if any(tok in name for tok in DOOR_TOKENS):
        return "door"
    if any(tok in name for tok in ("ChairFactory", "ArmchairFactory", "StoolFactory")):
        return "chair"
    if any(tok in name for tok in ("BedFactory", "MattressFactory", "BlanketFactory", "ComforterFactory")):
        return "bed"
    if "SofaFactory" in name:
        return "sofa"
    if any(tok in name for tok in TABLE_TOKENS):
        return "table"
    if any(tok in name for tok in STORAGE_TOKENS) or "DresserFactory" in name or "WardrobeFactory" in name or "NightstandFactory" in name:
        return "storage"
    if "PlantContainerFactory" in name:
        return "plant"
    if "FloorLampFactory" in name:
        return "lamp"
    return "object"


def is_walkable_elevation_name(name: str) -> bool:
    low = str(name or "").lower()
    return any(tok.lower() in low for tok in WALKABLE_ELEVATION_TOKENS)


def is_room_part(name: str) -> bool:
    low = name.lower()
    return any(tok in low for tok in (".floor", ".wall", ".ceiling", ".exterior", ".meshed"))


def is_annotation_collision_ignored_name(name: str) -> bool:
    low = str(name or "").lower()
    if is_walkable_elevation_name(name):
        return True
    return any(tok.lower() in low for tok in NON_COLLISION_DECOR_TOKENS)


def collect_walkable_elevation_surfaces(primary_room: dict[str, Any] | None, floor_z: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    floor_polygons = primary_room.get("floor_polygons", []) if isinstance(primary_room, dict) else []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if not is_annotation_visible(obj):
            continue
        name = obj.name
        if is_placeholder(name) or is_room_part(name):
            continue
        if not is_walkable_elevation_name(name):
            continue
        bb = bbox_world(obj)
        if bb is None:
            continue
        sx, sy, _sz = (float(bb["size"][0]), float(bb["size"][1]), float(bb["size"][2]))
        area = abs(sx * sy)
        if area < 0.035:
            continue
        if float(bb["min"][2]) > float(floor_z) + 0.25:
            continue
        if float(bb["max"][2]) < float(floor_z) - 0.03:
            continue
        if not bbox_touches_floor_polygons(bb, floor_polygons):
            continue
        out.append(
            {
                "name": name,
                "kind": "walkable_elevation",
                "bbox": bb,
                "area_xy": area,
                "top_z": float(bb["max"][2]),
                "height_offset": max(0.0, float(bb["max"][2]) - float(floor_z)),
            }
        )
    out.sort(key=lambda r: (-float(r["area_xy"]), r["name"]))
    return out


def collect_obstacles(primary_room: dict[str, Any] | None, floor_z: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    floor_polygons = primary_room.get("floor_polygons", []) if isinstance(primary_room, dict) else []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if not is_annotation_visible(obj):
            continue
        name = obj.name
        if is_placeholder(name) or is_room_part(name):
            continue
        is_door = any(tok in name for tok in DOOR_TOKENS)
        if (not is_door) and is_annotation_collision_ignored_name(name):
            continue
        bb = bbox_world(obj)
        if bb is None:
            continue
        sx, sy, sz = (float(bb["size"][0]), float(bb["size"][1]), float(bb["size"][2]))
        area = abs(sx * sy)
        if area < 0.035 or sz < 0.12:
            continue
        # A walking character's cylinder reaches well above table/bed height, so
        # BEV must include raised meshes such as comforters, tabletops, and shelves.
        if float(bb["min"][2]) > float(floor_z) + 1.90:
            continue
        if float(bb["max"][2]) < float(floor_z) + 0.12:
            continue
        if not bbox_touches_floor_polygons(bb, floor_polygons):
            continue
        out.append(
            {
                "name": name,
                "kind": obstacle_kind(name),
                "bbox": bb,
                "footprint": mesh_footprint_world(obj),
                "area_xy": area,
            }
        )
    out.sort(key=lambda r: (-float(r["area_xy"]), r["kind"], r["name"]))
    return out


def main() -> int:
    args = parse_args()
    global USE_FAST_BBOX
    USE_FAST_BBOX = bool(args.fast_bbox)
    rooms = collect_rooms()
    living = {k: v for k, v in rooms.items() if k.startswith("living-room")}
    living_floor_z = 0.0
    for row in living.values():
        if row.get("floor_z") is not None:
            living_floor_z = float(row["floor_z"])
            break
    primary_room = select_primary_room(rooms)
    primary_floor_z = living_floor_z
    if primary_room is not None and primary_room.get("floor_z") is not None:
        primary_floor_z = float(primary_room["floor_z"])
    precise_safe = collect_precise_safe_area(
        primary_room,
        primary_floor_z,
        proxy_radius_m=float(args.safe_proxy_radius_m),
        proxy_height_m=float(args.safe_proxy_height_m),
        grid_step_m=float(args.safe_grid_step_m),
        collision_margin_m=float(args.safe_collision_margin_m),
    )

    payload = {
        "schema_version": "4d_world_bev_annotation_data.v1",
        "scene_camera": bpy.context.scene.camera.name if bpy.context.scene.camera else None,
        "rooms": rooms,
        "living_rooms": living,
        "living_floor_z": living_floor_z,
        "primary_room": primary_room or {},
        "primary_room_name": str(primary_room.get("name", "")) if primary_room else "",
        "primary_room_kind": room_kind(str(primary_room.get("name", ""))) if primary_room else "",
        "primary_floor_z": primary_floor_z,
        "supports": collect_supports(),
        "furniture": collect_furniture(),
        "walkable_elevation_surfaces": collect_walkable_elevation_surfaces(primary_room, primary_floor_z),
        "obstacles": collect_obstacles(primary_room, primary_floor_z),
        "precise_safe": precise_safe.get("summary", {}),
        "precise_safe_world_rects": precise_safe.get("rects", []),
    }
    out = Path(args.out_json).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
