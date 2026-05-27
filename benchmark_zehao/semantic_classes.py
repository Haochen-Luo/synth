"""Semantic-class grouping for 4DSynth-Nav target disambiguation.

Problem: a scene may contain several factory types that a human (and a VLM)
would call by the SAME word. e.g. "bookshelf" could refer to SimpleBookcase,
CellShelf, or LargeShelf — all visually shelf-like furniture. An instruction
"go to the bookshelf" is then ambiguous if more than one is present.

Solution: map each factory class to a SEMANTIC CLASS — the everyday noun a
VLM would use. The benchmark runner then guarantees uniqueness per semantic
class (deactivating same-semantic-class non-target props), not per factory.

The grouping below is derived from native_capability_manifest.md's own category
headers — notably, NatureShelfTrinketsFactory sits under `elements` (it is a
decorative trinket set, NOT shelf furniture), so it is intentionally NOT in the
`bookshelf` class. This keeps disambiguation correct without per-asset manual
inspection: new scenes only need their factories looked up here.
"""

# factory_class -> semantic class (the word an instruction would use).
# Only navigation-relevant furniture/objects need an entry; anything absent
# falls back to its own factory name (treated as its own unique class).
SEMANTIC_CLASS = {
    # ── bookshelf: free-standing shelf furniture ──
    # TVStandFactory included — visually too similar to bookshelf for VLM
    # to reliably distinguish; grouping prevents ambiguous navigation.
    "SimpleBookcaseFactory": "bookshelf",
    "CellShelfFactory": "bookshelf",
    "LargeShelfFactory": "bookshelf",
    "TriangleShelfFactory": "bookshelf",
    "WallShelfFactory": "bookshelf",
    "TVStandFactory": "bookshelf",

    # ── cabinet: closed storage furniture ──
    "CabinetFactory": "cabinet",
    "KitchenCabinetFactory": "cabinet",
    "SingleCabinetFactory": "cabinet",
    "CountertopFactory": "cabinet",
    "KitchenIslandFactory": "cabinet",

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

    # ── desk ──
    "SimpleDeskFactory": "desk",
    "SidetableDeskFactory": "desk",

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

    # ── decor / fixtures ──
    "MirrorFactory": "mirror",
    "WallArtFactory": "wall_art",
    "RugFactory": "rug",
    "TVFactory": "tv",

    # ── decorative trinkets — NOT shelf furniture (manifest: `elements`) ──
    # Intentionally their own class so they are never confused with bookshelf.
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
