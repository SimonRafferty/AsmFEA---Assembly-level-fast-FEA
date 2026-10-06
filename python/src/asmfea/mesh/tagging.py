"""Backend-independent face tagging (Change 001, section 4).

The mesh's boundary facets are classified against the part's CAD faces (OCP, part frame)
purely by geometry: a facet belongs to a face when every one of its nodes lies on the
face's surface within the backend's surface tolerance, and, where several faces share a
surface, when its centre lies inside that face's trimmed boundary. A facet no face claims
is reported, never guessed.
"""
from __future__ import annotations

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.gp import gp_Pnt, gp_Pnt2d
from OCP.ShapeAnalysis import ShapeAnalysis_Surface
from OCP.TopAbs import TopAbs_IN, TopAbs_ON

from ..geometry import FaceInfo
from .elements import FACE_NODES, FACES


def boundary_facets(blocks: list[tuple[str, np.ndarray]]) -> list[tuple[int, int, np.ndarray]]:
    """[(element row, CalculiX face number, full face node ids)] for faces used once."""
    keys, rows, fnums, nodes = [], [], [], []
    row0 = 0
    for etype, conn in blocks:
        for k, (corners, full) in enumerate(zip(FACES[etype], FACE_NODES[etype])):
            c = np.sort(conn[:, corners], axis=1)
            if c.shape[1] == 3:
                c = np.hstack([c, np.full((len(c), 1), -1)])
            keys.append(c)
            rows.append(np.arange(len(conn)) + row0)
            fnums.append(np.full(len(conn), k + 1))
            nodes.extend(conn[:, full])
        row0 += len(conn)
    K = np.concatenate(keys)
    R = np.concatenate(rows)
    F = np.concatenate(fnums)
    _, inv, counts = np.unique(K, axis=0, return_inverse=True, return_counts=True)
    once = np.nonzero(counts[inv.ravel()] == 1)[0]
    return [(int(R[i]), int(F[i]), np.asarray(nodes[i])) for i in once]


def _surface_distance(f: FaceInfo, P: np.ndarray) -> np.ndarray:
    """Distance of points P (n, 3) to the face's underlying surface (analytic where possible)."""
    if f.surface == "plane":
        return np.abs((P - np.asarray(f.centroid)) @ np.asarray(f.normal))
    if f.surface == "cylinder":
        a, c = np.asarray(f.axis), np.asarray(f.axis_point)
        d = P - c
        radial = d - np.outer(d @ a, a)
        return np.abs(np.linalg.norm(radial, axis=1) - f.radius)
    surf = BRep_Tool.Surface_s(f.shape)
    out = np.empty(len(P))
    for i, p in enumerate(P):
        proj = GeomAPI_ProjectPointOnSurf(gp_Pnt(*p), surf)
        out[i] = proj.LowerDistance() if proj.NbPoints() else np.inf
    return out


def _inside(f: FaceInfo, p: np.ndarray, tol: float) -> bool:
    sas = ShapeAnalysis_Surface(BRep_Tool.Surface_s(f.shape))
    uv = sas.ValueOfUV(gp_Pnt(*p), tol)
    st = BRepClass_FaceClassifier(f.shape, gp_Pnt2d(uv.X(), uv.Y()), tol).State()
    return st in (TopAbs_IN, TopAbs_ON)


def tag_faces(X: np.ndarray, blocks: list[tuple[str, np.ndarray]], faces: list[FaceInfo],
              tol: float) -> tuple[np.ndarray, dict]:
    """face_elem rows (face index, element row, face number) and tagging statistics.

    X: node coordinates with row = local node id (row 0 unused).
    """
    facets = boundary_facets(blocks)
    if not facets:
        return np.zeros((0, 3), dtype=np.int64), {"facets": 0, "unclassified": 0}
    centres = np.array([X[n].mean(axis=0) for _, _, n in facets])
    # nodes per facet padded to 8 (quad8); pad with the first node
    width = max(len(n) for _, _, n in facets)
    nodes = np.array([np.pad(n, (0, width - len(n)), mode="edge") for _, _, n in facets])
    claims = [[] for _ in facets]
    for f in faces:
        lo, hi = np.asarray(f.bbox[:3]) - 10 * tol, np.asarray(f.bbox[3:]) + 10 * tol
        cand = np.nonzero(np.all((centres >= lo) & (centres <= hi), axis=1))[0]
        if not len(cand):
            continue
        d = _surface_distance(f, X[nodes[cand]].reshape(-1, 3)).reshape(len(cand), width)
        for i in cand[np.all(d <= tol, axis=1)]:
            claims[i].append(f)
    rows, unclassified, ambiguous = [], 0, 0
    for (row, fnum, _), c, centre in zip(facets, claims, centres):
        if len(c) > 1:
            c = [f for f in c if _inside(f, centre, max(tol, 1e-6))]
        if len(c) == 1:
            rows.append((c[0].index, row, fnum))
        elif not c:
            unclassified += 1
        else:
            ambiguous += 1
    stats = {"facets": len(facets), "unclassified": unclassified, "ambiguous": ambiguous,
             "faces_with_facets": len({r[0] for r in rows}), "faces": len(faces)}
    return np.array(rows, dtype=np.int64).reshape(-1, 3), stats
