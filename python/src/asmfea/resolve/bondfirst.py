"""Bond-first connections (D81, the user's design 2026-10-10, after SimSolid).

Everything touching within the bond tolerance (or overlapping) is bonded; the assembly's mates are then applied by
the usual rules (classify), but a joint is kept only where it makes a difference:

- a hinge, revolute, sliding joint or sliding face between two parts that are already held together some other way
  (bonded through other parts, or both fixed to the ground) is bonded: its motion is stopped by the structure
  anyway, and such redundant joints were what made big rigs fragile (a pinned cradle frame's pivot plates were hinged to
  their stiffeners and bonded to the same tubes);
- a hinge on a pinned face is bonded: a pin fixture holds translation only, so a hinge there frees nothing.

The user's own choices (overrides, hinges picked on the Connections tab) are never changed.

On a model with many parts, small parts (a tiny share of the volume) that carry no load or fixture and touch at
most one other part are left out without a message: on a big model they do not change the result (the user:
"fail loudly" is for what makes a difference). They are listed in the report only.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from .. import defaults

# joints whose motion a closed bonded loop stops (couplings, screws and bolts are left as they are)
_RELEASES = ("hinge", "revolute", "sliding", "slide")
_BONDED = ("tie", "bolted")


def choose(model, n_parts: int) -> str:
    """'bond_first' or 'classic' for this model: the study's choice, else bond-first above BOND_FIRST_MIN_PARTS."""
    m = (model.study.connection_method if model.study is not None else None) or "auto"
    if m != "auto":
        return m
    return "bond_first" if n_parts > defaults.BOND_FIRST_MIN_PARTS else "classic"


def _held_parts(model, face_map: dict) -> tuple[set, set]:
    """(parts fixed to the ground, parts carrying a load or a fixture)."""
    ground, busy = set(), set()
    for fx in model.fixtures:
        parts = {face_map[f][0] for f in fx.faces if f in face_map}
        busy |= parts
        if fx.type == "fixed" or (fx.type == "displacement" and set(fx.dofs) >= {1, 2, 3}):
            ground |= parts
    for ld in model.loads:
        busy |= {face_map[f][0] for f in ld.faces if f in face_map}
    return ground, busy


def small_parts(model, geoms: dict, pairs: list[tuple[str, str]], face_map: dict, removed) -> list[str]:
    """Parts left out as negligible on a big model (see the module notes). ``pairs``: every pair of parts that
    touch (interfaces and line contacts).

    A part is in a load path when it touches two or more other parts: it passes load between them, however small
    (a gusset in a corner, a fish plate across a splice, a boss on a pivot). The first rule (only the part that
    alone joins two groups) left out 54 such parts, 139 kg, of a pinned cradle frame (2026-10-10): each sat beside a
    welded joint, so none was the only link, yet all carry load. So only a small part touching at most one other
    part, carrying no load or fixture, is left out: it can carry nothing but its own weight. Leaving it out can
    leave its neighbour touching one part only, so the rule is applied again until nothing changes."""
    live = [p for p in geoms if p not in removed]
    if len(live) <= defaults.BOND_FIRST_MIN_PARTS:
        return []
    total = sum(geoms[p].volume for p in live)
    _, busy = _held_parts(model, face_map)
    small = {p for p in live if geoms[p].volume < defaults.SMALL_PART_VOLUME_FRACTION * total and p not in busy}
    adj = defaultdict(set)
    for a, b in pairs:
        if a in geoms and b in geoms and a not in removed and b not in removed and a != b:
            adj[a].add(b)
            adj[b].add(a)
    out: set = set()
    while True:
        leaves = {p for p in small - out if len(adj[p] - out) <= 1}
        if not leaves:
            break
        out |= leaves
    return sorted(out)


def simplify(model, conns: list[dict], face_map: dict, pin_faces: dict, removed) -> dict:
    """Bond the joints that make no difference (module notes), in place. Returns counts for the log and the
    report: {"redundant": [ids], "on_pins": [ids]}."""
    ground, _ = _held_parts(model, face_map)
    pinned = {face_map[f] for fx in model.fixtures if fx.type == "pinned" for f in fx.faces if f in face_map}
    pinned_parts = {p for p, _ in pinned}
    out = {"redundant": [], "on_pins": []}

    def user(c):
        return c.get("source") in ("override", "user")

    def bond(c):
        if c["type"] == "hinge":
            c["locked"] = True
        else:
            c["type"] = "tie"

    # hinges on a pinned face (or coaxial with a pin on the pinned part): a pin holds translation only
    for c in conns:
        if c["type"] != "hinge" or c.get("locked") or user(c) or set(c["parts"]) & set(removed):
            continue
        on_pin = any((s["part"], s["face"]) in pinned for s in c.get("sides", []))
        if not on_pin and set(c["parts"]) & pinned_parts and c.get("axis") is not None:
            a, p0 = np.asarray(c["axis"], float), np.asarray(c["axis_point"], float)
            for pa in pin_faces.values():
                b = np.asarray(pa["axis"], float)
                d = np.asarray(pa["axis_point"], float) - p0
                if abs(abs(float(np.dot(a, b))) - 1.0) < 1e-6 and np.linalg.norm(d - np.dot(d, a) * a) < defaults.NEAR_COAX_MM:
                    on_pin = True
                    break
        if on_pin:
            bond(c)
            c.setdefault("evidence", {})["rule"] = ("hinge on a pinned face: a pin holds translation only, so the "
                                                    "hinge frees nothing there (bonded, bond-first)")
            out["on_pins"].append(c["id"])

    # a joint between parts already held together another way: bonded. Joints are taken one at a time and the
    # bonded graph grows as they are bonded, so a chain of hinges (a linkage) keeps every hinge whose motion is free
    def bonded_edges():
        e = defaultdict(set)
        for c in conns:
            if len(c["parts"]) < 2 or set(c["parts"]) & set(removed):
                continue
            if c["type"] in _BONDED or (c["type"] == "hinge" and c.get("locked")):
                a, b = c["parts"][:2]
                e[a].add(b)
                e[b].add(a)
        for g in ground:
            e["<ground>"].add(g)
            e[g].add("<ground>")
        return e

    def joined(e, a, b, skip_pair):
        seen, stack = {a}, [a]
        while stack:
            x = stack.pop()
            for nb in e[x]:
                if {x, nb} == skip_pair or nb in seen:
                    continue
                if nb == b:
                    return True
                seen.add(nb)
                stack.append(nb)
        return False

    changed = True
    while changed:
        changed = False
        e = bonded_edges()
        by_pair = defaultdict(list)
        for c in conns:
            if (c["type"] in _RELEASES and not (c["type"] == "hinge" and c.get("locked")) and len(c["parts"]) >= 2
                    and not set(c["parts"]) & set(removed)):
                by_pair[tuple(sorted(c["parts"][:2]))].append(c)
        for (a, b), cs in sorted(by_pair.items()):
            if any(user(c) for c in cs):
                continue
            if joined(e, a, b, {a, b}):
                for c in cs:
                    bond(c)
                    c.setdefault("evidence", {})["rule"] = ("joint between parts already held together through the "
                                                            "structure: it frees nothing, so bonded (bond-first)")
                    out["redundant"].append(c["id"])
                e[a].add(b)                               # bonded now: later joints see it
                e[b].add(a)
                changed = True
    return out


def floating_gaps(geoms: dict, floating: list[str], anchored: set[str], tol: float) -> dict:
    """{floating part: the gap to its nearest anchored part, mm}: the bond tolerance that would join it (the
    floating-parts dialog). Exact distances (OpenCASCADE) to the few nearest by bounding box."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    def box_gap(a, b):
        lo = np.maximum(np.asarray(a[:3]), np.asarray(b[:3]))
        hi = np.minimum(np.asarray(a[3:]), np.asarray(b[3:]))
        return float(np.linalg.norm(np.maximum(lo - hi, 0.0)))

    out = {}
    others = [p for p in anchored if p in geoms]
    for p in floating:
        g = geoms.get(p)
        if g is None or g.solid is None:
            continue
        near = sorted(others, key=lambda q: box_gap(g.bbox, geoms[q].bbox))[:defaults.FLOATING_GAP_CANDIDATES]
        best = None
        for q in near:
            if geoms[q].solid is None:
                continue
            d = BRepExtrema_DistShapeShape(g.solid, geoms[q].solid)
            if d.IsDone():
                v = float(d.Value())
                if best is None or v < best[0]:
                    best = (v, q)
        if best is not None:
            out[p] = {"gap": round(best[0], 3), "nearest": best[1]}
    return out
