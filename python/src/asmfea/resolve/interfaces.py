"""Geometric interface search between part pairs (assembly frame, OCP shapes).

Finds, per candidate part pair:
  - planar face pairs: opposing normals, within the gap tolerance, overlap area > minimum
  - cylindrical face pairs: a pin (convex) in a hole (concave), coaxial, radii within gap
  - line contacts: a cylinder lying on a plane, or two parallel cylinders touching
  - near misses: planar pairs just outside the gap tolerance
  - interferences: solids that overlap by more than the overlap tolerance
"""
from __future__ import annotations

import hashlib
import math

import numpy as np
from OCP.TopAbs import TopAbs_FACE

from .. import defaults
from ..geometry import (FaceInfo, PartGeometry, boundary_samples, common, subshapes, surface_props,
                        translated, volume_props)


def interface_id(pa: str, fa: int, pb: str, fb: int) -> str:
    key = "|".join(sorted([f"{pa}#{fa}", f"{pb}#{fb}"]))
    return "I-" + hashlib.sha1(key.encode()).hexdigest()[:10]


def bbox_overlap(a, b, grow: float) -> bool:
    return all(a[i] - grow <= b[i + 3] and b[i] - grow <= a[i + 3] for i in range(3))


def planar_overlap(fa: FaceInfo, fb: FaceInfo, offset: float) -> tuple[float, list[list[float]]]:
    """(area, boundary sample points) of face b projected onto a's plane, intersected with a."""
    moved = translated(fb.shape, np.asarray(fa.normal) * offset)
    res = common(fa.shape, moved)
    if res is None:
        return 0.0, []
    area, samples = 0.0, []
    for f in subshapes(res, TopAbs_FACE):
        a, _ = surface_props(f)
        area += a
        samples += boundary_samples(f)
    return area, samples


def _axis_distance(pa, a, pb) -> float:
    d = np.asarray(pb) - np.asarray(pa)
    return float(np.linalg.norm(d - np.dot(d, a) * a))


def find_interfaces(geoms: dict[str, PartGeometry], tol: float | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (interfaces, near_misses, line_contacts). Faces within ``tol`` (mm) of each other, a
    gap or an overlap, count as touching (the study's bond tolerance; default GAP_TOL)."""
    gap = defaults.GAP_TOL if tol is None else float(tol)
    near = defaults.NEAR_MISS_FACTOR * max(gap, defaults.GAP_TOL)
    interfaces, near_misses, lines = [], [], []
    ids = sorted(geoms)
    for i, pa in enumerate(ids):
        for pb in ids[i + 1:]:
            ga, gb = geoms[pa], geoms[pb]
            if not bbox_overlap(ga.bbox, gb.bbox, near):
                continue
            for fa in ga.faces:
                for fb in gb.faces:
                    if not bbox_overlap(fa.bbox, fb.bbox, near):
                        continue
                    pair = (fa.surface, fb.surface)
                    if pair == ("plane", "plane"):
                        _plane_plane(pa, fa, pb, fb, gap, near, interfaces, near_misses)
                    elif pair == ("cylinder", "cylinder"):
                        _cyl_cyl(pa, fa, pb, fb, gap, interfaces, lines)
                    elif "plane" in pair and "cylinder" in pair:
                        _plane_cyl(pa, fa, pb, fb, gap, lines)
    return interfaces, near_misses, lines


def _plane_plane(pa, fa, pb, fb, gap, near, interfaces, near_misses):
    na, nb = np.asarray(fa.normal), np.asarray(fb.normal)
    if float(np.dot(na, nb)) > defaults.OPPOSING_NORMAL_COS:
        return
    # signed distance of b's plane from a's, along a's outward normal
    d = float(np.dot(np.asarray(fb.centroid) - np.asarray(fa.centroid), na))
    if d < -gap or d > near:
        return
    area, samples = planar_overlap(fa, fb, -d)
    if area < defaults.MIN_INTERFACE_AREA:
        return
    rec = {"id": interface_id(pa, fa.index, pb, fb.index), "parts": [pa, pb],
           "sides": [{"part": pa, "face": fa.index}, {"part": pb, "face": fb.index}],
           "geometry": "plane", "gap": d, "area": area, "normal": na.tolist(), "samples": samples}
    (interfaces if d <= gap else near_misses).append(rec)


def _cyl_cyl(pa, fa, pb, fb, gap, interfaces, lines):
    a, b = np.asarray(fa.axis), np.asarray(fb.axis)
    if fa.radius is None or fb.radius is None or abs(abs(float(np.dot(a, b))) - 1) > 1e-4:
        return
    dist = _axis_distance(fa.axis_point, a, fb.axis_point)
    if fa.convex != fb.convex and dist <= defaults.COAX_TOL:
        hole, pin = (fa, fb) if not fa.convex else (fb, fa)
        clearance = hole.radius - pin.radius
        if -gap <= clearance <= gap:
            # axial overlap of the two faces
            ta = [float(np.dot(np.asarray(p) - np.asarray(hole.axis_point), a)) for p in fa.samples]
            tb = [float(np.dot(np.asarray(p) - np.asarray(hole.axis_point), a)) for p in fb.samples]
            length = min(max(ta), max(tb)) - max(min(ta), min(tb))
            if length <= 0:
                return
            span = (hole.area / (2 * math.pi * hole.radius)) / max(hole.axial_range[1] - hole.axial_range[0], 1e-9)
            area = 2 * math.pi * pin.radius * length * min(1.0, span)
            if area < defaults.MIN_INTERFACE_AREA:
                return
            short = fa if (max(ta) - min(ta)) <= (max(tb) - min(tb)) else fb
            interfaces.append({"id": interface_id(pa, fa.index, pb, fb.index), "parts": [pa, pb],
                               "sides": [{"part": pa, "face": fa.index}, {"part": pb, "face": fb.index}],
                               "geometry": "cylinder", "gap": clearance, "area": area, "axis": a.tolist(),
                               "axis_point": hole.axis_point, "radius": pin.radius, "length": length,
                               "samples": short.samples})
    elif fa.convex and fb.convex and abs(dist - fa.radius - fb.radius) <= gap:
        # the contact line: on a's surface, facing b, over the length both faces share
        d = np.asarray(fb.axis_point, float) - np.asarray(fa.axis_point, float)
        u = d - np.dot(d, a) * a
        u /= max(np.linalg.norm(u), 1e-300)
        off = float(np.dot(d, a))
        sgn = 1.0 if float(np.dot(a, b)) > 0 else -1.0
        rb = sorted([off + sgn * t for t in (fb.axial_range or [0.0, 0.0])])
        ra = fa.axial_range or [0.0, 0.0]
        t0, t1 = max(ra[0], rb[0]), min(ra[1], rb[1])
        rec = {"parts": [pa, pb], "faces": [fa.index, fb.index], "kind": "cylinder-cylinder", "axis": a.tolist()}
        if t1 > t0:
            base = np.asarray(fa.axis_point, float) + u * fa.radius
            rec.update(segment=[(base + a * t0).tolist(), (base + a * t1).tolist()], normal=u.tolist())
        lines.append(rec)


def _plane_cyl(pa, fa, pb, fb, gap, lines):
    plane, cyl = (fa, fb) if fa.surface == "plane" else (fb, fa)
    if not cyl.convex or cyl.radius is None:
        return
    n, a = np.asarray(plane.normal), np.asarray(cyl.axis)
    if abs(float(np.dot(n, a))) > 1e-4:
        return
    d = float(np.dot(np.asarray(cyl.axis_point) - np.asarray(plane.centroid), n))
    if abs(d - cyl.radius) <= gap:
        # the contact line: the cylinder's line nearest the plane, over the length the plane covers
        foot = np.asarray(cyl.axis_point, float) - n * d
        ra = cyl.axial_range or [0.0, 0.0]
        sp = [float(np.dot(np.asarray(q, float) - foot, a)) for q in plane.samples] or [ra[0], ra[1]]
        t0, t1 = max(ra[0], min(sp)), min(ra[1], max(sp))
        rec = {"parts": [pa, pb], "faces": [fa.index, fb.index], "kind": "plane-cylinder", "axis": a.tolist()}
        if t1 > t0:
            rec.update(segment=[(foot + a * t0).tolist(), (foot + a * t1).tolist()], normal=n.tolist())
        lines.append(rec)


def find_interferences(geoms: dict[str, PartGeometry]) -> list[dict]:
    """Pairs of solids overlapping by more than OVERLAP_VOL_TOL (mm^3). Pairs bonded across an
    overlap within the bond tolerance are dropped by the caller."""
    out = []
    ids = sorted(geoms)
    for i, pa in enumerate(ids):
        for pb in ids[i + 1:]:
            ga, gb = geoms[pa], geoms[pb]
            if not bbox_overlap(ga.bbox, gb.bbox, -defaults.OVERLAP_TOL):
                continue
            res = common(ga.solid, gb.solid)
            if res is None:
                continue
            vol, _ = volume_props(res)
            if vol > defaults.OVERLAP_VOL_TOL:
                out.append({"parts": [pa, pb], "volume": vol})
    return out
