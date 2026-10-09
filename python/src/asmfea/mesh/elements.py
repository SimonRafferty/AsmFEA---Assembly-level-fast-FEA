"""Element tables shared by the mesher, deck writer and post-processor.

perm[i] = gmsh local index of the node CalculiX expects in position i. Orderings from
the gmsh reference manual (node ordering) and the CalculiX manual (C3D10, C3D15,
C3D20). Proven in milestone 0 (spikes/m0) by the midside-midpoint check and by solved
results; tests/test_elements.py re-checks them.
"""
from __future__ import annotations

import numpy as np

GMSH_TO_CCX = {
    11: ("C3D10", [0, 1, 2, 3, 4, 5, 6, 7, 9, 8]),
    18: ("C3D15", [0, 1, 2, 3, 4, 5, 6, 9, 7, 12, 14, 13, 8, 10, 11]),
    17: ("C3D20R", [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 13, 9, 16, 18, 19, 17, 10, 12, 14, 15]),
}
N_CORNER = {"C3D10": 4, "C3D15": 6, "C3D20R": 8}
MIDSIDE = {
    "C3D10": [(0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3)],
    "C3D15": [(0, 1), (1, 2), (2, 0), (3, 4), (4, 5), (5, 3), (0, 3), (1, 4), (2, 5)],
    "C3D20R": [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4),
               (0, 4), (1, 5), (2, 6), (3, 7)],
}
# Face corner lists (0-based); face k is CalculiX face S(k+1) / P(k+1).
FACES = {
    "C3D10": [[0, 1, 2], [0, 3, 1], [1, 3, 2], [2, 3, 0]],
    "C3D15": [[0, 1, 2], [3, 4, 5], [0, 1, 4, 3], [1, 2, 5, 4], [2, 0, 3, 5]],
    "C3D20R": [[0, 1, 2, 3], [4, 7, 6, 5], [0, 4, 5, 1], [1, 5, 6, 2], [2, 6, 7, 3], [3, 7, 4, 0]],
}
# Full face node lists (corners then midsides), CalculiX order, for consistent loads.
FACE_NODES = {}
for _et, _faces in FACES.items():
    nc = N_CORNER[_et]
    pairs = MIDSIDE[_et]
    lst = []
    for f in _faces:
        mids = []
        for a, b in zip(f, f[1:] + f[:1]):
            k = next(i for i, p in enumerate(pairs) if set(p) == {a, b})
            mids.append(nc + k)
        lst.append(f + mids)
    FACE_NODES[_et] = lst

# Column permutation that mirrors an element (fixes a negative Jacobian).
FLIP = {
    "C3D10": [0, 2, 1, 3, 6, 5, 4, 7, 9, 8],
    "C3D15": [0, 2, 1, 3, 5, 4, 8, 7, 6, 11, 10, 9, 12, 14, 13],
    "C3D20R": [0, 3, 2, 1, 4, 7, 6, 5, 11, 10, 9, 8, 15, 14, 13, 12, 16, 19, 18, 17],
}


def corner_volume_sign(etype: str, P: np.ndarray) -> np.ndarray:
    """Sign of a corner-based volume measure for elements P = (n, nnodes, 3)."""
    a, b, c = {"C3D10": (1, 2, 3), "C3D15": (1, 2, 3), "C3D20R": (1, 3, 4)}[etype]
    v = np.einsum("ij,ij->i", P[:, a] - P[:, 0], np.cross(P[:, b] - P[:, 0], P[:, c] - P[:, 0]))
    return np.sign(v)


def midside_offset(etype: str, P: np.ndarray) -> float:
    """Worst midside-node offset from its corners' midpoint, relative to edge length."""
    nc = N_CORNER[etype]
    worst = 0.0
    for k, (i, j) in enumerate(MIDSIDE[etype]):
        mid = 0.5 * (P[:, i] + P[:, j])
        edge = np.linalg.norm(P[:, i] - P[:, j], axis=1)
        worst = max(worst, float((np.linalg.norm(P[:, nc + k] - mid, axis=1) / edge).max()))
    return worst


def _tet10_dN() -> list[np.ndarray]:
    """Shape-function gradients (10, 3) of C3D10 at the 4 Gauss points (weight 1/24)."""
    a, b = 0.5854101966249685, 0.1381966011250105
    dL = np.array([[-1, -1, -1], [1, 0, 0], [0, 1, 0], [0, 0, 1]], float)
    out = []
    for gp in ([a, b, b], [b, a, b], [b, b, a], [b, b, b]):
        L = np.array([1 - sum(gp)] + gp)
        dN = [(4 * L[i] - 1) * dL[i] for i in range(4)]
        for i, j in MIDSIDE["C3D10"]:
            dN.append(4 * (L[i] * dL[j] + L[j] * dL[i]))
        out.append(np.array(dN))
    return out


_TET10_DN = _tet10_dN()


def _wedge15_N(r: float, s: float, t: float) -> np.ndarray:
    """C3D15 shape functions (CalculiX order) at triangle coordinates (r, s), t in [-1, 1]."""
    L = (1 - r - s, r, s)
    N = [0.5 * L[i] * (1 - t) * (2 * L[i] - t - 2) for i in range(3)]
    N += [0.5 * L[i] * (1 + t) * (2 * L[i] + t - 2) for i in range(3)]
    N += [2 * L[i] * L[j] * (1 - t) for i, j in ((0, 1), (1, 2), (2, 0))]
    N += [2 * L[i] * L[j] * (1 + t) for i, j in ((0, 1), (1, 2), (2, 0))]
    N += [L[i] * (1 - t * t) for i in range(3)]
    return np.array(N)


def _wedge15_dN() -> list[tuple[float, np.ndarray]]:
    """(weight, dN (15, 3)) at a 7-point triangle x 3-point Gauss rule; gradients by central
    differences, exact to round-off for these low-order polynomials."""
    a1, b1 = 0.059715871789770, 0.470142064105115      # Dunavant 7-point, degree 5
    a2, b2 = 0.797426985353087, 0.101286507323456
    tri = [((1 / 3, 1 / 3), 0.225 / 2)] + \
          [((p, q), 0.132394152788506 / 2) for p, q in ((b1, b1), (a1, b1), (b1, a1))] + \
          [((p, q), 0.125939180544827 / 2) for p, q in ((b2, b2), (a2, b2), (b2, a2))]
    g = np.sqrt(0.6)
    line = [(-g, 5 / 9), (0.0, 8 / 9), (g, 5 / 9)]
    h = 1e-6
    out = []
    for (r, s), wt in tri:
        for t, wl in line:
            dN = np.stack([(_wedge15_N(r + h, s, t) - _wedge15_N(r - h, s, t)) / (2 * h),
                           (_wedge15_N(r, s + h, t) - _wedge15_N(r, s - h, t)) / (2 * h),
                           (_wedge15_N(r, s, t + h) - _wedge15_N(r, s, t - h)) / (2 * h)], axis=1)
            out.append((wt * wl, dN))
    return out


_WEDGE15_DN = _wedge15_dN()


# Share of a uniform body force taken by each node (CalculiX order), sum = 1: the integral
# of each shape function over the element divided by its volume, exact for straight-sided
# elements. Values from Gauss integration of the serendipity shape functions (computed
# once in development; tests/test_elements.py re-derives the tet10 and hex20 values).
_BODY_WEIGHTS = {
    "C3D10": np.array([-1 / 20] * 4 + [1 / 5] * 6),
    "C3D15": np.array([-1 / 9] * 6 + [1 / 6] * 6 + [2 / 9] * 3),
    "C3D20R": np.array([-1 / 8] * 8 + [1 / 6] * 12),
}


def body_load_weights(etype: str) -> np.ndarray:
    return _BODY_WEIGHTS[etype]


def inverted_count(etype: str, P: np.ndarray) -> int:
    """Elements with a non-positive Jacobian at any Gauss point (C3D10 only so far;
    swept elements are built straight-sided and checked by corner sign)."""
    if etype != "C3D10":
        return int(np.sum(corner_volume_sign(etype, P) <= 0))
    bad = np.zeros(len(P), dtype=bool)
    for dN in _TET10_DN:
        bad |= np.linalg.det(np.einsum("nai,aj->nij", P, dN)) <= 0
    return int(bad.sum())


def element_volumes(etype: str, P: np.ndarray) -> np.ndarray:
    """Element volumes. C3D10: exact isoparametric (curved edges included, as the
    solver integrates them); C3D15/C3D20R: from corners (straight-sided)."""
    # J[n] = P[n]' dN as one batched matrix product (an unoptimised einsum here was 38 % of writing the deck)
    Pt = np.ascontiguousarray(np.swapaxes(P, 1, 2))                  # (n, 3, nodes)
    if etype == "C3D10":
        vol = np.zeros(len(P))
        for dN in _TET10_DN:
            vol += np.linalg.det(Pt @ dN) / 24.0
        return np.abs(vol)
    if etype == "C3D15":
        # isoparametric, as the solver integrates it: skin-swept wedges on a curved wall
        # have curved faces, and a corner-only volume misses their bulge
        vol = np.zeros(len(P))
        for w, dN in _WEDGE15_DN:
            vol += w * np.linalg.det(Pt @ dN)
        return np.abs(vol)
    a = np.linalg.norm(P[:, 1] - P[:, 0], axis=1)
    b = np.linalg.norm(P[:, 3] - P[:, 0], axis=1)
    c = np.linalg.norm(P[:, 4] - P[:, 0], axis=1)
    return a * b * c
