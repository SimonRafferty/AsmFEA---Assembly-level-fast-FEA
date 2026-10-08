"""Connection classification: interfaces + mates + part kinds -> connections.

Rules (PLAN.md, Connection resolver, classification table), applied per part pair:

| Evidence                                          | Connection                  |
|---------------------------------------------------|-----------------------------|
| touching, no mate between the parts               | tie (static joint)          |
| bodies of the same weldment part touching         | tie                         |
| weld bead touching parts                          | bead removed, parts tied directly (D7) |
| lock mate                                         | tie                         |
| concentric (or hinge) mate between cylinders     | hinge, one per mate on its own axis; it has |
|   (hole to hole, or a pin surface in a hole)      | priority over the parts' touching faces,    |
|                                                   | which slide without separating (as SimSolid)|
|   a distance mate beside it keeps the parts apart: the hinge holds them axially |
| faces chosen as a hinge by the user (2+ concentric)| hinge from the first part to each other part |
| ... with Lock rotation (mate or user's hinge)     | hinge locked: bonded across the faces       |
| profile center on round profiles, not locked      | hinge about the faces' normal (D51)         |
| screw mate                                        | screw: a hinge whose turn drives an advance |
| gear, rack and pinion mate                        | coupling of the two parts' joints; their   |
|                                                   | touching teeth carry nothing (D51)          |
| a part held only by coaxial hinges (a loose pin)  | its first hinge locked, so it cannot spin   |
| concentric + coincident, or hinge                 | revolute                    |
| slot, width or slider mate                        | sliding                     |
| fastener through coaxial holes in >= 2 parts      | bolted (fastener removed, clamped faces tied) |
| distance / angle mate                             | positions only: touching faces bonded (D55) |
| limit distance / angle mate, or to ref geometry   | none; exception if touching |
| concentric hole mate the parts cannot turn about  | bonded: no hinge (checked on all mates, D55) |
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
COUPLING_MATES = {"gear", "rackpinion"}
COUPLINGS = ("gear", "rack_pinion")
TURNING = ("hinge", "revolute", "screw")   # joints whose free coordinate is a turn (found live: a gear on a screw)
SLIDING_MATES = {"slot", "width", "slider"}
FREE_MATES = {"limit", "limitdistance", "limitangle"}   # and distance / angle mates with limits (free_mate)
POSITIONING_MATES = {"distance", "angle"}                 # without limits they only place a part (D55)


def free_mate(m) -> bool:
    """A mate that lets its parts move: a limit mate, or a distance or angle mate with limits set."""
    t = m.type.lower()
    return t in FREE_MATES or (t in POSITIONING_MATES and m.limits is not None)


def _pair(a, b):
    return tuple(sorted((a, b)))


def mate_index(model: Model, skip=()) -> dict[tuple, list]:
    out = defaultdict(list)
    for m in model.mates:
        if len(m.parts) == 2 and m.id not in skip:
            out[_pair(*m.parts)].append(m)
    return out


def _sw_axis(model: Model, entity: str, fallback) -> np.ndarray:
    """A mate entity's cylinder axis as SolidWorks orients it (the add-in's fingerprint), else the geometry's.
    SolidWorks measures a gear's turn and a screw's hand about each entity's own axis (found live, D51), and
    the STEP geometry's axis may point the other way."""
    fr = model.face_ref(entity)
    a = (fr.fingerprint.axis if fr is not None and fr.fingerprint is not None and fr.fingerprint.axis is not None
         else fallback)
    a = np.asarray(a, float)
    return a / np.linalg.norm(a)


def hinges_from_mates(model: Model, geoms, face_map: dict, clamped: dict, locked_holes=None) -> list[dict]:
    """A concentric mate between cylindrical faces of two parts (hole to hole, or a pin's surface in a hole)
    is a hinge there: its own connection on that axis, joining the two cylindrical faces, free to turn about
    the axis. It is a type of its own, distinct from the revolute joint that concentric + coincident mates
    make of a touching interface. Found live: two plates with a coincident mate and two concentric mates on
    parallel hole pairs came out as ONE revolute on the flat face, about its normal through the face's
    centre, so neither hole was used; the user asked for SimSolid's behaviour, where the hinge has priority
    and the touching faces slide without separating."""
    pins = []
    locked_holes = set() if locked_holes is None else locked_holes   # concentric hole mates found bonded (D55)
    # the faces of the user's hinges, by face (the study's face_ref ids differ from the mates' ids)
    taken = {tuple(face_map[fid]) for h in model.hinges for fid in h.faces if fid in face_map}
    for m in model.mates:
        # a concentric mate names two cylinders; SolidWorks' hinge mate names two cylinders and two planes
        if m.type.lower() not in ("concentric", "hinge", "screw") or len(m.parts) != 2 or len(m.entities) < 2:
            continue
        if _pair(*m.parts) in clamped:
            continue                               # a fastener through the holes: the bolted joint governs
        if any(tuple(face_map.get(e, ())) in taken for e in m.entities):
            continue                               # the user made a hinge of these faces
        sides = []
        for e in m.entities:
            hit = face_map.get(e)
            if hit is None or hit[0] not in geoms:
                continue
            f = geoms[hit[0]].faces[hit[1]]
            if f.surface == "cylinder" and f.axis is not None and f.axis_point is not None:
                sides.append((hit[0], f, e))
        if len(sides) != 2 or sides[0][0] == sides[1][0]:
            continue
        (pa, fa, ea), (pb, fb, eb) = sides
        kinds = " to ".join("pin surface" if f.convex else "hole" for f in (fa, fb))
        if m.type.lower() == "concentric" and not m.lock_rotation and not free_to_turn(model, m, fa, pa, pb):
            # the parts cannot turn about these holes for the assembly's other mates (D55): bonded, no hinge
            locked_holes.add(m.id)
            continue
        if m.type.lower() == "screw":
            if not m.distance_per_rev:
                continue                           # the lead is unknown (SolidWorks 2018): left to the mates
            # Found live (D51): SolidWorks' screw is right-handed when its two cylinders' axes point the same
            # way and Reverse is ticked, or point opposite ways and it is not.
            same = float(np.dot(_sw_axis(model, ea, fa.axis), _sw_axis(model, eb, fb.axis))) > 0
            lead = (1.0 if same == m.reverse else -1.0) * float(m.distance_per_rev)
            c = _hinge(f"S-{m.id}", pa, fa, pb, fb, f"screw mate, {kinds}: turning one part on the other "
                       f"advances it {abs(lead):g} mm per revolution ({'left' if lead < 0 else 'right'}-handed)",
                       "mates", [m.id])
            c["type"], c["lead"] = "screw", lead
        elif m.lock_rotation:
            c = _hinge(f"H-{m.id}", pa, fa, pb, fb, f"concentric mate with rotation locked, {kinds}: bonded "
                       "there (it has priority; the parts' other touching faces slide without separating)",
                       "mates", [m.id])
            c["locked"] = True
        else:
            c = _hinge(f"H-{m.id}", pa, fa, pb, fb, f"concentric mate, {kinds}: a hinge on that axis (it has "
                       "priority; the parts' other touching faces slide without separating)", "mates", [m.id])
        pins.append(c)
    pins += profile_hinges(model, geoms, face_map, clamped, taken)
    return pins


def profile_hinges(model: Model, geoms, face_map: dict, clamped: dict, taken: set) -> list[dict]:
    """A profile center mate on round profiles, without Lock rotation: the parts turn about the profiles'
    common axis, the faces' normal through the round face's centre. Locked, or on other profiles, it only
    places the parts (their touching faces are bonded as usual)."""
    out = []
    for m in model.mates:
        if (m.type.lower() != "profilecenter" or m.lock_rotation or m.profile != "round" or len(m.parts) != 2
                or _pair(*m.parts) in clamped or any(tuple(face_map.get(e, ())) in taken for e in m.entities)):
            continue
        sides = []
        for e in m.entities:
            hit = face_map.get(e)
            if hit and hit[0] in geoms:
                f = geoms[hit[0]].faces[hit[1]]
                if f.surface == "plane" and f.normal is not None:
                    sides.append((hit[0], f))
        if len(sides) != 2 or sides[0][0] == sides[1][0]:
            continue
        (pa, fa), (pb, fb) = sides
        if abs(abs(float(np.dot(fa.normal, fb.normal))) - 1) > 1e-3:
            continue                               # not facing each other: not a profile center to turn on
        small = fa if fa.area <= fb.area else fb      # the round face: its centroid is the profile's centre
        n = np.asarray(fa.normal, float)
        n /= np.linalg.norm(n)
        out.append({"id": f"H-{m.id}", "type": "hinge", "parts": [pa, pb],
                    "sides": [{"part": pa, "face": fa.index}, {"part": pb, "face": fb.index}],
                    "geometry": "plane", "axis": n.tolist(), "axis_point": list(map(float, small.centroid)),
                    "radius": float(np.sqrt(small.area / np.pi)), "length": float(2 * np.sqrt(small.area / np.pi)),
                    "gap": 0.0, "area": float(small.area), "samples": list(small.samples), "source": "mates",
                    "evidence": {"rule": "profile center mate on round profiles, rotation not locked: a hinge about "
                                         "the faces' normal through the profile's centre", "mates": [m.id]}})
    return out


def _joint_of(conns: list[dict], part: str, axis, point, kinds) -> dict | None:
    """The free joint of `part` (a hinge or pin; or a slide when axis is None) on the given axis line."""
    for c in conns:
        if c["type"] not in kinds or part not in c["parts"][:2] or c.get("locked"):
            continue
        if axis is None:
            return c
        if c.get("axis") is None or c.get("axis_point") is None:
            continue
        a = np.asarray(c["axis"], float)
        a /= np.linalg.norm(a)
        if abs(abs(float(np.dot(a, axis))) - 1) > 1e-3:
            continue
        d = np.asarray(point, float) - np.asarray(c["axis_point"], float)
        if np.linalg.norm(d - np.dot(d, a) * a) <= defaults.COAX_TOL + 0.5:
            return c
    return None


def couplings_from_mates(model: Model, geoms, face_map: dict, conns: list[dict], removed) -> list[Diagnostic]:
    """Gear and rack & pinion mates couple the free motions of two joints: each gear turns on its own hinge
    (or pin), the rack slides on its sliding joint. A coupled part whose joint is missing or locked is held,
    so the other part's joint is locked too. The coupling is written as one linear relation
    sum(scale_i * motion_i) = 0, motion_i being the part's turn (rad, about `axis`) or slide (mm, along
    `axis`) relative to the other part of its joint (its carrier)."""
    diags = []
    for m in model.mates:
        kind = m.type.lower()
        if kind not in COUPLING_MATES or len(m.parts) != 2 or set(m.parts) & set(removed):
            continue
        ents = []
        for e in m.entities:
            hit = face_map.get(e)
            if hit and hit[0] in geoms:
                ents.append((hit[0], geoms[hit[0]].faces[hit[1]], e))
        cyl = [(p, f, e) for p, f, e in ents if f.surface == "cylinder" and f.axis is not None and f.axis_point is not None]
        missing = None
        if kind == "gear":
            if len(cyl) != 2 or cyl[0][0] == cyl[1][0]:
                missing = "two cylindrical faces or circular edges, one on each gear"
            elif not m.ratio or min(m.ratio) <= 0:
                missing = "the gear ratio (SolidWorks 2018 does not give it to add-ins)"
        else:
            pin = [(p, f) for p, f, _ in cyl]
            if not pin:
                missing = "the pinion's cylindrical face or circular edge"
            elif not m.distance_per_rev:
                missing = "the rack travel per revolution (SolidWorks 2018 does not give it to add-ins)"
        if missing:
            diags.append(make("MATE_COUPLING_UNUSED", where={"parts": list(m.parts)}, mate=m.id, kind=kind,
                              missing=missing))
            continue
        terms, held = [], []
        if kind == "gear":
            (p1, f1, e1), (p2, f2, e2) = cyl
            # Found live (D51), with the entities in the mate's order: SolidWorks turns them so that
            # |phi1| * den = |phi2| * num, each turn measured about that entity's own axis (as SolidWorks orients
            # it), the two turns of opposite sign unless Reverse is ticked. So den*phi1 + num*phi2 = 0, or
            # den*phi1 - num*phi2 = 0 reversed: external gears on axes pointing the same way turn opposite ways.
            a1 = _sw_axis(model, e1, f1.axis)
            a2 = _sw_axis(model, e2, f2.axis)
            num, den = (float(x) for x in m.ratio)
            spec = [(p1, a1, f1.axis_point, den), (p2, a2, f2.axis_point, (-1.0 if m.reverse else 1.0) * num)]
            for part, ax, pt, scale in spec:
                j = _joint_of(conns, part, ax, pt, TURNING)
                (terms if j else held).append({"part": part, "joint": j, "kind": "rotation", "axis": ax.tolist(),
                                               "point": list(map(float, pt)), "scale": scale})
            rule = (f"gear mate, ratio {num:g}:{den:g} ({p1} turns {num / den:g} times for each turn of {p2})"
                    f"{', reversed' if m.reverse else ''}")
        else:
            pp, pf, _ = cyl[0]
            rack = next(p for p in m.parts if p != pp)
            a = np.asarray(pf.axis, float); a /= np.linalg.norm(a)
            jr = _joint_of(conns, rack, None, None, ("sliding",))
            jp = _joint_of(conns, pp, a, pf.axis_point, TURNING)
            # the rack moves with the pinion's pitch point: s = phi * D/(2 pi) * (a x n).d, n towards the rack
            g = geoms[rack]
            rc = (np.asarray(g.bbox[:3]) + np.asarray(g.bbox[3:])) / 2 - np.asarray(pf.axis_point, float)
            nrm = rc - np.dot(rc, a) * a
            nrm /= (np.linalg.norm(nrm) or 1.0)
            d_rack = np.asarray(jr["params"]["direction"] if jr and jr.get("params", {}).get("direction")
                                else (jr or {}).get("axis") or np.cross(a, nrm), float)
            d_rack /= np.linalg.norm(d_rack)
            # Reverse is not used: found live (D51), SolidWorks sets it from its own sense of the rack's edge, and
            # this geometric rule gave SolidWorks' motion with the rack beside the pinion
            k = float(m.distance_per_rev) / (2 * np.pi) * float(np.dot(np.cross(a, nrm), d_rack))
            for part, j, kd, ax, scale in ((pp, jp, "rotation", a, k), (rack, jr, "slide", d_rack, -1.0)):
                pt = pf.axis_point if kd == "rotation" else list(rc + np.asarray(pf.axis_point, float))
                (terms if j else held).append({"part": part, "joint": j, "kind": kd, "axis": np.asarray(ax).tolist(),
                                               "point": list(map(float, pt)), "scale": scale})
            rule = f"rack and pinion mate, {float(m.distance_per_rev):g} mm of rack travel per revolution"
        # the touching faces of the coupled parts (gear teeth) carry nothing: the coupling stands for the mesh
        pair = _pair(*m.parts)
        for c in conns:
            if _pair(*c["parts"][:2]) == pair and c["type"] in ("tie", "slide", "contact") and c.get("source") != "override":
                c["type"] = "none"
                c["evidence"]["rule"] = f"touching faces of parts coupled by {m.id}: the coupling carries the mesh"
        if len(terms) == 2:
            for t in terms:
                j = t.pop("joint")
                t["connection"], t["carrier"] = j["id"], next(p for p in j["parts"][:2] if p != t["part"])
            conns.append({"id": f"G-{m.id}", "type": "gear" if kind == "gear" else "rack_pinion",
                          "parts": list(m.parts), "couples": terms, "source": "mates",
                          "evidence": {"rule": rule, "mates": [m.id]}})
        elif len(terms) == 1:
            # the other part cannot move: this one is held through the coupling
            j = terms[0]["joint"]
            j["locked"] = True
            j["evidence"]["rule"] += (f"; locked by {m.id}: {held[0]['part']} has no free joint, so the coupling "
                                      "holds this one")
            diags.append(make("MATE_COUPLING_HELD", where={"parts": [held[0]["part"], terms[0]["part"]],
                                                           "connections": [j["id"]]},
                              mate=m.id, held=held[0]["part"], part=terms[0]["part"]))
    return diags


def _hinge(cid: str, pa: str, fa, pb: str, fb, rule: str, source: str, mates: list[str]) -> dict:
    """A hinge connection between two cylindrical faces on one axis, placed midway between them."""
    axis = np.asarray(fa.axis, float)
    axis /= np.linalg.norm(axis)
    p0 = np.asarray(fa.axis_point, float)
    mid = (np.asarray(fa.centroid, float) + np.asarray(fb.centroid, float)) / 2
    at = p0 + axis * float(np.dot(mid - p0, axis))
    spans = [abs(f.axial_range[1] - f.axial_range[0]) for f in (fa, fb) if f.axial_range]
    return {"id": cid, "type": "hinge", "parts": [pa, pb],
            "sides": [{"part": pa, "face": fa.index}, {"part": pb, "face": fb.index}],
            "geometry": "cylinder", "axis": axis.tolist(), "axis_point": at.tolist(),
            "radius": fa.radius, "length": float(sum(spans)) if spans else 10.0,
            "gap": 0.0, "area": float(fa.area + fb.area),
            "samples": list(fa.samples) + list(fb.samples), "source": source,
            "evidence": {"rule": rule, "mates": mates, "radius": fa.radius}}


def concentric_faces(geoms, chosen: list[tuple[str, object]], removed=()) -> list[tuple[str, object]]:
    """The cylindrical faces of every part on the chosen faces' axis, of a radius close to theirs (a pin in its
    hole, with clearance), that continue the chosen faces along the axis without a gap longer than the radius:
    the holes and pin surfaces a pin passes through. Found live: a face can be hidden behind another, so the user
    chooses one and AsmFEA finds the rest."""
    a0 = np.asarray(chosen[0][1].axis, float)
    a0 /= np.linalg.norm(a0)
    p0 = np.asarray(chosen[0][1].axis_point, float)
    radii = [f.radius for _, f in chosen if f.radius]
    r0 = float(np.mean(radii)) if radii else 1.0
    span = lambda f: [float(np.dot(np.asarray(s) - p0, a0)) for s in f.samples] or [0.0]  # noqa: E731
    lo = min(min(span(f)) for _, f in chosen)
    hi = max(max(span(f)) for _, f in chosen)
    gap = max(r0, defaults.HINGE_FIND_GAP)
    have = {(p, f.index) for p, f in chosen}
    cands = []
    for pid, g in geoms.items():
        if pid in removed:
            continue
        for f in g.faces:
            if (pid, f.index) in have or f.surface != "cylinder" or f.axis is None or f.axis_point is None:
                continue
            if abs(abs(float(np.dot(a0, f.axis))) - 1) > 1e-3 or abs((f.radius or 0) - r0) > max(
                    defaults.HINGE_FIND_RADIUS, defaults.HINGE_FIND_RADIUS_REL * r0):
                continue
            d = np.asarray(f.axis_point, float) - p0
            if np.linalg.norm(d - np.dot(d, a0) * a0) > defaults.COAX_TOL + 0.05 * r0:
                continue
            ts = span(f)
            cands.append((min(ts), max(ts), pid, f))
    found, grew = [], True
    while grew:                                     # grow the run along the axis, face by face
        grew = False
        for c in list(cands):
            if c[0] <= hi + gap and c[1] >= lo - gap:
                lo, hi = min(lo, c[0]), max(hi, c[1])
                found.append((c[2], c[3]))
                cands.remove(c)
                grew = True
    return found


def hinges_from_selection(model: Model, geoms, face_map: dict, removed=()) -> tuple[list[dict], list[Diagnostic]]:
    """Hinges the user added by choosing one or more concentric cylindrical faces (holes or pin surfaces).
    The concentric faces of the other parts along the axis are found and added (concentric_faces), so one face
    is enough. Each part's faces join the first part's on that axis; with three or more parts each turns about
    the axis on its own, as on a real pin."""
    out, diags = [], []
    for h in model.hinges:
        faces, bad, named = [], [], []
        for fid in h.faces:
            hit = face_map.get(fid)
            if hit is None or hit[0] not in geoms:
                bad.append(f"face {fid} was not found in the geometry")
                continue
            named.append(hit[0])
            f = geoms[hit[0]].faces[hit[1]]
            if f.surface != "cylinder" or f.axis is None or f.axis_point is None:
                bad.append(f"face {fid} on {hit[0]} is not cylindrical ({f.surface})")
                continue
            faces.append((hit[0], f))
        if faces and not bad:
            a0 = np.asarray(faces[0][1].axis, float)
            a0 /= np.linalg.norm(a0)
            p0 = np.asarray(faces[0][1].axis_point, float)
            for pid, f in faces[1:]:
                d = np.asarray(f.axis_point, float) - p0
                off = float(np.linalg.norm(d - np.dot(d, a0) * a0))
                if abs(abs(float(np.dot(a0, f.axis))) - 1) > 1e-3 or off > defaults.COAX_TOL + 0.05 * (f.radius or 0):
                    bad.append(f"face {f.index} on {pid} is not concentric with the first face "
                               f"(axis {off:.3g} mm away)")
        added = concentric_faces(geoms, faces, removed) if faces and not bad else []
        faces += added
        parts = list(dict.fromkeys(p for p, _ in faces))
        if not bad and len(parts) < 2:
            bad.append("no concentric hole or pin surface of another part was found along the chosen face's axis")
        if bad:
            diags.append(make("HINGE_FACES_INVALID", where={"parts": list(dict.fromkeys(named)), "faces": list(h.faces)},
                              hinge=h.id, problem="; ".join(bad)))
            continue
        hub = parts[0]
        hub_faces = [f for p, f in faces if p == hub]
        for other in parts[1:]:
            mine = [f for p, f in faces if p == other]
            c = _hinge(f"H-{h.id}" + ("" if len(parts) == 2 else f"-{other}"), hub, hub_faces[0], other, mine[0],
                       "hinge chosen by the user on concentric faces" + (
                           f" ({len(added)} more found along the axis)" if added else "") +
                       " (it has priority; the parts' other touching faces slide without separating)", "user", [])
            # every chosen face of the pair takes part in the joint
            c["sides"] = ([{"part": hub, "face": f.index} for f in hub_faces] +
                          [{"part": other, "face": f.index} for f in mine])
            c["samples"] = [s for f in hub_faces + mine for s in f.samples]
            c["area"] = float(sum(f.area for f in hub_faces + mine))
            c["hinge"] = h.id
            if h.lock_rotation:
                c["locked"] = True
                c["evidence"]["rule"] = "hinge chosen by the user with rotation locked: bonded on these faces"
            out.append(c)
    return out, diags


def lock_loose_pins(hinges: list[dict], conns: list[dict]) -> None:
    """A part held only by hinges on one axis (a loose pin through holes) would spin freely about its own
    axis, leaving the model singular. Its first hinge is locked: the pin is held in the first part it
    passes through and turns in the others, which changes nothing that matters."""
    by_part = defaultdict(list)
    for c in conns:
        for p in c["parts"][:2]:
            by_part[p].append(c)
    done = set()
    for pid, cs in sorted(by_part.items()):
        if not cs or any(c["type"] != "hinge" for c in cs if c["type"] != "none"):
            continue
        hs = [c for c in cs if c["type"] == "hinge"]
        if not hs or any(c.get("locked") for c in hs) or any(c["type"] == "screw" for c in cs):
            continue
        if len({q for c in hs for q in c["parts"][:2]} - {pid}) < 2:
            continue                # hinged to one part only: a real swing, left for the mechanism check
        a0 = np.asarray(hs[0]["axis"], float)
        p0 = np.asarray(hs[0]["axis_point"], float)
        coaxial = all(abs(abs(float(np.dot(a0, c["axis"]))) - 1) < 1e-3 and
                      np.linalg.norm(np.cross(np.asarray(c["axis_point"], float) - p0, a0)) < defaults.COAX_TOL + 0.5
                      for c in hs)
        if not coaxial or hs[0]["id"] in done:
            continue
        hs[0]["locked"] = True
        hs[0]["evidence"]["rule"] += (f"; {pid} is held only by hinges on this axis (a loose pin), so this one "
                                      "is locked: the pin is held here and turns in the other parts")
        done.add(hs[0]["id"])


def _mate_rows(model, mate, ix: dict, ncol: int, scale: float) -> list:
    """Constraint rows of one mate on the parts' rigid motions (6 per part: velocity at the origin, then
    rotation x scale), relative motion of its second part against its first. Mates that do not fix a
    relative motion this simple model can describe give none."""
    if len(mate.parts) != 2 or mate.parts[0] not in ix or mate.parts[1] not in ix:
        return []
    i, j = ix[mate.parts[0]], ix[mate.parts[1]]
    fps = [model.face_ref(e).fingerprint for e in mate.entities if model.face_ref(e) is not None
           and model.face_ref(e).fingerprint is not None]
    t = mate.type.lower()

    def row(lin, ang):
        r = np.zeros(ncol)
        lin, ang = np.asarray(lin, float), np.asarray(ang, float) / scale
        r[6 * j:6 * j + 3], r[6 * j + 3:6 * j + 6] = lin, ang
        r[6 * i:6 * i + 3], r[6 * i + 3:6 * i + 6] = -lin, -ang
        n = np.linalg.norm(r)
        return r / n if n > 0 else None

    def perp(n):
        return np.linalg.svd(np.asarray(n, float)[None])[2][1:]

    rows = []
    if t == "lock":
        for e in np.eye(3):
            rows += [row(e, np.zeros(3)), row(np.zeros(3), e)]
    elif t in ("coincident", "distance") and len(fps) == 2 and all(f.surface == "plane" and f.normal for f in fps):
        if t == "distance" and mate.limits is not None:
            return []
        n = np.asarray(fps[0].normal, float)
        p = np.asarray(fps[0].centroid, float)
        rows.append(row(n, np.cross(p, n)))
        rows += [row(np.zeros(3), e) for e in perp(n)]
    elif t == "parallel" and len(fps) == 2 and all(f.normal or f.axis for f in fps):
        rows += [row(np.zeros(3), e) for e in perp(fps[0].normal or fps[0].axis)]
    elif t in ("perpendicular", "angle") and len(fps) == 2 and all(f.normal or f.axis for f in fps):
        if t == "angle" and mate.limits is not None:
            return []
        n1, n2 = (np.asarray(f.normal or f.axis, float) for f in fps)
        k = np.cross(n2, n1)
        if np.linalg.norm(k) > 1e-9:
            rows.append(row(np.zeros(3), k / np.linalg.norm(k)))
    elif t == "concentric" and len(fps) == 2 and all(f.surface == "cylinder" and f.axis for f in fps):
        a = np.asarray(fps[0].axis, float)
        a /= np.linalg.norm(a)
        p = np.asarray(fps[0].centroid, float)
        rows += [row(e, np.cross(p, e)) for e in perp(a)] + [row(np.zeros(3), e) for e in perp(a)]
    return [r for r in rows if r is not None]


def free_to_turn(model, mate, face, pa: str, pb: str) -> bool:
    """Can the two parts of a concentric hole mate turn relative to each other about its axis, for all the
    assembly's mates? Parts are rigid; parts fixed in SolidWorks are held; the other concentric and hinge mates
    (pins) are left out, so two pins between the same parts stay hinges (the user's choice, D55: a part free to
    turn about a hole is hinged there, one held by other mates is bonded)."""
    parts = sorted({p for m in model.mates for p in m.parts} | {pa, pb})
    ix = {p: k for k, p in enumerate(parts)}
    ncol = 6 * len(parts)
    lo = [np.asarray(model.part(p).body.bbox_min) for p in parts if model.part(p) and model.part(p).body]
    hi = [np.asarray(model.part(p).body.bbox_max) for p in parts if model.part(p) and model.part(p).body]
    scale = float(np.linalg.norm(np.max(hi, axis=0) - np.min(lo, axis=0))) if lo else 1000.0
    rows = []
    for m in model.mates:
        t = m.type.lower()
        if m.id != mate.id and t in ("concentric", "hinge", "screw"):
            continue                                 # the other pins
        rows += _mate_rows(model, m, ix, ncol, scale)
    for p in parts:
        if model.part(p) is not None and model.part(p).fixed:
            for k in range(6):
                r = np.zeros(ncol)
                r[6 * ix[p] + k] = 1.0
                rows.append(r)
    a = np.asarray(face.axis, float)
    a /= np.linalg.norm(a)
    s = np.zeros(ncol)
    s[6 * ix[pb] + 3:6 * ix[pb] + 6], s[6 * ix[pa] + 3:6 * ix[pa] + 6] = a, -a
    if not rows:
        return True
    C = np.asarray(rows)
    _, sv, vt = np.linalg.svd(C, full_matrices=True)
    rank = int(np.sum(sv > 1e-9 * sv[0]))
    null = vt[rank:]
    return bool(len(null)) and float(np.linalg.norm(null @ s)) > 1e-6 * np.linalg.norm(s)


def joint_from_mates(mates) -> tuple[str | None, str]:
    """(joint type or None, evidence text) for the mates between one part pair."""
    types = {m.type.lower() for m in mates if not (m.type.lower() in POSITIONING_MATES and m.limits is None)}
    if any(free_mate(m) for m in mates):
        types.add("limit")
    if "lock" in types:
        return "tie", "lock mate"
    if types & REVOLUTE_MATES or {"concentric", "coincident"} <= types:
        return "revolute", "hinge mate" if types & REVOLUTE_MATES else "concentric + coincident mates"
    if types & SLIDING_MATES:
        return "sliding", f"{sorted(types & SLIDING_MATES)[0]} mate"
    if types & FREE_MATES or any(m.geometry == "reference" for m in mates):
        return "none", "limit mate (distance or angle with limits) or mate to reference geometry"
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


def classify(model: Model, geoms, interfaces: list[dict], lines: list[dict],
             face_map: dict | None = None) -> tuple[list[dict], list[str], list[Diagnostic]]:
    """Returns (connections, removed part ids, diagnostics)."""
    diags: list[Diagnostic] = []
    kinds = {p.id: p.kind for p in model.parts}
    steps = {p.id: (p.step, tuple(p.transform)) for p in model.parts}

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
    # hinges from concentric mates on cylinders; those mates then no longer decide the touching faces' type
    chosen, hdiags = hinges_from_selection(model, geoms, face_map or {}, removed)
    diags += hdiags
    locked_holes: set = set()
    pins = [p for p in chosen + hinges_from_mates(model, geoms, face_map or {}, clamped, locked_holes)
            if not set(p["parts"]) & set(removed)]
    mates = mate_index(model, skip={m for p in pins for m in p["evidence"]["mates"]} | locked_holes)
    hinged = {_pair(*p["parts"]) for p in pins}
    # a face that is part of a hinge belongs to it: touching interfaces on it are left to the hinge
    hinge_faces = {(s["part"], s["face"]) for p in pins for s in p["sides"]}
    # beside a hinge, a distance (angle, limit) mate only says where the parts sit: the hinge holds them
    # apart along its axis, so the mate does not disconnect the hinged parts' touching faces
    for pair in hinged:
        if pair in mates:
            mates[pair] = [m for m in mates[pair] if not free_mate(m)]

    # Parts of sub-assemblies are analysed as if they were in the top level: the add-in reads every
    # sub-assembly's mates (D52, the user's choice; sub-assemblies were rigid until then)
    for pair, itfs in sorted(by_pair.items()):
        joint, why = joint_from_mates(mates.get(pair, []))
        source = "mates" if joint else "geometry"
        if pair in clamped:
            # Parts clamped by a fastener: default bolted mode ties the clamped faces,
            # whatever hole-to-hole mates say.
            joint, why = "tie", f"clamped faces of bolted joint {clamped[pair]} (default mode)"
            source = "mates"
        same_weldment = steps[pair[0]] == steps[pair[1]]
        for itf in itfs:
            if any((s["part"], s["face"]) in hinge_faces for s in itf.get("sides", [])):
                continue                    # a pin surface touching its hole: the hinge stands for it
            c = {k: v for k, v in itf.items()}
            if joint is None and pair in hinged:
                # the hinge has priority: the touching faces slide on each other but do not separate
                c["type"] = "slide"
                rule = "touching faces of parts joined by a hinge: they slide but do not separate"
            elif joint is None:
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

    conns += pins
    # Joints from mates where nothing touches (e.g. a pin with clearance) stay unresolved.
    touching = set(by_pair) | {_pair(*p["parts"]) for p in pins}
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
    lock_loose_pins(pins, conns)
    diags += couplings_from_mates(model, geoms, face_map or {}, conns, removed)
    _apply_overrides(model, [c for c in conns if c["type"] in COUPLINGS], diags)
    _preloaded_bolts(conns)
    _hide_incidental_lines(conns, diags)
    return conns, sorted(removed), diags


def _hide_incidental_lines(conns: list[dict], diags: list[Diagnostic]) -> None:
    """Edge contacts between parts that are already joined through the structure (any chain of other
    connections) are incidental: a bend or flange edge resting on a neighbour that the load reaches anyway. They
    are dropped with their LINE_CONTACT warning (the user's choice, 2026-10-08: the water tank listed 985, all
    between parts already bonded through the foam strips and flanges). An edge contact that is the only link
    between two groups of parts is kept: it may be the load path, and the user decides."""
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c in conns:
        if c["type"] != "none":                     # any connection that carries load joins its parts
            ps = c["parts"]
            for q in ps[1:]:
                parent[find(ps[0])] = find(q)
    hidden = set()
    for c in conns:
        if c["type"] == "none" and c.get("geometry") == "line" and len(c["parts"]) >= 2 and c.get("source") != "override":
            if find(c["parts"][0]) == find(c["parts"][1]):
                hidden.add(c["id"])
    if hidden:
        conns[:] = [c for c in conns if c["id"] not in hidden]
        diags[:] = [d for d in diags if not (d.code == "LINE_CONTACT" and set(d.where.connections) <= hidden)]


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
            if c["type"] in COUPLINGS and (ov.interface != c["id"] or ov.connection != "none"):
                continue                    # a gear coupling is only switched off, by name
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
