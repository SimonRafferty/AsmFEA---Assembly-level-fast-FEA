"""Free holes (D101): which holes a part's mesh refines, and the Gmsh size fields that do it.

A free hole is a concave cylindrical face that no connection and no fixture uses: a bolt's hole is clamped, a pin's
bore is tied, and either peak is set by how the connection is modelled, not by the mesh. Its notch peak is what the
plot shows and what fatigue will need, and curvature sizing alone (held to half the element size) left a 20 mm hole in
a 5 mm plate with 9 elements round it, 16 % low (m13). A refined hole gets HOLE_ELEMENTS round its edge, growing
back to the part's size away from it.

Holes are found once per study from geometry.json (assembly frame) and handed to the mesher in the part frame, as
the axis point at the middle of the bore, the axis, the radius and the bore's half-length. The size at a point is set
from its distance to that bore: radially from the cylinder, and along the axis beyond the bore's ends.
"""
from __future__ import annotations

import math

import numpy as np

from .. import defaults


def _face_refs(obj, out: set) -> None:
    """Every (part, face) pair named anywhere in a connection record (sides, holes, butt masters and slaves...)."""
    if isinstance(obj, dict):
        if "part" in obj and isinstance(obj.get("face"), (int, float)):
            out.add((obj["part"], int(obj["face"])))
        for v in obj.values():
            _face_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _face_refs(v, out)


def used_faces(conns: list[dict], fixture_faces: list[tuple[str, int]]) -> set[tuple[str, int]]:
    """(part, face index) of every face a connection or a fixture uses."""
    out: set = set()
    _face_refs(conns, out)
    out.update((p, int(i)) for p, i in fixture_faces)
    return out


def free_holes(part_id: str, faces: list[dict], transform: np.ndarray, used: set, wall: float | None) -> list[dict]:
    """The part's free notches in its own frame: holes and fillets, [{"c", "n", "r", "half"}] for a concave
    cylinder and [{"kind": "torus", "c", "n", "R", "r"}] for a concave torus (F1).

    wall: the thickness of a swept wall, None for a tet-meshed solid. On a swept wall a cylinder must run through it
    (no longer than HOLE_BORE_T x wall): a hole, or a fillet in the outline; a bend's inside is longer and is left
    alone. On a solid, a full hole is no longer than HOLE_BORE_D x its diameter (a drilled hole, not a tube's bore),
    and a fillet (a cylinder spanning less than a full turn) no longer than FILLET_LENGTH_R x its radius. A concave
    torus is a shoulder fillet round a shaft."""
    Tinv = np.linalg.inv(np.asarray(transform, float).reshape(4, 4))
    out = []
    for f in faces:
        if f.get("surface") not in ("cylinder", "torus") or f.get("convex") is not False \
                or (part_id, int(f["index"])) in used:
            continue
        if not (f.get("axis") and f.get("axis_point")):
            continue
        n = np.asarray(f["axis"], float)
        n = n / np.linalg.norm(n)
        if f["surface"] == "torus":
            if wall or not (f.get("major_radius") and f.get("minor_radius")):
                continue                        # a swept wall's torus is a bend's corner, not a fillet
            c = np.asarray(f["axis_point"], float)
            out.append({"kind": "torus", "c": [round(float(v), 6) for v in Tinv[:3, :3] @ c + Tinv[:3, 3]],
                        "n": [round(float(v), 9) for v in Tinv[:3, :3] @ n],
                        "R": round(float(f["major_radius"]), 6), "r": round(float(f["minor_radius"]), 6)})
            continue
        if not (f.get("radius") and f.get("axial_range")):
            continue
        a0, a1 = map(float, f["axial_range"])
        r = float(f["radius"])
        fillet = f.get("span") is not None and f["span"] < 1.9 * math.pi
        if wall:
            if (a1 - a0) > defaults.HOLE_BORE_T * wall:
                continue
        elif (a1 - a0) > (defaults.FILLET_LENGTH_R * r if fillet else defaults.HOLE_BORE_D * 2 * r):
            continue
        c = np.asarray(f["axis_point"], float) + n * 0.5 * (a0 + a1)
        out.append({"c": [round(float(v), 6) for v in Tinv[:3, :3] @ c + Tinv[:3, 3]],
                    "n": [round(float(v), 9) for v in Tinv[:3, :3] @ n],
                    "r": round(r, 6), "half": round(0.5 * (a1 - a0), 6)})
    return out


def sized(holes: list[dict], factor: float, size: float) -> list[dict]:
    """The holes worth refining at one level, each with its target size "h": HOLE_ELEMENTS round the edge at
    standard fidelity, times the level's factor (fidelity x level), not below HOLE_SIZE_MIN_FRACTION x size. A hole
    that curvature sizing already meshes as finely (big holes) is left out. size: the part's element size there (the
    skin's in-plane size for a swept wall)."""
    curv_floor = size * defaults.MESH_SIZE_MIN_FRACTION
    out = []
    for hole in holes:
        h = max(2 * math.pi * hole["r"] / defaults.HOLE_ELEMENTS * factor, defaults.HOLE_SIZE_MIN_FRACTION * size)
        today = min(max(2 * math.pi * hole["r"] / defaults.MESH_CURVATURE_POINTS, curv_floor), size)
        if h < 0.999 * today:
            out.append({**hole, "h": round(h, 6)})
    return out


def _distance_expr(hole: dict) -> str:
    """A Gmsh MathEval expression: the distance from (x, y, z) to the notch: a hole's bore or a fillet (a finite
    cylinder), or a shoulder fillet (a torus: from the tube's centre circle, less the tube's radius)."""
    cx, cy, cz = (f"{v:.9g}" for v in hole["c"])
    nx, ny, nz = (f"{v:.9g}" for v in hole["n"])
    d = f"(x-({cx}))", f"(y-({cy}))", f"(z-({cz}))"
    a = f"({d[0]}*({nx})+{d[1]}*({ny})+{d[2]}*({nz}))"                 # along the axis from the bore's middle
    rho = f"Sqrt(Fabs({d[0]}^2+{d[1]}^2+{d[2]}^2-{a}^2))"              # from the axis
    if hole.get("kind") == "torus":
        return f"Fabs(Sqrt(({rho}-{hole['R']:.9g})^2+{a}^2)-{hole['r']:.9g})"
    beyond = f"((Fabs({a})-{hole['half']:.9g}+Fabs(Fabs({a})-{hole['half']:.9g}))/2)"   # past the bore's ends, else 0
    return f"Sqrt(({rho}-{hole['r']:.9g})^2+{beyond}^2)"


def to_refine(holes: list[dict], size: float) -> list[dict]:
    """The holes whose size is below the size away from them (the others leave the mesh as it is)."""
    return [h for h in holes if h["h"] < size]


def write_size_view(path, X: np.ndarray, tri: np.ndarray) -> None:
    """A Gmsh post-processing view of the mesh's own element size: each corner node's mean edge length, on the
    triangles' corners (tri: (m, >= 3) 0-based rows of X, corners first). The second pass reads it back as its
    size field away from the holes, so the mesh there stays as the first pass made it, curvature sizing included."""
    T = np.asarray(tri)[:, :3]
    e = np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]])
    le = np.linalg.norm(X[e[:, 0]] - X[e[:, 1]], axis=1)
    s, c = np.zeros(len(X)), np.zeros(len(X))
    for k in (0, 1):
        np.add.at(s, e[:, k], le)
        np.add.at(c, e[:, k], 1)
    h = s / np.maximum(c, 1)
    with open(path, "w") as f:
        f.write('View "size" {\n')
        for t in T:
            f.write("ST(" + ",".join(f"{v:.9g}" for v in X[t].ravel()) + "){" + ",".join(f"{h[j]:.9g}" for j in t)
                    + "};\n")
        f.write("};\n")


def gmsh_fields(holes: list[dict], size: float, view) -> tuple[list[str], float]:
    """.geo lines for the second pass: the first pass's sizes (view: its write_size_view file) and each hole's
    size, whichever is smaller, as the background field; curvature sizing off (the view carries it) and the
    minimum size the smallest hole's. Also returns that smallest size. size: the size away from the holes."""
    from pathlib import Path
    lines, ids = [f'Merge "{Path(view).resolve().as_posix()}";', "Field[1] = PostView; Field[1].ViewIndex = 0;"], [1]
    for k, hole in enumerate(holes):
        fm, ft = 2 * k + 2, 2 * k + 3
        near = defaults.HOLE_NEAR_R * hole["r"]
        grow = (size - hole["h"]) / defaults.HOLE_SIZE_GRADIENT
        lines += [f'Field[{fm}] = MathEval; Field[{fm}].F = "{_distance_expr(hole)}";',
                  f"Field[{ft}] = Threshold; Field[{ft}].InField = {fm}; Field[{ft}].SizeMin = {hole['h']:.9g}; "
                  f"Field[{ft}].SizeMax = {size:.9g}; Field[{ft}].DistMin = {near:.9g}; "
                  f"Field[{ft}].DistMax = {near + grow:.9g};"]
        ids.append(ft)
    fmin = 2 * len(holes) + 2
    hmin = min(h["h"] for h in holes)
    lines += [f"Field[{fmin}] = Min; Field[{fmin}].FieldsList = {{{', '.join(map(str, ids))}}};",
              f"Background Field = {fmin};",
              "Mesh.MeshSizeFromCurvature = 0;",
              f"Mesh.MeshSizeMin = {hmin:.9g};"]
    return lines, hmin
