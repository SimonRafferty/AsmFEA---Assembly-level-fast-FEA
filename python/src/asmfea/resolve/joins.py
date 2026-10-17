"""Joins the user added (D97): the Join button on the Connections tab, for two parts the analysis left unconnected.

A join names two parts and none, one or two faces (study.joins):
  none  the nearest faces of the two parts are bonded, when they are within the join limit (about one element);
  one   that face and the nearest face of the other part, within the limit, even where the parts are already joined
        elsewhere;
  two   one on each part, joined whatever the distance or orientation.
Faces that meet (opposing planes, or a pin in its hole, within the limit and overlapping) are bonded by a tie, as
touching faces are; others are joined by a rigid link: each face's mean motion follows one rigid connector between
them (the deck's double mean motion, spikes/rigid P7), which carries force and moment without stiffening either face.
Either way the connection is a tie (type "tie", a link with ``link``), so Set to can change it as any other.
"""
from __future__ import annotations

import numpy as np

from .. import defaults
from ..diagnostics import Diagnostic, make
from ..model import Model
from .interfaces import _cyl_cyl, _plane_plane, interface_id


def join_limit(model: Model, geoms: dict, a: str, b: str) -> float:
    """The largest gap a join without a face on each part bonds: JOIN_MAX_GAP_ELEMENTS x the smaller of the two
    parts' element sizes, and at least JOIN_MAX_GAP_MIN."""
    from ..mesh import element_size
    from ..planner import plan_study
    try:
        fidelity = plan_study(model.study).fidelity
    except Exception:                                   # noqa: BLE001 - no machine probe: the standard level
        fidelity = "standard"
    user = model.study.mesh.size if model.study is not None else None
    sizes = []
    for p in (a, b):
        g = geoms[p]
        dims = [g.bbox[k + 3] - g.bbox[k] for k in range(3)]
        sizes.append(element_size(dims, user, fidelity))
    # and never less than the study's bond tolerance: what the analysis bonds anyway, a join bonds too
    tol = (model.study.bond_tolerance if model.study is not None and model.study.bond_tolerance is not None else 0.0)
    return max(defaults.JOIN_MAX_GAP_MIN, float(tol), defaults.JOIN_MAX_GAP_ELEMENTS * min(sizes))


def _box_gap(a: list, b: list) -> float:
    d = [max(a[i] - b[i + 3], b[i] - a[i + 3], 0.0) for i in range(3)]
    return float(np.linalg.norm(d))


def face_distance(fa, fb) -> float:
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    d = BRepExtrema_DistShapeShape(fa.shape, fb.shape)
    return float(d.Value()) if d.IsDone() else float("inf")


def nearest_faces(ga, gb, only_a=None, limit: float = 0.0) -> tuple[int, int, float]:
    """(face of a, face of b, distance) of the nearest pair: faces pruned by their boxes, then measured exactly.
    only_a: a's face, given. Faces at the nearest distance often include pairs meeting only at an edge (two
    coplanar faces side by side, the faces round an end): among the pairs as near as the nearest (within the bond
    tolerance), one that can be bonded (opposing faces overlapping, a pin in its hole, within ``limit``) wins, the
    largest overlap first; else the largest pair."""
    fas = [ga.faces[only_a]] if only_a is not None else ga.faces
    cands = sorted(((_box_gap(fa.bbox, fb.bbox), fa, fb) for fa in fas for fb in gb.faces if fa.bbox and fb.bbox),
                   key=lambda t: t[0])
    measured = []
    best_d = float("inf")
    slack = defaults.GAP_TOL
    for lo, fa, fb in cands:
        if lo > best_d + slack:
            break
        d = face_distance(fa, fb)
        measured.append((d, fa, fb))
        best_d = min(best_d, d)
    near = [(d, fa, fb) for d, fa, fb in measured if d <= best_d + slack]
    if not near:
        return None, None, float("inf")
    bonds = [(b["area"], d, fa, fb) for d, fa, fb in near
             if d <= limit and (b := _bond(ga.part, fa, gb.part, fb, limit)) is not None]
    if bonds:
        _, d, fa, fb = max(bonds, key=lambda t: (t[0], -t[1]))
        return fa.index, fb.index, d
    d, fa, fb = min((t for t in near if t[0] <= best_d + 1e-9), key=lambda t: -min(t[1].area, t[2].area))
    return fa.index, fb.index, d


def _bond(pa, fa, pb, fb, limit: float) -> dict | None:
    """The tie between two faces that meet within ``limit`` (opposing planes, or a pin in its hole), or None."""
    found: list = []
    if fa.surface == "plane" and fb.surface == "plane" and fa.normal and fb.normal:
        if float(np.dot(fa.normal, fb.normal)) <= -defaults.JOIN_TIE_COS:
            _plane_plane(pa, fa, pb, fb, limit, limit, found, [])
    elif fa.surface == "cylinder" and fb.surface == "cylinder":
        _cyl_cyl(pa, fa, pb, fb, limit, found, [])
    found = [f for f in found if f["area"] >= defaults.MIN_INTERFACE_AREA and abs(f["gap"]) <= limit + 1e-9]
    return found[0] if found else None


def joins_from_study(model: Model, geoms: dict, face_map: dict, removed) -> tuple[list[dict], list[Diagnostic], list[dict]]:
    """(connections, diagnostics, refused joins [{join, parts, gap, limit}]). Each connection is a tie J-<join id>
    (``source`` "user", ``join``), a link when the faces do not meet."""
    conns, diags, refused = [], [], []
    removed = set(removed)
    made: set = set()                                   # face pairs already joined: a second join on them adds nothing
    for j in model.joins:
        problems = []
        parts = list(j.parts)
        if len(set(parts)) != 2:
            problems.append("it must join two different parts")
        for p in parts:
            if p not in geoms:
                problems.append(f"part {p} is not in the model")
            elif p in removed:
                problems.append(f"part {p} is left out of the analysis")
        faces: dict[str, int] = {}
        for fid in j.faces:
            hit = face_map.get(fid)
            if hit is None:
                problems.append(f"face {fid} was not found in the geometry")
            elif hit[0] not in parts:
                problems.append(f"face {fid} is on {hit[0]}, not on {' or '.join(parts)}")
            elif hit[0] in faces:
                problems.append(f"both faces are on {hit[0]}")
            else:
                faces[hit[0]] = hit[1]
        if problems:
            diags.append(make("JOIN_INVALID", where={"parts": sorted(set(parts)), "faces": list(j.faces)}, join=j.id,
                              problem="; ".join(problems)))
            continue
        a, b = parts
        if b in faces and a not in faces:
            a, b = b, a                                 # the face given is on the first part
        ga, gb = geoms[a], geoms[b]
        limit = join_limit(model, geoms, a, b)
        if a in faces and b in faces:
            ia, ib = faces[a], faces[b]
            dist = face_distance(ga.faces[ia], gb.faces[ib])
        else:
            ia, ib, dist = nearest_faces(ga, gb, faces.get(a), limit)
            if ia is None:
                problems.append("no faces to join")
            elif dist > limit:
                diags.append(make("JOIN_TOO_FAR", where={"parts": sorted([a, b]), "faces": list(j.faces)}, join=j.id,
                                  parts=f"{a} and {b}", gap=f"{dist:.3g}", limit=f"{limit:.3g}",
                                  evidence={"join": j.id, "gap": dist, "limit": limit, "faces": [ia, ib]}))
                refused.append({"join": j.id, "parts": [a, b], "gap": dist, "limit": limit})
                continue
        if problems:
            diags.append(make("JOIN_INVALID", where={"parts": sorted([a, b]), "faces": list(j.faces)}, join=j.id,
                              problem="; ".join(problems)))
            continue
        fa, fb = ga.faces[ia], gb.faces[ib]
        pair = frozenset([(a, ia), (b, ib)])
        if pair in made:
            continue                                    # the same faces as an earlier join (found live: JN4 = JN5)
        made.add(pair)
        sides = [{"part": a, "face": ia}, {"part": b, "face": ib}]
        tie = _bond(a, fa, b, fb, limit) if dist <= limit else None
        base = {"id": f"J-{j.id}", "parts": [a, b], "sides": sides, "type": "tie", "source": "user", "join": j.id,
                "takes_over": interface_id(a, ia, b, ib)}
        if tie is not None:
            c = {**{k: v for k, v in tie.items() if k not in ("id", "parts", "sides")}, **base}
            c["evidence"] = {"rule": f"your join {j.id}: the faces meet (gap {tie['gap']:.3g} mm), bonded",
                             "gap": tie["gap"], "area": tie["area"]}
        else:
            c = {**base, "link": True, "geometry": "link", "gap": dist, "area": float(min(fa.area, fb.area)),
                 "samples": [list(map(float, p)) for p in (fa.samples + fb.samples)],
                 "evidence": {"rule": f"your join {j.id}: the faces are {dist:.3g} mm apart"
                                      + ("" if dist <= limit else f" (more than the {limit:.3g} mm a bond reaches)")
                                      + ", joined by a rigid link", "gap": dist}}
        conns.append(c)
    return conns, diags, refused


def check_join_types(conns: list[dict], diags: list[Diagnostic]) -> None:
    """A rigid link set to a type that needs touching faces (contact, sliding) cannot be made: JOIN_TYPE_UNSUPPORTED."""
    for c in conns:
        if c.get("link") and c["type"] not in ("tie", "none"):
            diags.append(make("JOIN_TYPE_UNSUPPORTED", where={"parts": sorted(c["parts"][:2]), "connections": [c["id"]]},
                              connection=c["id"], parts=" and ".join(c["parts"][:2]), kind=c["type"]))


def fixed_edges_as_links(conns: list[dict]) -> None:
    """An edge contact (two parts touching along a line only) the user set to Fixed joins the two faces that meet there
    by a rigid link, not by the strip along the line: a strip is a hinge about its own line (found live, 2026-10-16:
    a cap on a folded sheet-metal part, bonded along one edge, drooped about it). Edges bonded automatically (a
    weldment's welded seams) keep their strip."""
    for c in conns:
        if c.get("geometry") == "line" and c["type"] == "tie" and c.get("source") == "override" and len(c.get("sides", [])) == 2:
            c["link"] = True
            c.pop("region", None)
            ev = c.setdefault("evidence", {})
            ev["rule"] = (ev.get("rule", "edge contact") + "; set to Fixed by you: the two faces joined by a rigid link "
                          "(bonded along the edge alone they would hinge about it)")
