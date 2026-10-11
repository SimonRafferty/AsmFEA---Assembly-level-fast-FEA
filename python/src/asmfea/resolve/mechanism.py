"""Preflight mechanism detection: rigid-body kinematics of the connected assembly.

Each part is a rigid body with 6 DOF (u, theta about a reference point c). Connections
and fixtures become linear constraint rows; the null space of the constraint matrix is
the set of motions the model allows without straining anything. A non-empty null space
means the solve would be singular, and each null vector names the parts that move and
how (translation along a direction, or rotation about an axis through a point).

Parts joined by a tie over a non-degenerate area are merged into rigid clusters first
(union-find), so the matrix stays small for large frames.
"""
from __future__ import annotations

import numpy as np

from .. import defaults
from ..diagnostics import Diagnostic, make


class _UF:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def area_like(points, rel=1e-3) -> bool:
    """True when the points span an area (not all on one line)."""
    P = np.asarray(points, float)
    if P.ndim == 2:
        P = P[np.isfinite(P).all(axis=1)]          # invalid points never span anything (an SVD on them fails)
    if len(P) < 3:
        return False
    s = np.linalg.svd(P - P.mean(axis=0), compute_uv=False)
    return s[0] > 0 and s[1] > rel * s[0]


def _perp(a) -> np.ndarray:
    """Two unit vectors square to a and to each other (rows)."""
    a = np.asarray(a, float) / np.linalg.norm(a)
    return np.linalg.svd(a[None])[2][1:]


def _constraints(model, geoms, conns: list[dict], face_map: dict, removed: list[str], anchored: set[str],
                 pin_axes: dict | None = None, held: set | None = None):
    """The rigid-body constraint system of the anchored parts: {C (rows x 6 per cluster), clusters, members, col,
    centre, L, uf}, or None with no parts or no constraint. A cluster's 6 unknowns: the velocity at its centre and
    its rotation x L."""
    bodies = [p.id for p in model.parts if p.id not in removed and p.id in geoms and p.id in anchored]
    if not bodies:
        return None
    uf = _UF(bodies)
    rows_spec = []  # (kind, data)
    couplings = []
    for c in conns:
        if c["type"] in ("gear", "rack_pinion"):
            if all(t["part"] in uf.p and t["carrier"] in uf.p for t in c["couples"]):
                couplings.append(c["couples"])
            continue
        a, b = (c["parts"] + [None, None])[:2]
        if a not in uf.p or b not in uf.p:
            continue
        if c["type"] == "screw" and not c.get("locked"):
            # relative motion: only the helical twist (turn about the axis with an advance of lead per turn)
            rows_spec.append(("helical", a, b, [c["axis_point"]], (np.asarray(c["axis"], float), c["lead"])))
            continue
        if c["type"] == "bolted" and c.get("preload"):
            # a preloaded bolt clamps its parts together
            for other in c["parts"][1:]:
                if other in uf.p:
                    uf.union(c["parts"][0], other)
            continue
        if c["type"] in ("tie", "contact"):
            # contact counts as closed for this check; a part held only by contact gets
            # stabilising springs and its spring load share is checked after the solve
            if area_like(c.get("samples", [])):
                uf.union(a, b)
            else:
                rows_spec.append(("points", a, b, c.get("samples", []), np.eye(3)))
        elif c["type"] == "slide":
            # held only along the face normal (no separation, no penetration); free to slide in its plane
            nrm = c.get("normal")
            if nrm is not None and c.get("samples"):
                rows_spec.append(("points", a, b, c["samples"], np.asarray(nrm, float)[None]))
        elif c["type"] in ("hinge", "screw") and c.get("locked"):
            uf.union(a, b)
        elif c["type"] in ("revolute", "hinge") and c.get("axis") is not None:
            ax = np.asarray(c["axis"], float)
            p0 = np.asarray(c["axis_point"], float)
            half = 0.5 * max(c.get("length", 10.0), 1.0)
            rows_spec.append(("points", a, b, [p0 - half * ax, p0 + half * ax], np.eye(3)))
        elif c["type"] == "revolute":
            rows_spec.append(("points", a, b, c.get("samples", []), np.eye(3)))
        elif c["type"] == "sliding":
            # slide direction: a cylinder's axis, else the mate's direction (never the face normal)
            mate_dirs = [m.direction for m in model.mates
                         if m.id in c.get("evidence", {}).get("mates", []) and m.direction is not None]
            d = c.get("params", {}).get("direction")              # chosen by the user (override)
            if d is None:
                d = c.get("axis") if c.get("geometry") == "cylinder" else (mate_dirs[0] if mate_dirs else None)
            if d is None:
                continue  # the deck reports JOINT_AXIS_UNKNOWN for it
            rows_spec.append(("sliding", a, b, c.get("samples", [])[:1] or [geoms[a].faces[0].centroid],
                              np.asarray(d, float)))
        # none / contact / bolted records: no rigid constraint

    clusters = sorted({uf.find(b) for b in bodies})
    members = {k: [b for b in bodies if uf.find(b) == k] for k in clusters}
    col = {k: i for i, k in enumerate(clusters)}
    centre = {k: np.mean([(np.asarray(geoms[b].bbox[:3]) + np.asarray(geoms[b].bbox[3:])) / 2
                          for b in members[k]], axis=0) for k in clusters}
    lo = np.min([geoms[b].bbox[:3] for b in bodies], axis=0)
    hi = np.max([geoms[b].bbox[3:] for b in bodies], axis=0)
    L = float(np.linalg.norm(hi - lo)) or 1.0
    n = 6 * len(clusters)
    rows = []

    def point_row(k, p, e, sign=1.0):
        r = np.zeros(n)
        j = 6 * col[k]
        r[j:j + 3] = sign * e
        r[j + 3:j + 6] = sign * np.cross(np.asarray(p) - centre[k], e) / L
        return r

    def rot_row(k, w, sign=1.0):
        r = np.zeros(n)
        r[6 * col[k] + 3:6 * col[k] + 6] = sign * np.asarray(w) / L
        return r

    for terms in couplings:
        # sum of scale * (motion of the part relative to its carrier) = 0
        r = np.zeros(n)
        for t in terms:
            kp, kc = uf.find(t["part"]), uf.find(t["carrier"])
            if t["kind"] == "rotation":
                r += t["scale"] * (rot_row(kp, t["axis"]) - rot_row(kc, t["axis"]))
            else:
                e = np.asarray(t["axis"], float)
                r += t["scale"] * (point_row(kp, t["point"], e) - point_row(kc, t["point"], e))
        if np.any(r):
            rows.append(r)

    for kind, a, b, pts, extra in rows_spec:
        ka, kb = uf.find(a), uf.find(b)
        if ka == kb:
            continue
        if kind == "helical":
            ax, lead = extra
            ax = ax / np.linalg.norm(ax)
            perp = np.linalg.svd(ax[None])[2][1:]
            p = pts[0]
            for e in perp:   # no relative turn about, nor shift across, the axis
                rows.append(rot_row(ka, e) - rot_row(kb, e))
                rows.append(point_row(ka, p, e) + point_row(kb, p, e, -1.0))
            # advance along the axis = lead/(2 pi) * turn about it
            rows.append(point_row(ka, p, ax) + point_row(kb, p, ax, -1.0)
                        - lead / (2 * np.pi) * (rot_row(ka, ax) - rot_row(kb, ax)))
            continue
        if kind == "points":
            for p in pts:
                for e in extra:
                    rows.append(point_row(ka, p, e) + point_row(kb, p, e, -1.0))
        else:  # sliding: relative rotation fixed, relative translation fixed normal to d
            d = extra / np.linalg.norm(extra)
            for e in np.eye(3):
                r = np.zeros(n)
                r[6 * col[ka] + 3:6 * col[ka] + 6] = e
                r[6 * col[kb] + 3:6 * col[kb] + 6] = -e
                rows.append(r)
            perp = np.linalg.svd(d[None])[2][1:]
            for e in perp:
                rows.append(point_row(ka, pts[0], e) + point_row(kb, pts[0], e, -1.0))

    for fx in model.fixtures:
        dirs = np.eye(3) if fx.type == "fixed" else np.eye(3)[[d - 1 for d in fx.dofs]]
        for fid in fx.faces:
            if fid not in face_map:
                continue
            pid, idx = face_map[fid]
            if pid not in uf.p:
                continue
            k = uf.find(pid)
            if fx.type == "pinned":
                # held translations at the face centroid only, plus any held rotations
                c = geoms[pid].faces[idx].centroid
                for e in dirs:
                    rows.append(point_row(k, c, e))
                for d in fx.hold_rotations:
                    r = np.zeros(n)
                    r[6 * col[k] + 3 + d - 1] = 1.0
                    rows.append(r)
                # a pin (a cylindrical face): turns only about its own axis, as the deck holds it; that turn is held
                # too when nothing else stopped it (held: lock_spin)
                pa = (pin_axes or {}).get(fid)
                if pa is not None:
                    for e in _perp(pa["axis"]):
                        rows.append(rot_row(k, e))
                    if fid in (held or ()):
                        rows.append(rot_row(k, np.asarray(pa["axis"], float) / np.linalg.norm(pa["axis"])))
                continue
            for p in geoms[pid].faces[idx].samples:
                for e in dirs:
                    rows.append(point_row(k, p, e))

    if not rows:
        return None
    return {"C": np.asarray(rows), "clusters": clusters, "members": members, "col": col, "centre": centre, "L": L,
            "uf": uf}


def detect_mechanisms(model, geoms, conns: list[dict], face_map: dict, removed: list[str],
                      anchored: set[str], pin_axes: dict | None = None, held: set | None = None) -> list[Diagnostic]:
    sys_ = _constraints(model, geoms, conns, face_map, removed, anchored, pin_axes, held)
    if sys_ is None:
        return []
    C, clusters, members, col, centre, L = (sys_[k] for k in ("C", "clusters", "members", "col", "centre", "L"))
    _, s, vt = np.linalg.svd(C, full_matrices=True)
    tol = 1e-9 * (s[0] if len(s) else 1.0)
    rank = int(np.sum(s > tol))
    null = vt[rank:]
    if len(null) == 0:
        return []

    motions, moving = [], set()
    for mode, v in enumerate(null):
        for k in clusters:
            j = 6 * col[k]
            u, th = v[j:j + 3], v[j + 3:j + 6] / L
            mag = np.linalg.norm(u) + np.linalg.norm(th) * L
            if mag < 1e-6:
                continue
            moving.update(members[k])
            if np.linalg.norm(th) * L < 1e-3 * np.linalg.norm(u):
                desc = {"parts": members[k], "mode": mode, "motion": "translation",
                        "direction": (u / np.linalg.norm(u)).round(4).tolist()}
            else:
                ax = th / np.linalg.norm(th)
                p = centre[k] + np.cross(th, u) / np.dot(th, th)
                desc = {"parts": members[k], "mode": mode, "motion": "rotation", "axis": ax.round(4).tolist(),
                        "through": p.round(3).tolist(),
                        "pitch_mm_per_rad": float(np.dot(th, u) / np.dot(th, th))}
            motions.append(desc)
    text = "; ".join(
        f"{', '.join(m['parts'][:3])}{'...' if len(m['parts']) > 3 else ''} "
        + (f"can translate along {m['direction']}" if m["motion"] == "translation"
           else f"can rotate about axis {m['axis']} through {m['through']}")
        for m in motions[:4])
    return [make("MECHANISM", where={"parts": sorted(moving)}, count=len(null), motions=text,
                 evidence={"free_motions": motions, "constraint_rows": len(C), "clusters": len(clusters)})]


def lock_spin(model, geoms, conns: list[dict], face_map: dict, removed: list[str], anchored: set[str],
              pin_axes: dict | None = None, held: set | None = None) -> str | None:
    """One free spin locked, if there is one: a hinge whose group (the parts rigidly joined to one of its sides)
    can turn about the hinge's own axis without breaking any constraint. Every joint of that group then lets it
    turn, so it can carry no torque about the axis through them: locking this hinge removes the spin without
    putting load through it (as a loose pin's, lock_loose_pins). A pivot boss on a pin, and the pin in a pinned
    fixture (free to turn about its own axis), spun freely on the user's cradle and stopped the run as a mechanism
    (2026-10-10). Only a group centred on the axis spins: one hanging off it swings, a real mechanism that may carry
    load as it moves, so it is left to the mechanism check; so is a spin a load turns (a torque on a shaft): nothing
    resists it, and a lock would hide that. Tested hinge by hinge against the constraints: the mechanism check's free motions mix several
    spins in each. Returns the locked hinge's id, or None; run again until None."""
    sys_ = _constraints(model, geoms, conns, face_map, removed, anchored, pin_axes, held)
    if sys_ is None:
        return None
    C, col, centre, L, uf, members = (sys_[k] for k in ("C", "col", "centre", "L", "uf", "members"))

    def on_axis(k, ax, p0) -> bool:
        """Every part of the group centred on the axis (a pin, a boss, a bush): it spins. A part hanging off the axis
        (a bar or a door on a hinge) swings: a real mechanism, left to the check (the hinged-plates case)."""
        for b in members[k]:
            lo, hi = np.asarray(geoms[b].bbox[:3], float), np.asarray(geoms[b].bbox[3:], float)
            off = np.linalg.norm(np.cross((lo + hi) / 2 - p0, ax))
            if off > defaults.SPIN_CENTRE_FRACTION * np.linalg.norm(hi - lo):
                return False
        return True

    def turned(k, ax, p0) -> bool:
        """A load on the group with a moment about the axis (a torque on a shaft in its bearings): the spin is not
        free of load, and nothing in the model resists it; locking a hinge would quietly hold it there."""
        group = set(members[k])
        for ld in getattr(model, "loads", []) or []:
            faces = [face_map[f] for f in ld.faces if f in face_map and face_map[f][0] in group]
            if not faces:
                continue
            mag = float(ld.magnitude or 0.0)
            if ld.type in ("force", "remote_force") and ld.direction is not None:
                force = np.asarray(ld.direction, float) * mag
                area = np.array([geoms[q].faces[i].area for q, i in faces], float)
                cents = np.array([geoms[q].faces[i].centroid for q, i in faces], float)
                at = (area[:, None] * cents).sum(axis=0) / max(float(area.sum()), 1e-30)
                if ld.type == "remote_force":
                    at = (np.asarray(ld.point, float) if ld.point is not None
                          else at + np.asarray(ld.offset or [0.0, 0.0, 0.0], float))
                moment = float(np.dot(np.cross(at - p0, force), ax))
                scale = abs(mag) * L
            elif ld.type in ("pressure", "hydrostatic"):
                moment, scale = 0.0, 0.0
                for q, i in faces:
                    f = geoms[q].faces[i]
                    if f.normal is None:
                        continue                         # a curved face round the axis (a bore) turns nothing
                    push = -np.asarray(f.normal, float) * f.area * (mag or 1.0)
                    moment += float(np.dot(np.cross(np.asarray(f.centroid, float) - p0, push), ax))
                    scale += float(np.linalg.norm(push)) * L
            else:
                continue                                 # gravity: a group centred on its axis has no moment about it
            if abs(moment) > 1e-6 * max(scale, 1e-30):
                return True
        return False
    scale = float(np.linalg.norm(C)) or 1.0

    def spins(k, ax, p0) -> bool:
        """Group k can turn about the line (ax through p0) without breaking any constraint."""
        x = np.zeros(C.shape[1])
        j = 6 * col[k]
        x[j:j + 3] = np.cross(ax, centre[k] - p0)           # the velocity at its centre of a turn about the axis
        x[j + 3:j + 6] = ax * L
        return bool(np.linalg.norm(C @ x) <= 1e-8 * scale * np.linalg.norm(x))

    for c in sorted(conns, key=lambda c: c["id"]):
        if c["type"] != "hinge" or c.get("locked") or c.get("axis") is None:
            continue
        a, b = (c["parts"] + [None, None])[:2]
        if a not in uf.p or b not in uf.p or uf.find(a) == uf.find(b):
            continue
        ax = np.asarray(c["axis"], float)
        ax = ax / np.linalg.norm(ax)
        p0 = np.asarray(c["axis_point"], float)
        for side in (a, b):
            k = uf.find(side)
            if not on_axis(k, ax, p0) or turned(k, ax, p0):
                continue
            if spins(k, ax, p0):
                c["locked"] = True
                c.setdefault("evidence", {})["rule"] = c.get("evidence", {}).get("rule", "") + (
                    f"; {side} and the parts fixed to it could only spin about this axis (every other joint lets "
                    "them turn), so this hinge is locked: no load passes through the spin")
                return c["id"]
    # a pin with nothing else stopping its spin (no hinge to lock: sliding faces only, or nothing): its pinned
    # fixture holds that turn too (the user's item 1, 2026-10-10)
    if held is not None:
        for fid, pa in sorted((pin_axes or {}).items()):
            if fid in held or fid not in face_map or face_map[fid][0] not in uf.p:
                continue
            k = uf.find(face_map[fid][0])
            ax = np.asarray(pa["axis"], float) / np.linalg.norm(pa["axis"])
            p0 = np.asarray(pa["axis_point"], float)
            if on_axis(k, ax, p0) and not turned(k, ax, p0) and spins(k, ax, p0):
                held.add(fid)
                return "pin:" + fid
    return None


def snap_coaxial(conns: list[dict], pin_axes: dict) -> int:
    """Hinges and pins whose axes lie on one line within NEAR_COAX_MM along their whole length are put exactly on it
    (the first one's). Two pivots meant to share an axis but a fraction of a millimetre apart in the CAD otherwise
    hold the turn about it through their misalignment alone: no mechanism found, and huge, meaningless forces in the
    solve (the user's item 2, 2026-10-10). Returns how many were moved; each says so in its evidence."""
    tol = defaults.NEAR_COAX_MM
    lines: list = []                                     # (axis, point, name)
    items = [("hinge", c["id"], c, 0.5 * max(float(c.get("length", 10.0)), 1.0)) for c in conns
             if c["type"] in ("hinge", "revolute") and c.get("axis") is not None and c.get("axis_point") is not None]
    items += [("pin", fid, pa, max(float(pa.get("half", 5.0)), 0.5)) for fid, pa in sorted(pin_axes.items())]
    moved = 0
    for kind, name, rec, half in items:
        ax = np.asarray(rec["axis"], float) / np.linalg.norm(rec["axis"])
        p = np.asarray(rec["axis_point"], float)
        for la, lp, lname in lines:
            ends = [p - half * ax, p + half * ax]
            off = max(float(np.linalg.norm(np.cross(q - lp, la))) for q in ends)
            if abs(float(np.dot(ax, la))) > 0.99 and off <= tol:
                if off > 1e-9:
                    new_ax = la * np.sign(np.dot(ax, la))
                    new_p = lp + float(np.dot(p - lp, la)) * la
                    rec["axis"], rec["axis_point"] = new_ax.tolist(), new_p.tolist()
                    note = (f"axis put on {lname}'s ({off:.3g} mm off it: meant to share it); a turn about that line "
                            "is then found, not held by the misalignment")
                    if kind == "hinge":
                        rec.setdefault("evidence", {})["rule"] = rec.get("evidence", {}).get("rule", "") + "; " + note
                    else:
                        rec["note"] = note
                    moved += 1
                break
        else:
            lines.append((ax, p, f"{kind} {name}"))
    return moved

