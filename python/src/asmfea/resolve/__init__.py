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


def connectivity(model: Model, conns: list[dict], face_map: dict, removed: list[str]
                 ) -> tuple[set[str], list[Diagnostic], list[str]]:
    """(parts with a path to a fixture, diagnostics for the rest, parts to exclude).

    Parts with no path to a fixture are grouped by what joins them. A group that carries a load (on its faces)
    blocks the run: that load would have nowhere to go. A group with no load is left out of the analysis with
    a warning (the user's choice: a stray or unconnected body should not stop the run), so the rest is solved."""
    adj = defaultdict(set)
    for c in conns:
        if c["type"] not in ("none",) and len(c["parts"]) >= 2:
            for a in c["parts"]:
                for b in c["parts"]:
                    if a != b:
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
    conns, removed, diags = classify(model, geoms, interfaces, lines)
    diags += apply_welds(model, conns, removed)
    touching = {tuple(sorted(c["parts"][:2])) for c in conns}
    for nm in near:
        if tuple(sorted(nm["parts"])) not in touching:
            diags.append(make("NEAR_MISS", where={"parts": nm["parts"]}, parts=" and ".join(nm["parts"]),
                              gap=nm["gap"], evidence={"gap": nm["gap"], "area": nm["area"], "normal": nm["normal"]}))
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
    if not any(d.code == "PART_FLOATING" for d in diags):
        diags += detect_mechanisms(model, geoms, conns, face_map, removed, anchored)
    return conns, removed, diags
