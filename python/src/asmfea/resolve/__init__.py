"""Connection resolver and preflight checks.

Steps (PLAN.md): load geometry, find candidate pairs and interfaces, classify, apply
overrides, build the connectivity graph, then preflight: floating parts, loads on them,
near misses, interference, mechanisms. Writes connections.json and exceptions.json.
"""
from __future__ import annotations

from collections import defaultdict

from ..diagnostics import Diagnostic, make
from ..model import Model
from .classify import classify
from .interfaces import find_interferences, find_interfaces, interface_id  # noqa: F401
from .mechanism import detect_mechanisms
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


def resolve(model: Model, geoms: dict, face_map: dict) -> tuple[list[dict], list[str], list[Diagnostic]]:
    """Returns (connections, removed, diags)."""
    interfaces, near, lines = find_interfaces(geoms, model.study.bond_tolerance)
    conns, removed, diags = classify(model, geoms, interfaces, lines, face_map)
    diags += apply_welds(model, conns, removed)
    # Faces just beyond the bond tolerance are not reported (NEAR_MISS removed, the user's choice 2026-10-08):
    # the tolerance is the user's decision on which gaps are joined. A part left with no connection at all is
    # still reported (PART_FLOATING, PART_EXCLUDED_DISCONNECTED).
    # solids overlapping within the bond tolerance were bonded across the overlap on purpose
    overlapped = {tuple(sorted(c["parts"][:2])) for c in conns if c["type"] == "tie" and c.get("gap", 0.0) < 0}
    for itf in find_interferences({k: v for k, v in geoms.items() if k not in removed}):
        if tuple(sorted(itf["parts"])) in overlapped:
            continue
        diags.append(make("INTERFERENCE", where={"parts": itf["parts"]}, parts=" and ".join(itf["parts"]),
                          volume=itf["volume"], evidence=itf))
    anchored, cdiags, excluded = connectivity(model, conns, face_map, removed)
    diags += cdiags
    removed += excluded                           # left out of the mesh, the deck and the results
    diags += contact_only(model, conns, removed, face_map)
    if model.fixtures and not any(d.code == "PART_FLOATING" for d in diags):
        # (with no fixture everything is free: NO_FIXTURES says so, and no part is left out for it)
        diags += free_parts(model, geoms, conns, face_map, removed, anchored)
    return conns, removed, diags


def free_parts(model: Model, geoms: dict, conns: list[dict], face_map: dict, removed: list[str],
               anchored: set[str]) -> list[Diagnostic]:
    """The mechanism check, leaving out parts that are free to move instead of stopping the run.

    A part held only by a hinge (or a sliding joint) with nothing stopping that motion would only move out of
    the way of a load, so it carries nothing: it is left out with a warning naming it, and the rest is solved
    (the user's choice, against SimSolid's "insufficiently constrained" error). Leaving it out can free others
    (parts held only through it), so the check runs again. A motion of a part with a fixture means the
    fixtures themselves are not enough: that still stops the run."""
    fixed = {face_map[f][0] for fx in model.fixtures for f in fx.faces if f in face_map}
    out, freed = [], []
    for _ in range(len(model.parts) + 1):
        mech = detect_mechanisms(model, geoms, conns, face_map, removed, anchored)
        moving = set(mech[0].where.parts) if mech else set()
        if not moving or moving & fixed:
            out += mech
            break
        free = sorted(moving)
        removed += free
        freed += free
        motions = mech[0].evidence.get("free_motions", [])
        loads = sorted({ld.id for ld in model.loads for fid in ld.faces
                        if fid in face_map and face_map[fid][0] in moving})
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
                return detect_mechanisms(model, geoms, conns, face_map, removed, anchored | gone)
    return out
