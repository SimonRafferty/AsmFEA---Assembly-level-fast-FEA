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
    if len(P) < 3:
        return False
    s = np.linalg.svd(P - P.mean(axis=0), compute_uv=False)
    return s[0] > 0 and s[1] > rel * s[0]


def detect_mechanisms(model, geoms, conns: list[dict], face_map: dict, removed: list[str],
                      anchored: set[str]) -> list[Diagnostic]:
    bodies = [p.id for p in model.parts if p.id not in removed and p.id in geoms and p.id in anchored]
    if not bodies:
        return []
    uf = _UF(bodies)
    rows_spec = []  # (kind, data)
    for c in conns:
        a, b = (c["parts"] + [None, None])[:2]
        if a not in uf.p or b not in uf.p:
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
        elif c["type"] == "revolute" and c.get("axis") is not None:
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

    for kind, a, b, pts, extra in rows_spec:
        ka, kb = uf.find(a), uf.find(b)
        if ka == kb:
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
                continue
            for p in geoms[pid].faces[idx].samples:
                for e in dirs:
                    rows.append(point_row(k, p, e))

    if not rows:
        return []
    C = np.asarray(rows)
    _, s, vt = np.linalg.svd(C, full_matrices=True)
    tol = 1e-9 * (s[0] if len(s) else 1.0)
    rank = int(np.sum(s > tol))
    null = vt[rank:]
    if len(null) == 0:
        return []

    motions, moving = [], set()
    for v in null:
        for k in clusters:
            j = 6 * col[k]
            u, th = v[j:j + 3], v[j + 3:j + 6] / L
            mag = np.linalg.norm(u) + np.linalg.norm(th) * L
            if mag < 1e-6:
                continue
            moving.update(members[k])
            if np.linalg.norm(th) * L < 1e-3 * np.linalg.norm(u):
                desc = {"parts": members[k], "motion": "translation",
                        "direction": (u / np.linalg.norm(u)).round(4).tolist()}
            else:
                ax = th / np.linalg.norm(th)
                p = centre[k] + np.cross(th, u) / np.dot(th, th)
                desc = {"parts": members[k], "motion": "rotation", "axis": ax.round(4).tolist(),
                        "through": p.round(3).tolist(),
                        "pitch_mm_per_rad": float(np.dot(th, u) / np.dot(th, th))}
            motions.append(desc)
    text = "; ".join(
        f"{', '.join(m['parts'][:3])}{'...' if len(m['parts']) > 3 else ''} "
        + (f"can translate along {m['direction']}" if m["motion"] == "translation"
           else f"can rotate about axis {m['axis']} through {m['through']}")
        for m in motions[:4])
    return [make("MECHANISM", where={"parts": sorted(moving)}, count=len(null), motions=text,
                 evidence={"free_motions": motions, "constraint_rows": len(rows), "clusters": len(clusters)})]
