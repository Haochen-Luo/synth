"""Semantic-class grouping for 4DSynth-Nav target disambiguation.

Problem: a scene may contain several factory types that a human (and a VLM)
would call by the SAME word. e.g. "bookshelf" could refer to SimpleBookcase,
CellShelf, or LargeShelf — all visually shelf-like furniture. An instruction
"go to the bookshelf" is then ambiguous if more than one is present.

Solution: map each factory class to a SEMANTIC CLASS — the everyday noun a
VLM would use. The benchmark runner then guarantees uniqueness per semantic
class (deactivating same-semantic-class non-target props), not per factory.

The grouping below is aligned with native_capability_manifest.md's own
category headers. The manifest groups ALL storage furniture (bookcases,
cabinets, desks, shelves, TV stands) under a single `shelves` heading —
Infinigen generates these from the same family of procedural generators, and
the resulting assets form a visual continuum. A KitchenCabinetFactory instance
can look indistinguishable from a SimpleBookcase (open cubbies, same material).
Splitting them into separate semantic classes caused dedup misses (e.g.
case02-L2: KitchenCabinet left visible during a bookshelf task).

NatureShelfTrinketsFactory sits under `elements` in the manifest (it is a
decorative trinket set, NOT shelf furniture), so it is intentionally NOT in
the `shelves` class.
"""

# factory_class -> semantic class (the word an instruction would use).
# Only navigation-relevant furniture/objects need an entry; anything absent
# falls back to its own factory name (treated as its own unique class).
SEMANTIC_CLASS = {
    # ── shelves: ALL storage/shelf furniture (manifest category: `shelves`) ──
    # Merged into one class because Infinigen generates these from the same
    # procedural family — visual appearance is a continuum, not discrete
    # categories. A VLM cannot reliably distinguish "cabinet" from "bookshelf"
    # when both may have open cubbies, the same wood material, etc.
    "SimpleBookcaseFactory": "shelves",
    "CellShelfFactory": "shelves",
    "LargeShelfFactory": "shelves",
    "TriangleShelfFactory": "shelves",
    "WallShelfFactory": "shelves",
    "TVStandFactory": "shelves",
    "CabinetFactory": "shelves",
    "KitchenCabinetFactory": "shelves",
    "SingleCabinetFactory": "shelves",
    "CountertopFactory": "shelves",
    "KitchenIslandFactory": "shelves",
    "SimpleDeskFactory": "shelves",
    "SidetableDeskFactory": "shelves",

    # ── sofa ──
    "SofaFactory": "sofa",

    # ── chair ──
    "ChairFactory": "chair",
    "ArmChairFactory": "chair",
    "BarChairFactory": "chair",
    "OfficeChairFactory": "chair",

    # ── table ──
    "CoffeeTableFactory": "coffee_table",
    "SideTableFactory": "side_table",
    "TableCocktailFactory": "table",
    "TableDiningFactory": "dining_table",

    # ── lamp ──
    "DeskLampFactory": "lamp",
    "FloorLampFactory": "lamp",
    "LampFactory": "lamp",
    "CeilingClassicLampFactory": "ceiling_lamp",

    # ── small portable objects (L3/L4 pickups) ──
    "CupFactory": "cup",
    "BowlFactory": "bowl",
    "PlateFactory": "plate",
    "PotFactory": "pot",
    "PanFactory": "pan",
    "WineglassFactory": "wineglass",
    "VaseFactory": "vase",
    "BookFactory": "book",
    "BookStackFactory": "book",
    "BookColumnFactory": "book",
    "SupportBookStackFactory": "book",

    # ── screen / display ──
    "TVFactory": "tv",
    "MonitorFactory": "tv",

    # ── decor / fixtures ──
    "MirrorFactory": "mirror",
    "WallArtFactory": "wall_art",
    "RugFactory": "rug",

    # ── decorative trinkets — NOT shelf furniture (manifest: `elements`) ──
    # Intentionally their own class so they are never confused with shelves.
    "NatureShelfTrinketsFactory": "trinket_shelf",
}


def semantic_class_of(factory_or_name):
    """Return the semantic class for a factory class name or a prim name.

    Accepts either an exact factory class ("SimpleBookcaseFactory") or a prim
    name that contains one ("Obj_174996_SimpleBookcaseFactory"). Falls back to
    the factory token itself when not in the table — i.e. unmapped factories
    are treated as their own unique semantic class.
    """
    s = str(factory_or_name)
    # Exact match first.
    if s in SEMANTIC_CLASS:
        return SEMANTIC_CLASS[s]
    # Substring match: find a known factory contained in the prim name.
    for factory, sem in SEMANTIC_CLASS.items():
        if factory in s:
            return sem
    # Fallback: the "...Factory" token, else the raw string.
    m = s.split("Factory")
    if len(m) > 1:
        # take the last path-ish segment before "Factory"
        head = m[0].split("_")[-1]
        return (head + "Factory") if head else s
    return s


def same_semantic_class(name_a, name_b):
    """True if two factory/prim names map to the same semantic class."""
    return semantic_class_of(name_a) == semantic_class_of(name_b)
