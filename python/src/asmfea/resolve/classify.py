"""Connection classification: interfaces + mates + part kinds -> connections.

Rules (PLAN.md, Connection resolver, classification table), applied per part pair:

| Evidence                                          | Connection                  |
|---------------------------------------------------|-----------------------------|
| touching, no mate between the parts               | tie (static joint)          |
| bodies of the same weldment part touching         | tie                         |
| weld bead touching parts                          | bead removed, parts tied directly (D7) |
| lock mate                                         | tie                         |
| concentric + coincident, or hinge                 | revolute                    |
| slot, width or slider mate                        | sliding                     |
| fastener through coaxial holes in >= 2 parts      | bolted (fastener removed, clamped faces tied) |
| distance / angle / limit mate, or to ref geometry | none; exception if touching |
| line or point contact only                        | none; always an exception   |

User overrides always win.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from .. import defaults
from ..diagnostics import Diagnostic, make
from ..model import Model

REVOLUTE_MATES = {"hinge"}
SLIDING_MATES = {"slot", "width", "slider"}
FREE_MATES = {"distance", "angle", "limit", "limitdistance", "limitangle"}


def _pair(a, b):
    return tuple(sorted((a, b)))


def mate_index(model: Model) -> dict[tuple, list]:
    out = defaultdict(list)
    for m in model.mates:
        if len(m.parts) == 2:
            out[_pair(*m.parts)].append(m)
    return out


def joint_from_mates(mates) -> tuple[str | None, str]:
    """(joint type or None, evidence text) for the mates between one part pair."""
    types = {m.type.lower() for m in mates}
    if "lock" in types:
        return "tie", "lock mate"
    if types & REVOLUTE_MATES or {"concentric", "coincident"} <= types:
        return "revolute", "hinge mate" if types & REVOLUTE_MATES else "concentric + coincident mates"
    if types & SLIDING_MATES:
        return "sliding", f"{sorted(types & SLIDING_MATES)[0]} mate"
    if types & FREE_MATES or any(m.geometry == "reference" for m in mates):
        return "none", "distance/angle/limit mate or mate to reference geometry"
    return None, ""


def bolted_joints(model: Model, geoms) -> tuple[list[dict], list[Diagnostic]]:
    """Fasteners through coaxial holes in two or more parts."""
    joints, diags = [], []
    for f in (p for p in model.parts if p.kind == "fastener"):
        T = np.asarray(f.transform).reshape(4, 4)
        axis = T[:3, :3] @ np.asarray(f.fastener.axis, float)
        axis /= np.linalg.norm(axis)
        g = geoms.get(f.id)
        if g is None:
            continue
        centre = np.mean([(np.asarray(g.bbox[:3]) + np.asarray(g.bbox[3:])) / 2], axis=0)
        holes = []
        for pid, pg in geoms.items():
            if pid == f.id or model.part(pid).kind == "fastener":
                continue
            for face in pg.faces:
                if face.surface != "cylinder" or face.convex or face.radius is None:
                    continue
                if abs(abs(float(np.dot(face.axis, axis))) - 1) > 1e-3:
                    continue
                d = np.asarray(face.axis_point) - centre
                if np.linalg.norm(d - np.dot(d, axis) * axis) > defaults.COAX_TOL + 0.05 * f.fastener.d:
                    continue
                if not (0.45 * f.fastener.d <= face.radius <= defaults.HOLE_CLEARANCE_MAX * f.fastener.d / 2):
                    continue
                # the hole must lie along the fastener's length
                t = float(np.dot(d, axis))
                half = 0.5 * float(np.dot(np.asarray(g.bbox[3:]) - np.asarray(g.bbox[:3]), np.abs(axis)))
                if abs(t) > half + 1.0:
                    continue
                holes.append({"part": pid, "face": face.index, "radius": face.radius})
        parts = sorted({h["part"] for h in holes})
        if len(parts) < 2:
            diags.append(make("FASTENER_NO_HOLES", where={"parts": [f.id]}, part=f.id, count=len(parts),
                              evidence={"holes_found": holes}))
            continue
        joints.append({"id": f"B-{f.id}", "type": "bolted", "fastener": f.id, "parts": parts, "holes": holes,
                       "mode": "rigid: clamped faces tied, fastener removed",
                       "source": "geometry",
                       "evidence": {"rule": "fastener through coaxial holes in two or more parts",
                                    "diameter": f.fastener.d}})
    return joints, diags


def classify(model: Model, geoms, interfaces: list[dict], lines: list[dict]) -> tuple[list[dict], list[str], list[Diagnostic]]:
    """Returns (connections, removed part ids, diagnostics)."""
    diags: list[Diagnostic] = []
    kinds = {p.id: p.kind for p in model.parts}
    steps = {p.id: (p.step, tuple(p.transform)) for p in model.parts}
    mates = mate_index(model)

    joints, bdiags = bolted_joints(model, geoms)
    diags += bdiags
    removed = {j["fastener"]: "fastener in a bolted joint (default mode: removed, clamped faces tied)"
               for j in joints}
    for p in model.parts:
        if p.kind == "weld_bead":
            removed[p.id] = "weld bead (D7: removed, the parts it joins are tied directly)"

    conns = []
    by_pair = defaultdict(list)
    for itf in interfaces:
        if any(p in removed for p in itf["parts"]):
            continue
        by_pair[_pair(*itf["parts"])].append(itf)

    clamped = {}
    for j in joints:
        for a in j["parts"]:
            for b in j["parts"]:
                if a < b:
                    clamped[(a, b)] = j["id"]

    groups = {p.id: p.subassembly for p in model.parts}
    for pair, itfs in sorted(by_pair.items()):
        joint, why = joint_from_mates(mates.get(pair, []))
        source = "mates" if joint else "geometry"
        if groups.get(pair[0]) and groups[pair[0]] == groups.get(pair[1]):
            # a sub-assembly is rigid, like one multi-body part: its touching parts are bonded
            # (its own mates are not read, and a flexible sub-assembly is not modelled as such)
            joint, why = "tie", f"parts of the same sub-assembly {groups[pair[0]]} (rigid: touching faces bonded)"
            source = "sub-assembly"
        if pair in clamped:
            # Parts clamped by a fastener: default bolted mode ties the clamped faces,
            # whatever hole-to-hole mates say.
            joint, why = "tie", f"clamped faces of bolted joint {clamped[pair]} (default mode)"
            source = "mates"
        same_weldment = steps[pair[0]] == steps[pair[1]]
        for itf in itfs:
            c = {k: v for k, v in itf.items()}
            if joint is None:
                c["type"] = "tie"
                rule = ("bodies of the same weldment part touching" if same_weldment
                        else "touching faces, no mate between the parts (static joint)")
                if mates.get(pair):
                    rule = "touching faces; mates locate but do not free the parts (static joint)"
            elif joint == "none":
                c["type"] = "none"
                rule = f"touching, but {why}"
                diags.append(make("MATE_CONFLICT", where={"parts": list(pair), "connections": [c["id"]]},
                                  parts=" and ".join(pair), mates=why,
                                  evidence={"gap": itf["gap"], "area": itf["area"]}))
            else:
                c["type"] = joint
                rule = why
            c["source"] = source
            c["evidence"] = {"rule": rule, "gap": itf["gap"], "area": itf["area"],
                             "mates": [m.id for m in mates.get(pair, [])]}
            conns.append(c)

    # Joints from mates where nothing touches (e.g. a pin with clearance) stay unresolved.
    touching = set(by_pair)
    for pair, ms in mates.items():
        joint, why = joint_from_mates(ms)
        if joint in ("revolute", "sliding", "tie") and pair not in touching and not set(pair) & set(removed):
            diags.append(make("JOINT_WITHOUT_CONTACT", where={"parts": list(pair)}, parts=" and ".join(pair),
                              joint=joint, why=why))

    seen_lines: dict = defaultdict(int)
    for ln in lines:
        pair = _pair(*ln["parts"])
        if pair in touching or set(pair) & set(removed):
            continue  # the pair has a real interface; the line contact is incidental
        seen_lines[pair] += 1
        cid = "L-" + "-".join(pair) + ("" if seen_lines[pair] == 1 else f"#{seen_lines[pair]}")
        # the strip along the contact line, so the connection can be bonded (here or by the user)
        geom = _line_strip(ln)
        if geom and in_one_weldment(*pair):
            # bodies of one weldment part meeting along an edge are welded there in practice
            conns.append({"id": cid, "parts": list(pair), "type": "tie", "geometry": "line", "source": "weldment", **geom,
                          "evidence": {"rule": f"edge contact inside one weldment part: bonded along the line "
                                               f"({defaults.LINE_BOND_WIDTH:g} mm strip), as welded", "kind": ln["kind"]}})
            continue
        conns.append({"id": cid, "parts": list(pair), "type": "none", "geometry": "line", "source": "geometry",
                      **(geom or {"sides": []}),
                      "evidence": {"rule": "line or point contact only", "kind": ln["kind"]}})
        diags.append(make("LINE_CONTACT", where={"parts": list(pair), "connections": [cid]},
                          parts=" and ".join(pair), kind=ln["kind"]))
    _weldment_butts(conns, geoms, model.study.bond_tolerance)

    conns += joints
    _apply_overrides(model, conns, diags)
    _preloaded_bolts(conns)
    return conns, sorted(removed), diags


def in_one_weldment(a: str, b: str) -> bool:
    """Two bodies of one multi-body (weldment) part: ids "<component>/body:<name>" with one component."""
    return "/body:" in a and "/body:" in b and a.split("/body:")[0] == b.split("/body:")[0]


def _line_strip(ln: dict) -> dict | None:
    """Tie geometry for a line contact: its two faces, and a strip along the line that limits
    the tied nodes (the deck's ``region``, as for weld beads); None when the line has no extent."""
    seg = ln.get("segment")
    if not seg or ln.get("normal") is None:
        return None
    from .welds import _strip_samples
    w = defaults.LINE_BOND_WIDTH
    length = float(np.linalg.norm(np.asarray(seg[1]) - np.asarray(seg[0])))
    return {"sides": [{"part": ln["parts"][0], "face": ln["faces"][0]}, {"part": ln["parts"][1], "face": ln["faces"][1]}],
            "region": {"segments": [seg], "width": w}, "normal": ln["normal"], "gap": 0.0, "area": w * length,
            "samples": _strip_samples(np.asarray([seg], float), ln["normal"], w)}


def _weldment_butts(conns: list[dict], geoms, tol: float | None) -> None:
    """A member or plate butted against the side of a tube touches only the flat between the
    tube's corner radii: over the radii it leaves a V gap that is welded in practice. A butting
    face only partly covered by its interface, with a corner radius lying against its plane, is
    bonded over its whole area to the flat and the radii (within their radius plus the bond
    tolerance). Weldment bodies first (D29); any bonded joint since D31."""
    tol = defaults.GAP_TOL if tol is None else float(tol)
    for c in conns:
        if c["type"] != "tie" or c.get("geometry") != "plane" or len(c.get("sides", [])) != 2 or c.get("region"):
            continue
        (pa, ia), (pb, ib) = [(x["part"], x["face"]) for x in c["sides"]]
        if pa not in geoms or pb not in geoms:
            continue                       # any bonded joint (D31): a plate edge on a tube's radius as well
        fa, fb = geoms[pa].faces[ia], geoms[pb].faces[ib]
        (bp, bf), (op, of) = ((pa, fa), (pb, fb)) if fa.area <= fb.area else ((pb, fb), (pa, fa))
        if c.get("area", 0.0) >= defaults.BUTT_COVERED_FRACTION * bf.area:
            continue                       # the face is covered: nothing left to bond
        n = np.asarray(bf.normal, float)
        lo, hi = np.asarray(bf.bbox[:3]), np.asarray(bf.bbox[3:])
        masters, reach = [of.index], 0.0
        for f in geoms[op].faces:
            if f.surface != "cylinder" or not f.convex or f.radius is None or abs(float(np.dot(f.axis, n))) > 1e-3:
                continue
            dist = abs(float(np.dot(np.asarray(f.axis_point) - np.asarray(bf.centroid), n)))
            if dist - f.radius > tol or dist > f.radius + tol + 1e-6:
                continue                   # not a radius lying against the butting face's plane
            g = f.radius + tol
            if np.all(np.asarray(f.bbox[:3]) <= hi + g) and np.all(np.asarray(f.bbox[3:]) >= lo - g):
                masters.append(f.index)
                reach = max(reach, f.radius)
        if len(masters) == 1:
            continue
        c["butt"] = {"slave": {"part": bp, "face": bf.index}, "masters": [{"part": op, "face": i} for i in masters],
                     "tolerance": reach + tol}
        if in_one_weldment(pa, pb):
            c["source"] = "weldment"
        c.setdefault("evidence", {})["rule"] = (c.get("evidence", {}).get("rule", "touching faces") +
                                               "; butted against a corner radius: the whole end face bonded, "
                                               "across the gap at the radius (as welded)")


def _preloaded_bolts(conns: list[dict]) -> None:
    """Opt-in bolt mode (PLAN.md, bolted joints): a bolted joint whose override gives a
    preload gets frictional contact on its clamped faces instead of a tie, and a
    pre-tensioned bolt (deck). Bonded stays the default."""
    for j in conns:
        if j["type"] != "bolted" or not j.get("params", {}).get("preload"):
            continue
        mu = float(j["params"].get("friction", defaults.DEFAULT_BOLT_FRICTION))
        j["mode"] = (f"preloaded: {j['params']['preload']:g} N, clamped faces in frictional contact "
                     f"(mu {mu:g}), bolt length locked after preload")
        j["preload"] = float(j["params"]["preload"])
        pair = set(j["parts"])
        for c in conns:
            if c["type"] == "tie" and set(c["parts"][:2]) <= pair and c.get("sides") \
                    and "bolted joint" in c.get("evidence", {}).get("rule", ""):
                c["type"] = "contact"
                c["friction"] = mu
                c["source"] = "bolted (preload)"
                c["evidence"]["rule"] += f"; preloaded bolt {j['id']}: separating contact"


def _apply_overrides(model: Model, conns: list[dict], diags: list[Diagnostic]) -> None:
    for c in conns:
        for ov in model.overrides:
            if set(ov.parts) == set(c["parts"][:2]) and ov.interface in (None, c["id"]):
                c["type"] = ov.connection
                c["source"] = "override"
                c["override"] = ov.id
                if ov.friction is not None:
                    c["friction"] = ov.friction
                if ov.params:
                    c["params"] = dict(ov.params)
                    if ov.friction is not None:
                        c["params"].setdefault("friction", ov.friction)
    # A decided connection clears the exceptions raised for it.
    decided = {c["id"] for c in conns if c.get("source") == "override"}
    diags[:] = [d for d in diags if not (d.where.connections and set(d.where.connections) <= decided)]
