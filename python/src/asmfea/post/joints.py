"""Structural stress at bonded joints from nodal forces (DECISIONS D33).

The peak stress where a member's edge or end is bonded to another part is a singular point: it
grows without limit as the mesh is refined (the weld toe of a fillet weld, modelled sharp). The
mesh-insensitive structural stress of Dong (Battelle, the method behind Verity) reads the joint
from the FORCES it carries instead: the forces at the nodes of the section through the member at
the joint follow from equilibrium, are accurate however singular the stresses are, and give the
line force f and line moment m per unit length along the joint. The structural stress is then the
membrane plus bending stress of a plate strip of thickness t carrying them:

    sigma_s = f / t + 6 m / t^2        (and the shear tau = q / t)

For a tie whose slave face is a section through its member (a plate edge on a face, a member's end
butted against another), the slave face IS that section, and CalculiX's RF at its nodes (printed
by the deck) is the force the joint carries there. Quadratic elements carry it as CONSISTENT nodal
forces: large, self-cancelling corner and midside values that are not a stress pattern. As Dong
converts nodal forces to line forces, the traction field whose consistent nodal forces they are is
recovered (one sparse solve with the face's consistent area matrix) and integrated over windows
along the joint: per window, the resultant force and its moment about the window's mid-thickness
line give f, m and q. The equivalent stress reported is sqrt((|sigma_m| + |sigma_b|)^2 + 3 tau^2),
the larger surface value of the normal stress combined with the shear.
"""
from __future__ import annotations

import numpy as np

from .. import defaults


def joint_stresses(deck_info: dict, nodal: dict, coords: np.ndarray, thickness: dict) -> list[dict]:
    """One record per section tie with forces in this step.

    nodal: {(quantity, set): {node: vec}} of the step (read_dat_steps); coords: (n, 3) table by global
    node id; thickness: {part: wall thickness or None}.
    """
    out = []
    for t in deck_info.get("ties", []):
        if not t.get("section") or not t.get("facets"):
            continue
        F_tab = nodal.get(("forces", f"{t['name']}S".upper())) or nodal.get(("forces", f"{t['name']}S"))
        if not F_tab:
            continue
        pts = _traction_points(t["facets"], F_tab, coords)
        if pts is None:
            continue
        rec = _section(*pts, thickness.get(t["slave_part"]), _element_size(t["facets"], coords))
        if rec is None:
            continue
        rec.update(tie=t["name"], connection=t["connection"], part=t["slave_part"], other=t["master_part"], side="member")
        out.append(rec)
    return out


# ------------------------------------------------------------------ face shape functions (CalculiX order)

def _shape(n: int, xi: float, eta: float) -> tuple[np.ndarray, np.ndarray]:
    """(N, dN/d(xi, eta)) of a 6-node triangle (area coordinates xi, eta) or an 8-node quad
    (xi, eta in [-1, 1]); corners then midsides of edges 1-2, 2-3, 3-(4-)1."""
    if n == 6:
        L1, L2, L3 = 1 - xi - eta, xi, eta
        N = np.array([L1 * (2 * L1 - 1), L2 * (2 * L2 - 1), L3 * (2 * L3 - 1), 4 * L1 * L2, 4 * L2 * L3, 4 * L3 * L1])
        dL = np.array([[-1, -1], [1, 0], [0, 1]], float)
        dN = np.array([(4 * L1 - 1) * dL[0], (4 * L2 - 1) * dL[1], (4 * L3 - 1) * dL[2],
                       4 * (L1 * dL[1] + L2 * dL[0]), 4 * (L2 * dL[2] + L3 * dL[1]), 4 * (L3 * dL[0] + L1 * dL[2])])
        return N, dN
    c = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], float)
    N, dN = np.zeros(8), np.zeros((8, 2))
    for i, (a, b) in enumerate(c):
        N[i] = 0.25 * (1 + a * xi) * (1 + b * eta) * (a * xi + b * eta - 1)
        dN[i] = [0.25 * a * (1 + b * eta) * (2 * a * xi + b * eta), 0.25 * b * (1 + a * xi) * (a * xi + 2 * b * eta)]
    for i, (a, b) in enumerate([(0, -1), (1, 0), (0, 1), (-1, 0)], start=4):
        if a == 0:
            N[i] = 0.5 * (1 - xi ** 2) * (1 + b * eta)
            dN[i] = [-xi * (1 + b * eta), 0.5 * b * (1 - xi ** 2)]
        else:
            N[i] = 0.5 * (1 + a * xi) * (1 - eta ** 2)
            dN[i] = [0.5 * a * (1 - eta ** 2), -eta * (1 + a * xi)]
    return N, dN


def _gauss(n: int):
    """Integration points (xi, eta, weight) exact for the products N_i N_j of the face."""
    if n == 6:   # 6-point rule, degree 4; weights sum to 1 (times the triangle's 1/2 below)
        a, b = 0.445948490915965, 0.091576213509771
        w1, w2 = 0.223381589678011, 0.109951743655322
        return [(a, a, w1), (1 - 2 * a, a, w1), (a, 1 - 2 * a, w1), (b, b, w2), (1 - 2 * b, b, w2), (b, 1 - 2 * b, w2)]
    g, w = np.sqrt(0.6), np.array([5, 8, 5]) / 9.0
    xs = [-g, 0.0, g]
    return [(xs[i], xs[j], w[i] * w[j]) for i in range(3) for j in range(3)]


def _traction_points(facets: list, F_tab: dict, coords: np.ndarray, signs=None, want_normals: bool = False):
    """The traction field whose consistent nodal forces are the printed forces (the surface version
    of Dong's line-force conversion), sampled at the face's integration points: (points, traction
    vectors, area weights)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import spsolve
    nodes = sorted({g for f in facets for g in f})
    idx = {g: i for i, g in enumerate(nodes)}
    # all facets of one kind (6-node triangles, 8-node quads) at once; the integration points keep the
    # facet-by-facet order of the loop this replaced (the windows' tie-break follows it)
    rows, cols, vals = [], [], []
    order, P_l, N_l, loc_l, dA_l, nrm_l = [], [], [], [], [], []
    for n in (6, 8):
        fis = [fi for fi, f in enumerate(facets) if len(f) == n]
        if not fis:
            continue
        loc = np.array([[idx[g] for g in facets[fi]] for fi in fis], dtype=np.int64)      # (m, n)
        X = coords[np.array([facets[fi] for fi in fis], dtype=np.int64)]                  # (m, n, 3)
        sg = np.array([signs[fi] for fi in fis], float) if signs is not None else np.ones(len(fis))
        for gi, (xi, eta, w) in enumerate(_gauss(n)):
            N, dN = _shape(n, xi, eta)
            J = dN.T @ X                                                                   # (m, 2, 3)
            cr = np.cross(J[:, 0], J[:, 1])
            ln = np.linalg.norm(cr, axis=1)
            dA = ln * w * (0.5 if n == 6 else 1.0)
            rows.append(np.repeat(loc, n, axis=1).ravel())
            cols.append(np.tile(loc, (1, n)).ravel())
            vals.append((np.outer(N, N)[None] * dA[:, None, None]).ravel())
            order.append(np.array(fis) * 16 + gi)
            P_l.append(N @ X)
            N_l.append(np.broadcast_to(N, (len(fis), n)))
            loc_l.append(loc)
            dA_l.append(dA)
            nrm_l.append(cr / np.maximum(ln, 1e-30)[:, None] * sg[:, None])
    if not order:
        return None
    M = coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                   shape=(len(nodes), len(nodes))).tocsc()
    F = np.array([F_tab.get(g, np.zeros(3)) for g in nodes], float)
    T = np.column_stack([spsolve(M, F[:, k]) for k in range(3)])     # nodal traction, MPa
    o = np.argsort(np.concatenate(order), kind="stable")
    P = np.concatenate(P_l)[o]
    Tg = np.concatenate([(Nn[:, None, :] @ T[lc])[:, 0] for Nn, lc in zip(N_l, loc_l)])[o]
    W = np.concatenate(dA_l)[o]
    if want_normals:
        return P, Tg, W, np.concatenate(nrm_l)[o]
    return P, Tg, W


def _element_size(facets: list, coords: np.ndarray) -> float:
    """Median corner-to-corner length of the section's facets."""
    d = []
    for f in facets:
        nc = 3 if len(f) == 6 else 4
        C = coords[np.asarray(f[:nc])]
        d.append(float(np.max(np.linalg.norm(C[:, None] - C[None], axis=2))))
    return float(np.median(d)) if d else 0.0


def _section(X: np.ndarray, Tr: np.ndarray, W: np.ndarray, t_wall: float | None, h: float = 0.0,
             planar: bool = True, normals: np.ndarray | None = None, toe: np.ndarray | None = None) -> dict | None:
    """Structural stress over a planar section: integration points X, traction Tr, area weights W.

    Windows centred on each point, radius JOINT_PATCH_T / 2 x t (>= t, so a window spans the whole
    wall): per window, the resultant force and its moment about the window's mid-thickness line,
    over the joint length the window carries (its area / t). The largest equivalent stress is kept.
    """
    from scipy.spatial import cKDTree
    c = X.mean(axis=0)
    _, s, vt = np.linalg.svd(X - c, full_matrices=False)
    n_s = vt[2]                                   # the section's normal (the face is planar)
    if planar and s[2] > 0.05 * max(s[0], 1e-12):
        return None                               # not a planar section
    if not planar and (normals is None or not t_wall):
        return None                               # a cut round a joint: local normals and the wall thickness needed
    t_est = float(t_wall) if t_wall else None
    if t_est is None:                             # a strip: its narrow extent (points sit inside the faces)
        v = (X - c) @ vt[1]
        t_est = float(v.max() - v.min()) * 1.1
    if t_est <= 0:
        return None
    # a window spans the wall, a few thicknesses, and at least two elements along the joint: shorter
    # windows alias with the element-to-element ripple of the recovered traction (found on the hand case)
    R = max(0.5 * defaults.JOINT_PATCH_T * t_est, t_est, defaults.JOINT_WINDOW_ELEMENTS * h)
    tree = cKDTree(X)
    toe_tree = cKDTree(toe) if toe is not None else None
    F = Tr * W[:, None]                           # the force each integration point carries
    worst = None
    balls = tree.query_ball_point(X, R, return_sorted=True)     # every window in one call
    for i in range(len(X)):
        sel = np.asarray(balls[i], dtype=np.int64)
        if len(sel) < 6:
            continue
        P, Fp, Wp = X[sel], F[sel], W[sel]
        if not planar:                            # the cut's local normal in this window
            n_s = (normals[sel] * Wp[:, None]).sum(axis=0)
            if np.linalg.norm(n_s) < 1e-9:
                continue
            n_s = n_s / np.linalg.norm(n_s)
        cp = P.mean(axis=0)
        Q = P - cp
        Q -= np.outer(Q @ n_s, n_s)
        _, sv, wt = np.linalg.svd(Q, full_matrices=False)
        if sv[0] <= 1e-9:
            continue
        e_s = wt[0] - (wt[0] @ n_s) * n_s         # along the joint
        e_s /= np.linalg.norm(e_s)
        if e_s[np.argmax(np.abs(e_s))] < 0:      # the SVD's sign is arbitrary and flipped with rounding: fix it,
            e_s = -e_s                            # so the reported bending's sign is repeatable (|sb| is unaffected)
        e_t = np.cross(n_s, e_s)                  # through the wall
        t = float(t_wall) if t_wall else t_est
        z = Q @ e_t
        r0 = cp + e_t * float((z * Wp).sum() / Wp.sum())   # mid-thickness, area-weighted
        if toe_tree is not None:
            # the toe line: the nearest bonded point, brought to this window's mid-thickness along the cut's
            # normal (the moment of the cut's forces about it is the moment at the toe)
            _, j = toe_tree.query(r0)
            r0 = r0 + float((toe[j] - r0) @ n_s) * n_s
        length = Wp.sum() / t                     # joint length the window carries
        Ft = Fp.sum(axis=0)
        M = np.cross(P - r0, Fp).sum(axis=0)
        f = float(Ft @ n_s) / length              # line force normal to the section
        m = float(M @ e_s) / length               # line moment about the joint line
        q = float(np.linalg.norm(Ft - (Ft @ n_s) * n_s)) / length
        sm, sb, tau = f / t, 6.0 * m / t ** 2, q / t
        seq = float(np.sqrt((abs(sm) + abs(sb)) ** 2 + 3 * tau ** 2))
        if worst is None or seq > worst["structural_stress"]:
            worst = {"structural_stress": seq, "membrane": sm, "bending": sb, "shear": tau,
                     "at": r0.round(3).tolist(), "thickness": t, "window_length": length}
    return worst
