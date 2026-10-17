"""Rigid parts (D96): parts the user set rigid in the study (study.rigid_parts) are not meshed. Rigid parts bonded to
each other form one rigid group; each group moves as one rigid body, carrying its weight (density x CAD volume) at its
centre of mass, and its connections join the other parts' faces to that motion (the deck's coupling equations).

This module decides the groups and refuses what a rigid part cannot have (separating contact, a preloaded bolt, two
rigid parts sliding on each other), and writes what the deck and post need of the rigid parts' geometry, which they
cannot read from a mesh: each part's mass and centre of mass, a triangulation of every referenced face (for loads and
fixtures on it), the boxes of its connection faces, and a coarse triangulation of the whole part (plots, and the
largest displacement of the part).
"""
from __future__ import annotations

import numpy as np

from .. import defaults
from ..diagnostics import Diagnostic, make
from ..model import Model

JOINT_TYPES = ("hinge", "revolute", "sliding", "screw")


def rigid_set(model: Model, removed) -> set[str]:
    """The rigid parts taking part in the analysis: the study's, less those not in the model or left out."""
    if model.study is None:
        return set()
    known = {p.id for p in model.parts}
    return {p for p in model.study.rigid_parts if p in known and p not in set(removed)}


def rigid_groups(model: Model, conns: list[dict], removed) -> tuple[list[dict], list[Diagnostic]]:
    """([{"id": "RG<k>", "parts": [...]}], diagnostics). Rigid parts joined by a bond (tie, the clamped faces of a
    bolted joint, a weld) form one group. Marks each joint between two parts of one group ``in_group`` (the deck leaves
    it out: it cannot move)."""
    rig = rigid_set(model, removed)
    diags: list[Diagnostic] = []
    if not rig:
        return [], diags
    live = [p.id for p in model.parts if p.id not in set(removed)]
    if live and all(p in rig for p in live):
        diags.append(make("RIGID_NO_FLEXIBLE_PART", where={"parts": sorted(live)}, parts=", ".join(sorted(live))))
    parent = {p: p for p in rig}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c in conns:
        if c["type"] == "tie" and len(c["parts"]) >= 2 and all(p in rig for p in c["parts"][:2]):
            a, b = c["parts"][:2]
            parent[find(a)] = find(b)
    members: dict[str, list[str]] = {}
    for p in sorted(rig):
        members.setdefault(find(p), []).append(p)
    groups = [{"id": f"RG{k}", "parts": ps} for k, ps in enumerate(sorted(members.values()), start=1)]
    group_of = {p: g["id"] for g in groups for p in g["parts"]}
    for c in conns:
        ps = [p for p in c.get("parts", [])[:2]]
        rigid_sides = [p for p in ps if p in rig]
        if not rigid_sides or c["type"] == "none":
            continue
        kind = None
        if c["type"] == "contact":
            kind = "separating contact"
        elif c["type"] == "bolted" and c.get("preload"):
            kind = "a preloaded bolt"
        elif c["type"] == "slide" and len(rigid_sides) == 2:
            kind = "sliding, no separation, between two rigid parts"
        elif c["type"] in ("gear", "rack_pinion"):
            continue                       # couples two joints, carried by them
        if kind is not None:
            pr = c.get("parts", [])
            diags.append(make("RIGID_CONNECTION_UNSUPPORTED", where={"parts": sorted(set(pr)), "connections": [c["id"]]},
                              connection=c["id"], parts=" and ".join(pr[:2]), kind=kind))
            continue
        if c["type"] in JOINT_TYPES and len(rigid_sides) == 2 and group_of[ps[0]] == group_of[ps[1]]:
            c["in_group"] = group_of[ps[0]]
            diags.append(make("RIGID_JOINT_IN_GROUP", where={"parts": sorted(ps), "connections": [c["id"]]},
                              connection=c["id"], parts=" and ".join(ps)))
    return groups, diags


# --------------------------------------------------------------------------- geometry for the deck and post

def _triangles(shape, deflection: float) -> tuple[np.ndarray, np.ndarray]:
    """(vertices (n, 3), triangles (m, 3)) of a face or solid, every face's triangles turned so their normals point
    out of the solid (a reversed face's triangles flipped)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    BRepMesh_IncrementalMesh(shape, max(deflection, 1e-4), False, 0.5, True)
    verts, tris = [], []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current()) if hasattr(TopoDS, "Face_s") else TopoDS.Face(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None and tri.NbTriangles() > 0:
            T = loc.Transformation()
            base = len(verts)
            for k in range(1, tri.NbNodes() + 1):
                p = tri.Node(k).Transformed(T)
                verts.append((p.X(), p.Y(), p.Z()))
            flip = face.Orientation() == TopAbs_REVERSED
            for k in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(k).Get()
                tris.append((base + a - 1, base + c - 1, base + b - 1) if flip else (base + a - 1, base + b - 1, base + c - 1))
        exp.Next()
    return np.asarray(verts, float).reshape(-1, 3), np.asarray(tris, np.int64).reshape(-1, 3)


def _hull(V: np.ndarray) -> np.ndarray:
    """The corners of the points' convex hull (all the points when they are too few or flat for one)."""
    try:
        from scipy.spatial import ConvexHull
        return V[ConvexHull(V).vertices]
    except Exception:                                   # noqa: BLE001 - degenerate: every point
        return V


def face_samples(face_info, deflection: float) -> dict:
    """A face as triangles for the deck: each one's centroid, area and outward unit normal, and the triangles'
    corners (points). The areas are scaled so they sum to the face's exact area (a force spread by area, or a
    pressure, then has its exact total on a flat face)."""
    V, T = _triangles(face_info.shape, deflection)
    if len(T) == 0:
        return {"centroids": [list(map(float, face_info.centroid))], "areas": [float(face_info.area)],
                "normals": [list(map(float, face_info.normal or (0.0, 0.0, 0.0)))],
                "points": [list(map(float, p)) for p in face_info.samples] or [list(map(float, face_info.centroid))]}
    P = V[T]
    cr = 0.5 * np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0])
    a = np.linalg.norm(cr, axis=1)
    keep = a > 1e-14
    P, cr, a = P[keep], cr[keep], a[keep]
    n = cr / a[:, None]
    scale = float(face_info.area) / float(a.sum()) if a.sum() > 0 else 1.0
    # the corners as well: a fixture holds the face at its points, and a square face's two triangle centres lie on a
    # line (it could turn about it)
    corners = np.unique(np.round(V[np.unique(T[keep])], 6), axis=0)
    return {"centroids": np.round(P.mean(axis=1), 6).tolist(), "areas": np.round(a * scale, 9).tolist(),
            "normals": np.round(n, 9).tolist(), "points": corners.tolist()}


def rigid_doc(model: Model, geoms: dict, face_map: dict, groups: list[dict], conns: list[dict]) -> dict:
    """rigid.json: the groups (parts, mass, centre of mass: the reference point of the group's rigid motion), each
    rigid part's mass, volume, centre of mass and coarse triangulation, the triangles of every referenced face on a
    rigid part (loads and fixtures), and the boxes of the faces its connections use."""
    from ..geometry import volume_props
    parts, tess = {}, {}
    group_of = {p: g["id"] for g in groups for p in g["parts"]}
    for pid in sorted(group_of):
        g = geoms.get(pid)
        if g is None:
            continue
        vol, cog = volume_props(g.solid)
        mat = model.material(model.part(pid).material)
        rho = mat.density if mat is not None else 0.0
        parts[pid] = {"group": group_of[pid], "volume": float(vol), "mass": float(vol * rho), "cog": list(map(float, cog))}
        diag = float(np.linalg.norm(np.asarray(g.bbox[3:]) - np.asarray(g.bbox[:3])))
        V, T = _triangles(g.solid, defaults.RIGID_TESSELLATION_REL * diag)
        tess[pid] = {"vertices": np.round(V, 5).tolist(), "triangles": T.tolist()}
        # a rigid motion moves a point of the part furthest at a corner of its convex hull: post's largest displacement
        parts[pid]["hull"] = np.round(_hull(V if len(V) else np.asarray([g.bbox[:3], g.bbox[3:]], float)), 5).tolist()
    out_groups = []
    for grp in groups:
        ms = [parts[p] for p in grp["parts"] if p in parts]
        m = sum(p["mass"] for p in ms)
        w = [p["mass"] for p in ms] if m > 0 else [p["volume"] for p in ms]
        cog = (np.average([p["cog"] for p in ms], axis=0, weights=w) if ms and sum(w) > 0
               else np.zeros(3))
        out_groups.append({"id": grp["id"], "parts": grp["parts"], "mass": float(m), "cog": cog.tolist(),
                           "ref": cog.tolist()})
    faces = {}
    for fid, (pid, idx) in sorted(face_map.items()):
        if pid in parts:
            g = geoms[pid]
            diag = float(np.linalg.norm(np.asarray(g.bbox[3:]) - np.asarray(g.bbox[:3])))
            faces[fid] = {"part": pid, "face": int(idx), **face_samples(g.faces[idx], defaults.RIGID_FACE_TESSELLATION_REL * diag)}
    boxes = {}
    for c in conns:
        for s in c.get("sides", []):
            if s["part"] in parts:
                f = geoms[s["part"]].faces[int(s["face"])]
                boxes[f"{s['part']}#{int(s['face'])}"] = list(map(float, f.bbox))
        for s in (c.get("butt") or {}).get("masters", []) + ([c["butt"]["slave"]] if c.get("butt") else []):
            if s["part"] in parts:
                boxes[f"{s['part']}#{int(s['face'])}"] = list(map(float, geoms[s["part"]].faces[int(s["face"])].bbox))
    return {"schema_version": "1.0", "groups": out_groups, "parts": parts, "faces": faces, "face_boxes": boxes,
            "tessellation": tess}
