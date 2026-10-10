"""STEP geometry through OpenCascade (OCP): find each part's solid, fingerprint its faces,
match face_refs, and provide the booleans the resolver needs.

OCP is Apache-2.0 over OCCT (LGPL-2.1 + exception), linked dynamically; see THIRD_PARTY.md.
Gmsh is never imported (Change 001).

Face identity: a part's faces are numbered by their order in an indexed face map of the
matched solid (TopExp.MapShapes). The same code produces the same numbering wherever it
runs, and meshes are tagged against these faces by geometry (mesh/tagging.py), never by
any mesher's own entity numbers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform, BRepBuilderAPI_MakeVertex, BRepBuilderAPI_Transform
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.GeomAbs import (GeomAbs_BezierSurface, GeomAbs_BSplineSurface, GeomAbs_Cone,
                         GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus)
from OCP.gp import gp_GTrsf, gp_Mat, gp_Pnt, gp_Trsf, gp_Vec, gp_XYZ
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as _IndexedShapeMap

from . import defaults
from .diagnostics import Diagnostic, make
from .model import FaceRef, Fingerprint, Model, Part

SURFACE_TYPES = {GeomAbs_Plane: "plane", GeomAbs_Cylinder: "cylinder", GeomAbs_Cone: "cone",
                 GeomAbs_Sphere: "sphere", GeomAbs_Torus: "torus", GeomAbs_BSplineSurface: "bspline",
                 GeomAbs_BezierSurface: "bspline"}


@dataclass
class FaceInfo:
    index: int
    surface: str
    area: float
    centroid: list[float]
    normal: Optional[list[float]] = None      # outward, planes only
    axis: Optional[list[float]] = None        # cylinders / cones
    radius: Optional[float] = None            # cylinders
    axis_point: Optional[list[float]] = None  # cylinders / cones: a point on the axis
    convex: Optional[bool] = None             # cylinders: True = pin/shaft, False = hole
    axial_range: Optional[list[float]] = None  # cylinders: [t0, t1] along axis from axis_point
    bbox: list[float] = field(default_factory=list)
    samples: list[list[float]] = field(default_factory=list)  # points on the face boundary
    shape: Any = field(default=None, repr=False)  # TopoDS_Face, session-local, not serialised

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None and k != "shape"}


@dataclass
class PartGeometry:
    part: str
    step: str
    solid_index: int            # position among the solids read from the STEP file
    volume: float
    faces: list[FaceInfo]
    bbox: list[float]           # assembly frame (xmin, ymin, zmin, xmax, ymax, zmax)
    solid: Any = field(default=None, repr=False)   # TopoDS_Solid in the assembly frame
    body_step: str = ""         # single-body STEP (part frame), relative to the study folder

    def to_json(self) -> dict:
        return {"part": self.part, "step": self.step, "solid_index": self.solid_index,
                "body_step": self.body_step, "volume": self.volume, "bbox": self.bbox,
                "faces": [f.to_json() for f in self.faces]}


def body_step_path(study: Path, geometry_hash: str, solid_index: int) -> Path:
    return study / "mesh" / "bodies" / f"{geometry_hash[:16]}_{solid_index}.step"


# --------------------------------------------------------------------------- #
# Basic shape queries

def _xyz(p) -> list[float]:
    return [p.X(), p.Y(), p.Z()]


def volume_props(shape) -> tuple[float, list[float]]:
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    return g.Mass(), _xyz(g.CentreOfMass())


def surface_props(shape) -> tuple[float, list[float]]:
    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, g)
    return g.Mass(), _xyz(g.CentreOfMass())


def bbox(shape) -> list[float]:
    b = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, b, False, False)
    return [b.GetXMin(), b.GetYMin(), b.GetZMin(), b.GetXMax(), b.GetYMax(), b.GetZMax()]


def subshapes(shape, kind) -> list:
    m = _IndexedShapeMap()
    TopExp.MapShapes_s(shape, kind, m)
    return [m.FindKey(i) for i in range(1, m.Extent() + 1)]


def read_step(path: Path) -> list:
    """Solids in a STEP file, in file order. Units: mm (OCCT default)."""
    r = STEPControl_Reader()
    if r.ReadFile(str(path)) != IFSelect_RetDone:
        raise IOError(f"cannot read STEP file {path}")
    r.TransferRoots()
    shape = r.OneShape()
    out = []
    ex = TopExp_Explorer(shape, TopAbs_SOLID)
    while ex.More():
        out.append(TopoDS.Solid(ex.Current()))
        ex.Next()
    return out


def write_step(path: Path, shapes: list) -> None:
    w = STEPControl_Writer()
    for s in shapes:
        w.Transfer(s, STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    if w.Write(str(path)) != IFSelect_RetDone:
        raise IOError(f"cannot write STEP file {path}")


def transform(shape, T: np.ndarray):
    """Place a shape by a 4x4 matrix. Rigid matrices keep analytic surfaces (gp_Trsf);
    others (mirror, scale) use a general transform."""
    if np.allclose(T, np.eye(4)):
        return shape
    R, t = T[:3, :3], T[:3, 3]
    if np.allclose(R @ R.T, np.eye(3), atol=1e-9) and np.linalg.det(R) > 0:
        tr = gp_Trsf()
        tr.SetValues(*R[0], t[0], *R[1], t[1], *R[2], t[2])
        return BRepBuilderAPI_Transform(shape, tr, True).Shape()
    g = gp_GTrsf()
    g.SetVectorialPart(gp_Mat(*R.ravel().tolist()))
    g.SetTranslationPart(gp_XYZ(*t.tolist()))
    return BRepBuilderAPI_GTransform(shape, g, True).Shape()


def translated(shape, v) -> TopoDS_Shape:
    tr = gp_Trsf()
    tr.SetTranslation(gp_Vec(*[float(x) for x in v]))
    return BRepBuilderAPI_Transform(shape, tr, True).Shape()


def common(a, b, fuzzy: float = 0.0):
    """The common part of two shapes, or None. OpenCASCADE runs it on all cores with oriented bounding
    boxes pre-filtering sub-shapes: the same result, about 3 x faster on a weldment's 1,026 touching pairs
    (the interference check took 118 s of a 297-body resolve; measured, identical volumes).

    ``fuzzy`` (mm): sub-shapes this close count as coincident. A plain intersection of a bush against its link
    (near-coincident bore and flange faces) took 268 s where its four identical siblings took 0.1 s; with 0.001 mm
    it took 0.06 s, and every overlap volume on that model came out the same (2026-10-09)."""
    from OCP.OCP.collections import List_TopoDS_Shape
    op = BRepAlgoAPI_Common()
    args, tools = List_TopoDS_Shape(), List_TopoDS_Shape()
    args.Append(a)
    tools.Append(b)
    op.SetArguments(args)
    op.SetTools(tools)
    op.SetRunParallel(True)
    op.SetUseOBB(True)
    if fuzzy > 0:
        op.SetFuzzyValue(fuzzy)
    op.Build()
    if not op.IsDone():
        return None
    return op.Shape()


class MemberPath:
    """A member's path in the part frame, straight or a circular arc (PLAN.md Meshing: both in
    the first release). s is the arc length from the start.

    line: origin + s * axis.  arc: centre + R (cos q start + sin q (normal x start)), q = s / R,
    turning about `normal` (right hand)."""

    def __init__(self, route: dict):
        self.kind = route.get("path", "line")
        if self.kind == "arc":
            self.centre = np.asarray(route["centre"], float)
            self.normal = _unit(route["normal"])
            self.start = _unit(route["start"])
            self.radius = float(route["radius"])
            self.side = np.cross(self.normal, self.start)
        else:
            self.origin = np.asarray(route["origin"], float)
            self.axis = _unit(route["axis"])

    def s_of(self, p) -> float:
        p = np.asarray(p, float)
        if self.kind == "arc":
            d = p - self.centre
            return self.radius * float(np.arctan2(d @ self.side, d @ self.start))
        return float((p - self.origin) @ self.axis)

    def point(self, s: float) -> np.ndarray:
        if self.kind == "arc":
            q = s / self.radius
            return self.centre + self.radius * (np.cos(q) * self.start + np.sin(q) * self.side)
        return self.origin + s * self.axis

    def tangent(self, s: float) -> np.ndarray:
        if self.kind == "arc":
            q = s / self.radius
            return -np.sin(q) * self.start + np.cos(q) * self.side
        return self.axis

    def move(self, X: np.ndarray, s0: float, s1: float) -> np.ndarray:
        """Carry points from the section plane at s0 to the one at s1 (translate or rotate)."""
        if self.kind == "arc":
            q = (s1 - s0) / self.radius
            n, c = self.normal, self.centre
            d = X - c
            rot = d * np.cos(q) + np.cross(n, d) * np.sin(q) + np.outer(d @ n, n) * (1 - np.cos(q))
            return c + rot
        return X + (s1 - s0) * self.axis

    def cut_face(self, s: float, size: float):
        """A face across the member at s: a full plane for a line; for an arc a half-plane
        bounded at the centre, so it never cuts the far side of the arc."""
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
        from OCP.gp import gp_Ax3, gp_Dir, gp_Pln
        p, t = self.point(s), self.tangent(s)
        if self.kind == "arc":
            u = (p - self.centre) / self.radius
            ax = gp_Ax3(gp_Pnt(*self.centre), gp_Dir(*t), gp_Dir(*u))
            return BRepBuilderAPI_MakeFace(gp_Pln(ax), 0.0, self.radius + size, -size, size).Face()
        return BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(*p), gp_Dir(*t)), -size, size, -size, size).Face()


def _unit(v) -> np.ndarray:
    v = np.asarray(v, float)
    return v / np.linalg.norm(v)


def split_member(solid, path: MemberPath, cuts: list[float]) -> list[tuple[Any, float, float]]:
    """Split a member at path positions s. Returns [(piece, s_lo, s_hi)] ordered along the
    path, where [s_lo, s_hi] is the interval between neighbouring cuts holding the piece."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.OCP.collections import List_TopoDS_Shape   # OCP 8 name of TopTools_ListOfShape

    bb = np.asarray(bbox(solid))
    size = 2.0 * float(np.linalg.norm(bb[3:] - bb[:3])) + 1.0
    args, tools = List_TopoDS_Shape(), List_TopoDS_Shape()
    args.Append(solid)
    for s in cuts:
        tools.Append(path.cut_face(s, size))
    sp = BRepAlgoAPI_Splitter()
    sp.SetArguments(args)
    sp.SetTools(tools)
    sp.Build()
    if not sp.IsDone():
        raise RuntimeError("splitting the member at its span ends failed")
    bounds = [-np.inf] + sorted(cuts) + [np.inf]
    out = []
    for piece in subshapes(sp.Shape(), TopAbs_SOLID):
        _, c = volume_props(piece)
        k = int(np.searchsorted(bounds, path.s_of(c))) - 1
        out.append((TopoDS.Solid(piece), bounds[k], bounds[k + 1]))
    return sorted(out, key=lambda x: (x[1], x[2]))


def split_along_axis(solid, origin, axis, cuts: list[float]) -> list[tuple[Any, float, float]]:
    """Split a solid by planes normal to `axis` at path positions s (measured from origin).

    Returns [(piece, s_lo, s_hi)] ordered along the axis, where [s_lo, s_hi] is the interval
    between neighbouring cut planes (or the solid's own extent) that holds the piece.
    """
    return split_member(solid, MemberPath({"path": "line", "origin": origin, "axis": axis}), cuts)


# --------------------------------------------------------------------------- #
# Faces

def boundary_samples(face, per_curve: int = 3) -> list[list[float]]:
    """Points on the face boundary: every vertex plus a few points along each edge."""
    pts = []
    for e in subshapes(face, TopAbs_EDGE):
        c = BRepAdaptor_Curve(TopoDS.Edge(e))
        t0, t1 = c.FirstParameter(), c.LastParameter()
        for k in range(per_curve):
            pts.append(_xyz(c.Value(t0 + (k + 0.5) / per_curve * (t1 - t0))))
    for v in subshapes(face, TopAbs_VERTEX):
        pts.append(_xyz(BRep_Tool.Pnt_s(TopoDS.Vertex(v))))
    return pts


def _point_normal(face, ad: BRepAdaptor_Surface, u: float, v: float) -> tuple[np.ndarray, np.ndarray]:
    p, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
    ad.D1(u, v, p, du, dv)
    n = np.cross(np.array([du.X(), du.Y(), du.Z()]), np.array([dv.X(), dv.Y(), dv.Z()]))
    n /= np.linalg.norm(n)
    if face.Orientation() == TopAbs_REVERSED:
        n = -n
    return np.array(_xyz(p)), n


def face_info(face, index: int) -> FaceInfo:
    face = TopoDS.Face(face)
    ad = BRepAdaptor_Surface(face)
    stype = SURFACE_TYPES.get(ad.GetType(), "other")
    area, cen = surface_props(face)
    info = FaceInfo(index, stype, area, cen, bbox=bbox(face), samples=boundary_samples(face), shape=face)
    um, vm = 0.5 * (ad.FirstUParameter() + ad.LastUParameter()), 0.5 * (ad.FirstVParameter() + ad.LastVParameter())
    if stype == "plane":
        _, n = _point_normal(face, ad, um, vm)
        info.normal = n.tolist()
    elif stype in ("cylinder", "cone"):
        ax = ad.Cylinder().Axis() if stype == "cylinder" else ad.Cone().Axis()
        a = np.array(_xyz(ax.Direction()))
        c = np.array(_xyz(ax.Location()))
        info.axis = a.tolist()
        info.axis_point = c.tolist()
        p, n = _point_normal(face, ad, um, vm)
        radial = (p - c) - np.dot(p - c, a) * a
        info.convex = bool(np.dot(radial, n) > 0)
        if stype == "cylinder":
            info.radius = ad.Cylinder().Radius()
        ts = [float(np.dot(np.asarray(s) - c, a)) for s in info.samples]
        info.axial_range = [min(ts), max(ts)] if ts else None
    return info


def part_faces(solid) -> list[FaceInfo]:
    return [face_info(f, i) for i, f in enumerate(subshapes(solid, TopAbs_FACE))]


# --------------------------------------------------------------------------- #
# Matching to the model

def transform_matrix(part: Part) -> np.ndarray:
    return np.asarray(part.transform, dtype=float).reshape(4, 4)


def solid_props(solids: list) -> list[tuple[float, list[float], list[float]]]:
    """(volume, centroid, bounding box) of each solid: worked out once per STEP file."""
    return [(*volume_props(s), bbox(s)) for s in solids]


def match_solids(parts: list[Part], props: list, step: str) -> tuple[dict[str, int], list[Diagnostic]]:
    """Which solid of one STEP file is each part's body: {part id: solid index}, and diagnostics.

    props: solid_props of the file's solids. Matched on the bounding box, which the STEP round trip keeps
    to well under a micron, with the centroid as a tie-break, one body to one solid (an assignment over
    the whole file, so a multi-body part's bodies share its solids out). The volume is checked but does not
    decide: OpenCASCADE's volume of a converted solid can differ from SolidWorks' (a 297-body weldment:
    0.1 % typical, 61 % worst, every bounding box within 0.001 mm; found live), so a difference beyond
    BODY_VOLUME_WARN is a warning. Solids that coincide exactly are interchangeable; when the file has more
    of them than bodies claim, the export holds a duplicate no body describes (BODY_AMBIGUOUS)."""
    # instances of one part (two components of the same file) share its solids: the assignment is over the
    # part's distinct bodies (by body name; "" for a single-body part), every instance taking its body's solid
    groups: dict[str, list[Part]] = {}
    for p in parts:
        groups.setdefault(p.id.split("/body:")[1] if "/body:" in p.id else "", []).append(p)
    if any(len(g) > 1 for g in groups.values()):
        reps = [g[0] for g in groups.values()]
        got_r, diags_r = match_solids(reps, props, step)
        got, diags = {}, []
        for g in groups.values():
            if g[0].id in got_r:
                got.update({p.id: got_r[g[0].id] for p in g})
            for d in (x for x in diags_r if x.where.parts == [g[0].id]):
                diags += [d.model_copy(update={"id": f"{d.code}:{p.id}", "what": d.what.replace(g[0].id, p.id),
                                               "where": d.where.model_copy(update={"parts": [p.id]})})
                          for p in g]
        return got, diags
    from scipy.optimize import linear_sum_assignment
    diags: list[Diagnostic] = []
    n, m = len(parts), len(props)
    if m == 0:
        return {}, [make("BODY_UNMATCHED", where={"parts": [p.id]}, part=p.id, step=step,
                         evidence={"expected": p.body.model_dump(), "solids_in_file": 0}) for p in parts]
    V = np.array([p[0] for p in props], float)
    C = np.array([p[1] for p in props], float)
    B = np.array([p[2] for p in props], float)
    tols = np.array([max(defaults.BODY_CENTROID_TOL, 1e-5 * math.dist(p.body.bbox_min, p.body.bbox_max)) for p in parts])
    bb = np.array([list(p.body.bbox_min) + list(p.body.bbox_max) for p in parts], float)
    ce = np.array([p.body.centroid for p in parts], float)
    e_bb = np.abs(bb[:, None, :] - B[None, :, :]).max(axis=2)              # (n, m), mm
    e_c = np.linalg.norm(ce[:, None, :] - C[None, :, :], axis=2)
    ok = e_bb <= 10 * tols[:, None]
    BIG = 1e12
    cost = np.where(ok, e_bb / tols[:, None] + 0.1 * e_c / tols[:, None], BIG)
    rows, cols = linear_sum_assignment(cost)
    got: dict[str, int] = {}
    for r, c in zip(rows, cols):
        if cost[r, c] < BIG:
            got[parts[r].id] = int(c)
    for k, p in enumerate(parts):
        where = {"parts": [p.id]}
        if p.id not in got:
            diags.append(make("BODY_UNMATCHED", where=where, part=p.id, step=step,
                              evidence={"expected": p.body.model_dump(), "solids_in_file": m}))
            continue
        i = got[p.id]
        # solids identical to the one taken (same box and centroid) are interchangeable with it; if the
        # file has more of them than bodies take, one is a duplicate no body describes
        twins = np.nonzero((np.abs(B - B[i]).max(axis=1) <= tols[k]) & (np.linalg.norm(C - C[i], axis=1) <= tols[k]))[0]
        if len(twins) > 1:
            taken = sum(1 for j in got.values() if j in set(twins.tolist()))
            if len(twins) > taken:
                diags.append(make("BODY_AMBIGUOUS", where=where, part=p.id, step=step, count=int(len(twins))))
                del got[p.id]
                continue
        dv = abs(V[i] - p.body.volume) / max(p.body.volume, 1e-30)
        if dv > defaults.BODY_VOLUME_WARN:
            diags.append(make("BODY_VOLUME_DIFFERS", where=where, part=p.id, step=step,
                              percent=round(100 * dv, 2), evidence={"solidworks_mm3": p.body.volume,
                                                                    "step_mm3": float(V[i])}))
    return got, diags


def faces_at_point(point, faces: list[FaceInfo], tol: float = defaults.FACE_POINT_TOL) -> list[int]:
    """The faces a point lies on (within tol, mm): one for a point inside a face, two or more on an edge."""
    v = BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float, point))).Vertex()
    hits = []
    for f in faces:
        if f.bbox and any(point[i] < f.bbox[i] - tol or point[i] > f.bbox[i + 3] + tol for i in range(3)):
            continue                                   # cheap box test first
        d = BRepExtrema_DistShapeShape(v, f.shape)
        if d.IsDone() and d.Value() <= tol:
            hits.append(f.index)
    return hits


def fingerprint_of(f: FaceInfo) -> Fingerprint:
    return Fingerprint(surface=f.surface, area=f.area, centroid=tuple(f.centroid),
                       normal=tuple(f.normal) if f.normal else None, axis=tuple(f.axis) if f.axis else None,
                       radius=f.radius)


def match_face(ref: FaceRef, faces: list[FaceInfo]) -> list[int]:
    if ref.fingerprint is None:                        # given by a point on the face (D53)
        return faces_at_point(ref.point, faces)
    fp = ref.fingerprint
    ctol = max(defaults.FACE_CENTROID_TOL, defaults.FACE_CENTROID_RTOL * math.sqrt(fp.area))
    hits = []
    for f in faces:
        if f.surface != fp.surface:
            continue
        if abs(f.area - fp.area) > defaults.FACE_AREA_RTOL * fp.area:
            continue
        if math.dist(f.centroid, fp.centroid) > ctol:
            continue
        if fp.normal is not None and f.normal is not None and float(np.dot(fp.normal, f.normal)) < defaults.FACE_NORMAL_COS:
            continue
        if fp.radius is not None and f.radius is not None and abs(f.radius - fp.radius) > 1e-3 * fp.radius:
            continue
        hits.append(f.index)
    return hits


def resolve_geometry(study: Path, model: Model, on_part=None) -> tuple[dict[str, PartGeometry], dict[str, tuple[str, int]], list[Diagnostic]]:
    """Load every part in the assembly frame, fingerprint faces, match face_refs. on_part(done) is called
    after each part, for the job's progress."""
    diags: list[Diagnostic] = []
    geoms: dict[str, PartGeometry] = {}
    # each STEP file read and measured once, its solids shared out among the parts that use it (a
    # multi-body part's bodies all come from one file: read once per body, a 297-body weldment's 40 MB file
    # took hours; found live)
    chosen: dict[str, tuple[Any, int]] = {}
    by_step: dict[str, list[Part]] = {}
    for part in model.parts:
        by_step.setdefault(part.step, []).append(part)
    for step, users in by_step.items():
        path = study / step
        if not path.is_file():
            diags += [make("STEP_MISSING", where={"parts": [p.id]}, part=p.id, step=step) for p in users]
            continue
        try:
            solids = read_step(path)
        except Exception as e:
            diags += [make("STEP_MISSING", where={"parts": [p.id]}, part=p.id, step=step, evidence={"error": str(e)})
                      for p in users]
            continue
        # a part without a body description (a file not written by the add-in, D53) is the file's only solid
        bare = [p for p in users if p.body is None]
        if bare:
            if len(users) == 1 and len(solids) == 1:
                chosen[bare[0].id] = (solids[0], 0)
            else:
                diags += [make("BODY_UNMATCHED", where={"parts": [p.id]}, part=p.id, step=step,
                               evidence={"expected": "the file's only solid (no body description)",
                                         "solids_in_file": len(solids), "parts_using_file": len(users)})
                          for p in bare]
            users = [p for p in users if p.body is not None]
            if not users:
                continue
        got, d = match_solids(users, solid_props(solids), step)
        diags += d
        chosen.update({pid: (solids[i], i) for pid, i in got.items()})
    for k, part in enumerate(model.parts):
        if on_part is not None:
            on_part(k)
        if part.id not in chosen:
            continue
        solid, idx = chosen[part.id]
        # The matched body is written to its own STEP file and read back. Mesher and
        # tagging read that same file, so face numbering is shared by construction.
        body = body_step_path(study, part.geometry_hash, idx)
        if not body.exists():
            write_step(body, [solid])
        solid = read_step(body)[0]
        placed = transform(solid, transform_matrix(part))
        vol, _ = volume_props(placed)
        g = PartGeometry(part.id, part.step, idx, vol, part_faces(placed), bbox(placed), placed)
        g.body_step = str(body.relative_to(study))
        geoms[part.id] = g
    face_map: dict[str, tuple[str, int]] = {}
    for ref in model.face_refs:
        g = geoms.get(ref.part)
        if g is None:
            continue  # the part is already reported
        hits = match_face(ref, g.faces)
        where = {"parts": [ref.part], "faces": [ref.id]}
        given = ({"fingerprint": ref.fingerprint.model_dump(exclude_none=True)} if ref.fingerprint is not None
                 else {"point": list(ref.point)})
        if not hits:
            diags.append(make("FACE_UNMATCHED", where=where, face=ref.id, part=ref.part, evidence=given))
        elif len(hits) > 1:
            diags.append(make("FACE_AMBIGUOUS", where=where, face=ref.id, part=ref.part, count=len(hits),
                              evidence={"candidates": hits}))
        else:
            face_map[ref.id] = (ref.part, hits[0])
            if ref.fingerprint is None:                # later stages may read it (classify._sw_axis)
                ref.fingerprint = fingerprint_of(g.faces[hits[0]])
    return geoms, face_map, diags
