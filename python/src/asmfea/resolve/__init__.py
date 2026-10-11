"""Connection resolver and preflight checks.

Steps (PLAN.md): load geometry, find candidate pairs and interfaces, classify, apply
overrides, build the connectivity graph, then preflight: floating parts, loads on them,
near misses, interference, mechanisms. Writes connections.json and exceptions.json.
"""
from __future__ import annotations

from collections import defaultdict

from .. import defaults
from ..diagnostics import Diagnostic, make
from ..model import Model
from . import bondfirst
from .classify import classify
from .interfaces import find_interferences, find_interfaces, interface_id  # noqa: F401
from .mechanism import detect_mechanisms, lock_spin, snap_coaxial
from .welds import apply_welds


def connectivity(model: Model, conns: list[dict], face_map: dict, removed: list[str], skip=()
                 ) -> tuple[set[str], list[Diagnostic], list[str]]:
    """(parts with a path to a fixture, diagnostics for the rest, parts to exclude).

    Parts with no path to a fixture are grouped by what joins them. A group that carries a load (on its faces)
    blocks the run: that load would have nowhere to go. A group with no load is left out of the analysis with
    a warning (the user's choice: a stray or unconnected body should not stop the run), so the rest is solved.
    skip: parts already left out (free to move), through which nothing is carried."""
    skip = set(skip)
    adj = defaultdict(set)
    for c in conns:
        if c["type"] not in ("none",) and len(c["parts"]) >= 2:
            for a in c["parts"]:
                for b in c["parts"]:
                    if a != b and a not in skip and b not in skip:
                        adj[a].add(b)
    roots = {face_map[f][0] for fx in model.fixtures for f in fx.faces if f in face_map}
    seen, stack = set(roots), list(roots)
    while stack:
        for nb in adj[stack.pop()]:
            if nb not in seen:
                seen.add(nb)
                stack.append(nb)
    floating = sorted(p.id for p in model.parts if p.id not in seen and p.id not in removed)
    diags: list[Diagnostic] = []
    excluded: list[str] = []
    if not (floating and roots):
        return seen, diags, excluded
    loaded = {face_map[fid][0] for ld in model.loads for fid in ld.faces if fid in face_map}
    left, stuck = set(floating), []
    while left:                                   # the floating parts, grouped by what joins them
        group, stack = set(), [min(left)]
        while stack:
            p = stack.pop()
            if p in group:
                continue
            group.add(p)
            stack += [nb for nb in adj[p] if nb in left and nb not in group]
        left -= group
        (stuck if group & loaded else excluded).extend(sorted(group))
    if stuck:
        stuck.sort()
        diags.append(make("PART_FLOATING", where={"parts": stuck}, count=len(stuck),
                          parts=", ".join(stuck), evidence={"anchored": sorted(seen)}))
        for ld in model.loads:
            for fid in ld.faces:
                if fid in face_map and face_map[fid][0] in stuck:
                    diags.append(make("LOAD_ON_FLOATING_PART", where={"loads": [ld.id], "parts": [face_map[fid][0]]},
                                      load=ld.id, part=face_map[fid][0]))
    if excluded:
        excluded.sort()
        diags.append(make("PART_EXCLUDED_DISCONNECTED", where={"parts": excluded}, count=len(excluded),
                          parts=", ".join(excluded), evidence={"anchored": sorted(seen)}))
    return seen, diags, excluded


def contact_only(model: Model, conns: list[dict], removed: list[str], face_map: dict) -> list[Diagnostic]:
    """Parts whose every connection is separating contact: no stiffness until it closes.
    A preloaded bolt counts as contact here: it is an axial spring only, so a part held by
    contact and preloaded bolts has no in-plane stiffness until friction sticks."""
    by_part = defaultdict(set)
    for c in conns:
        if c["type"] != "none":
            kind = "contact" if (c["type"] == "bolted" and c.get("preload")) else c["type"]
            for p in c["parts"]:
                by_part[p].add(kind)
    fixed = {face_map[f][0] for fx in model.fixtures for f in fx.faces if f in face_map}
    out = []
    for pid, types in sorted(by_part.items()):
        if pid not in removed and pid not in fixed and types == {"contact"}:
            out.append(make("CONTACT_ONLY_SUPPORT", where={"parts": [pid]}, part=pid))
    return out


def pin_axes_of(model: Model, geoms: dict, face_map: dict) -> dict:
    """{face id: {axis, axis_point, half}} of every cylindrical face of a pinned fixture: a pin, which turns only
    about its own axis (the user's definition, 2026-10-10; the deck holds the other two turns)."""
    out = {}
    for fx in model.fixtures:
        if fx.type != "pinned":
            continue
        for fid in fx.faces:
            if fid not in face_map or face_map[fid][0] not in geoms:
                continue
            f = geoms[face_map[fid][0]].faces[face_map[fid][1]]
            if f.surface == "cylinder" and f.axis is not None and f.axis_point is not None:
                t = f.axial_range or [0.0, 10.0]
                out[fid] = {"axis": list(map(float, f.axis)), "axis_point": list(map(float, f.axis_point)),
                            "half": 0.5 * abs(float(t[1]) - float(t[0]))}
    return out


def resolve(model: Model, geoms: dict, face_map: dict,
            overlap_cache: dict | None = None, out: dict | None = None) -> tuple[list[dict], list[str], list[Diagnostic]]:
    """Returns (connections, removed, diags). overlap_cache: the interference check's results of earlier
    analyses (find_interferences), read and updated. out: filled with {"pin_axes": the pins' axes (aligned
    where near-coaxial), "held_spins": pin faces whose own turn is held too} for connections.json and the deck."""
    interfaces, near, lines = find_interfaces(geoms, model.study.bond_tolerance)
    method = bondfirst.choose(model, len(geoms))
    conns, removed, diags, info = _connect(model, geoms, face_map, interfaces, lines, overlap_cache, method)
    if (method == "classic" and (model.study.connection_method or "auto") == "auto"
            and any(d.code in _MECHANISMS for d in diags)):
        # the classic rules leave a mechanism: bond-first may not (a redundant joint bonded), so it is tried
        b = _connect(model, geoms, face_map, interfaces, lines, overlap_cache, "bond_first")
        if not any(d.code in _MECHANISMS for d in b[2]):
            conns, removed, diags, info = b
            info["why"] = "the classic rules left a mechanism"
    if out is not None:
        out.update(info)
    return conns, removed, diags


def _overlap_interfaces(model: Model, geoms: dict, interfaces: list, overlap_cache, skip: set) -> list:
    """Interfaces of overlapping parts that the bond tolerance did not reach (bond-first): each such pair's faces
    searched again with a tolerance a little deeper than the overlap (the overlap solid's thinnest extent)."""
    from ..geometry import bbox, common
    have = {tuple(sorted(i["parts"][:2])) for i in interfaces}
    out = []
    for itf in find_interferences({k: v for k, v in geoms.items() if k not in skip}, overlap_cache):
        pair = tuple(sorted(itf["parts"]))
        if pair in have:
            continue
        sub = {p: geoms[p] for p in pair}
        res = common(geoms[pair[0]].solid, geoms[pair[1]].solid, defaults.OVERLAP_FUZZY_MM)
        if res is None:
            continue
        b = bbox(res)
        depth = min(b[3] - b[0], b[4] - b[1], b[5] - b[2])
        found, _, _ = find_interfaces(sub, max(model.study.bond_tolerance or defaults.GAP_TOL, 1.5 * depth))
        if not found:
            found = _overlap_box_interface(pair, geoms, b, depth)
        out += [dict(i, evidence_note="overlap bonded (bond-first)") for i in found]
    return out


def _overlap_box_interface(pair, geoms: dict, box: list, depth: float) -> list:
    """An overlap with no opposing faces to tie (the cradle's gusset corners sunk into its tubes' corner radii):
    one face of each part passing through the overlap, tied only where their nodes lie inside the overlap's box
    (grown a little), across its depth. Each side's face is the one whose box shares the most with the overlap's."""
    from .interfaces import interface_id
    grow = max(depth, defaults.GAP_TOL)
    lo = [box[i] - grow for i in range(3)]
    hi = [box[i + 3] + grow for i in range(3)]

    def shared(fb):
        """How much of a face's box lies in the overlap's (a face's box may be flat: an area-like measure)."""
        d = [min(hi[i], fb[i + 3]) - max(lo[i], fb[i]) for i in range(3)]
        if min(d) < 0:
            return 0.0
        return d[0] * d[1] + d[1] * d[2] + d[0] * d[2]

    best = []
    for p in pair:
        faces = [(shared(f.bbox), f) for f in geoms[p].faces if len(f.bbox or []) == 6]
        faces = [t for t in faces if t[0] > 0]
        if not faces:
            return []
        best.append(max(faces, key=lambda t: t[0])[1])
    fa, fb = best
    corners = [[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    return [{"id": interface_id(pair[0], fa.index, pair[1], fb.index), "parts": list(pair),
             "sides": [{"part": pair[0], "face": fa.index}, {"part": pair[1], "face": fb.index}],
             "geometry": "overlap", "gap": -float(depth), "area": float(min(fa.area, fb.area)),
             "normal": None, "samples": corners, "box": lo + hi}]


_MECHANISMS = ("MECHANISM", "MECHANISM_LOADED", "PART_EXCLUDED_MECHANISM")


def _connect(model: Model, geoms: dict, face_map: dict, interfaces: list, lines: list, overlap_cache, method: str):
    """One connection analysis by ``method`` ('classic' or 'bond_first', D81): (connections, removed, diags, info)."""
    exclude = {p: "left out by the user" for p in (model.study.excluded_parts or []) if p in geoms}
    small = []
    if method == "bond_first":
        # what joins parts: touching faces, line contacts, and mates and the user's hinges (a boss on a pin with
        # clearance touches nothing but is joined by its concentric mate: the cradle's pivot bosses, 2026-10-10)
        pairs = [tuple(i["parts"][:2]) for i in interfaces] + [tuple(ln["parts"][:2]) for ln in lines]
        pairs += [tuple(m.parts[:2]) for m in model.mates if len(m.parts) >= 2]
        for h in model.hinges:
            hp = sorted({face_map[f][0] for f in h.faces if f in face_map})
            pairs += [(hp[0], q) for q in hp[1:]]
        small = bondfirst.small_parts(model, geoms, pairs, face_map, set(exclude))
        exclude.update({p: "small part left out (bond-first: under "
                           f"{100 * defaults.SMALL_PART_VOLUME_FRACTION:g} % of the volume, carries nothing)"
                        for p in small})
    if method == "bond_first":
        # overlapping solids are bonded across the overlap, however deep (the user, 2026-10-10): their faces are
        # searched again with a tolerance that reaches through it
        interfaces = interfaces + _overlap_interfaces(model, geoms, interfaces, overlap_cache, set(exclude))
    conns, removed, diags = classify(model, geoms, interfaces, lines, face_map, exclude)
    diags += apply_welds(model, conns, removed)
    # Faces just beyond the bond tolerance are not reported (NEAR_MISS removed, the user's choice 2026-10-08):
    # the tolerance is the user's decision on which gaps are joined. A part left with no connection at all is
    # still reported (PART_FLOATING, PART_EXCLUDED_DISCONNECTED).
    # solids overlapping within the bond tolerance were bonded across the overlap on purpose
    overlapped = {tuple(sorted(c["parts"][:2])) for c in conns if c["type"] == "tie" and c.get("gap", 0.0) < 0}
    if method == "bond_first":                    # overlaps are bonded on purpose: a bonded pair is not a warning
        overlapped |= {tuple(sorted(c["parts"][:2])) for c in conns if c["type"] in ("tie", "bolted")}
    for itf in find_interferences({k: v for k, v in geoms.items() if k not in removed}, overlap_cache):
        if tuple(sorted(itf["parts"])) in overlapped:
            continue
        diags.append(make("INTERFERENCE", where={"parts": itf["parts"]}, parts=" and ".join(itf["parts"]),
                          volume=itf["volume"], evidence=itf))
    pins = pin_axes_of(model, geoms, face_map)
    simplified = {"redundant": [], "on_pins": []}
    if method == "bond_first":
        simplified = bondfirst.simplify(model, conns, face_map, pins, removed)
    anchored, cdiags, excluded = connectivity(model, conns, face_map, removed)
    # each floating part's gap to its nearest anchored part: the tolerance that would join it (the add-in's
    # floating-parts dialog offers it, or leaving the part out)
    for d in cdiags:
        if d.code in ("PART_FLOATING", "PART_EXCLUDED_DISCONNECTED"):
            d.evidence["gaps"] = bondfirst.floating_gaps(geoms, list(d.where.parts), anchored,
                                                         model.study.bond_tolerance or defaults.GAP_TOL)
    diags += cdiags
    removed += excluded                           # left out of the mesh, the deck and the results
    diags += contact_only(model, conns, removed, face_map)
    snap_coaxial(conns, pins)                     # pivots meant to share an axis put exactly on it (item 2)
    held: set = set()
    if model.fixtures and not any(d.code == "PART_FLOATING" for d in diags):
        # (with no fixture everything is free: NO_FIXTURES says so, and no part is left out for it)
        diags += free_parts(model, geoms, conns, face_map, removed, anchored, pins, held)
    vol = {p: geoms[p].volume for p in small}
    dens = {p.id: (model.material(p.material).density if model.material(p.material) else 0.0) for p in model.parts}
    info = {"pin_axes": {fid: {k: v for k, v in pa.items() if k in ("axis", "axis_point", "note")}
                         for fid, pa in pins.items()},
            "held_spins": sorted(held), "method": method,
            # small parts left out (bond-first): in the report only, kept off the Connections tab
            "small_parts": [{"part": p, "volume": vol[p], "mass": vol[p] * dens.get(p, 0.0)} for p in small],
            "bonded_redundant": simplified["redundant"], "bonded_on_pins": simplified["on_pins"]}
    return conns, removed, diags, info


def free_parts(model: Model, geoms: dict, conns: list[dict], face_map: dict, removed: list[str],
               anchored: set[str], pins: dict | None = None, held: set | None = None) -> list[Diagnostic]:
    """The mechanism check, leaving out parts that are free to move instead of stopping the run.

    A part held only by a hinge (or a sliding joint) with nothing stopping that motion would only move out of
    the way of a load, so it carries nothing: it is left out with a warning naming it, and the rest is solved
    (the user's choice, against SimSolid's "insufficiently constrained" error). Leaving it out can free others
    (parts held only through it), so the check runs again. A motion of a part with a fixture means the
    fixtures themselves are not enough: that still stops the run, as does a free part carrying a load (leaving it
    out would lose the load: MECHANISM_LOADED, the user's item 5, 2026-10-10). pins, held: the pins' axes and the
    pin faces whose own turn is held (lock_spin adds to held)."""
    held = set() if held is None else held
    fixed = {face_map[f][0] for fx in model.fixtures for f in fx.faces if f in face_map}
    out, freed = [], []
    for _ in range(len(model.parts) + 1):
        mech = detect_mechanisms(model, geoms, conns, face_map, removed, anchored, pins, held)
        # a group that can only spin about its own pin carries no load in it: lock one hinge (or hold the pin's own
        # turn) and look again
        spun = 0
        while mech and spun < len(conns) + len(pins or {}) and lock_spin(model, geoms, conns, face_map, removed,
                                                                          anchored, pins, held):
            spun += 1
            mech = detect_mechanisms(model, geoms, conns, face_map, removed, anchored, pins, held)
        moving = set(mech[0].where.parts) if mech else set()
        if not moving or moving & fixed:
            out += mech
            break
        free = sorted(moving)
        motions = mech[0].evidence.get("free_motions", [])
        loads = sorted({ld.id for ld in model.loads for fid in ld.faces
                        if fid in face_map and face_map[fid][0] in moving})
        if loads:
            # a free part carrying a load: leaving it out would lose the load, so the run stops (item 5)
            out.append(make("MECHANISM_LOADED", where={"parts": free, "loads": loads}, parts=", ".join(free),
                            loads=", ".join(loads), motions=mech[0].what.split(". ", 1)[-1].rstrip("."),
                            evidence={"free_motions": motions}))
            break
        removed += free
        freed += free
        out.append(make("PART_EXCLUDED_MECHANISM", where={"parts": free, "loads": loads}, count=len(free),
                        parts=", ".join(free), motions=mech[0].what.split(". ", 1)[-1].rstrip("."),
                        loads=", ".join(loads) or "none", evidence={"free_motions": motions}))
        # parts held only through the ones just left out are now floating
        anchored, cdiags, excluded = connectivity(model, conns, face_map, removed, skip=freed)
        out += cdiags
        removed += excluded
        if any(d.code == "PART_FLOATING" for d in cdiags):
            break
    if freed and model.study is not None:
        # a load case whose every load is on the parts left out has nothing left to analyse: that stops the run
        gone = set(freed)
        for case in model.study.load_cases:
            lds = [ld for ld in model.loads if ld.load_case == case]
            if lds and all(ld.faces and all(f in face_map and face_map[f][0] in gone for f in ld.faces) for ld in lds):
                removed[:] = [r for r in removed if r not in gone]
                return detect_mechanisms(model, geoms, conns, face_map, removed, anchored | gone, pins, held)
    return out
