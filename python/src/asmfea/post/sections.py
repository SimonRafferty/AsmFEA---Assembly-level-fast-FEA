"""Stress classification for static checks (DECISIONS D34).

1. Membrane plus bending of member stresses (ASME VIII Div. 2 Part 5 stress classification): at a
   highly stressed surface point, a stress classification line (SCL) runs into the material along
   the inward surface normal to the far surface. The stress tensor along it is linearised: the
   membrane part is its average, the bending part its linear part; the rest (the peak) is a notch
   effect that matters for fatigue, not static strength. At a notch in a plate's outline the normal
   points across the plate, so the SCL crosses the ligament; on a plate's face it crosses the
   thickness. The equivalent (von Mises) stress of membrane plus bending at the surface is what the
   static check uses.

2. The receiving wall of a bonded joint (Dong / Battelle structural stress, as post/joints.py for the
   member's own section): the receiving part's elements under the bonded area form a free body; the
   faces between them and the rest of the wall are a section through the wall round the joint (it
   follows element faces, as Dong's method requires); the forces across it are the free body's
   element internal forces (Ke ue) at the section's nodes. They are turned back into a traction field
   and read over windows along the section exactly as on the member's side.
"""
from __future__ import annotations

import numpy as np

from .. import defaults
from ..mesh.elements import FACE_NODES, FACES, MIDSIDE, N_CORNER, _TET10_DN, _WEDGE15_DN

# ------------------------------------------------------------------ element integration rules


def _hex20_rule():
    """(weight, dN (20, 3)) at the 2 x 2 x 2 points of C3D20R (reduced integration, as CalculiX)."""
    corners = np.array([[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
                        [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], float)
    pts = np.vstack([corners, [(corners[a] + corners[b]) / 2 for a, b in MIDSIDE["C3D20R"]]])

    def N(x):
        out = np.zeros(20)
        for i, p in enumerate(pts):
            if i < 8:
                out[i] = 0.125 * np.prod(1 + x * p) * (x @ p - 2)
            else:
                k = int(np.nonzero(p == 0)[0][0])
                f = 0.25 * (1 - x[k] ** 2)
                for j in range(3):
                    if j != k:
                        f *= 1 + x[j] * p[j]
                out[i] = f
        return out
    g, h = 1 / np.sqrt(3), 1e-6
    rule = []
    for a in (-g, g):
        for b in (-g, g):
            for c in (-g, g):
                x = np.array([a, b, c])
                dN = np.stack([(N(x + h * e) - N(x - h * e)) / (2 * h) for e in np.eye(3)], axis=1)
                rule.append((1.0, dN))
    return rule


RULES = {"C3D10": [(1 / 24, dN) for dN in _TET10_DN], "C3D15": _WEDGE15_DN, "C3D20R": _hex20_rule()}


def element_forces(etype: str, X: np.ndarray, U: np.ndarray, E: float, nu: float) -> np.ndarray:
    """Internal nodal forces f = Ke ue (n_el, n, 3) of linear elastic elements: nodes X, displacements U
    (n_el, n, 3), assembly frame."""
    lam, mu = E * nu / ((1 + nu) * (1 - 2 * nu)), E / (2 * (1 + nu))
    f = np.zeros_like(X, dtype=float)
    for w, dN in RULES[etype]:
        J = np.einsum("ai,eaj->eij", dN, X)              # J_ij = d x_j / d xi_i
        det = np.linalg.det(J)
        inv = np.linalg.inv(J)
        dNdx = np.einsum("ai,eji->eaj", dN, inv)         # d N_a / d x_j
        G = np.einsum("eai,eaj->eij", U, dNdx)           # d u_i / d x_j
        eps = 0.5 * (G + np.transpose(G, (0, 2, 1)))
        S = lam * np.trace(eps, axis1=1, axis2=2)[:, None, None] * np.eye(3) + 2 * mu * eps
        f += (w * np.abs(det))[:, None, None] * np.einsum("eaj,eji->eai", dNdx, S)
    return f


# ------------------------------------------------------------------ part topology


class PartTopology:
    """Elements of one part instance: per element type and conn, their centroids, and the face map."""

    def __init__(self, inst):
        self.inst = inst
        self.elems = []                                  # (etype, local conn) per element
        for etype, conn in inst.mesh.blocks:
            for row in conn:
                self.elems.append((etype, np.asarray(row, dtype=np.int64)))
        self.centroid = np.array([inst.X[c[:N_CORNER[e]] - 1].mean(axis=0) for e, c in self.elems])
        self.faces: dict = {}                            # corner set -> [(element, face number)]
        for k, (e, c) in enumerate(self.elems):
            for fn, corners in enumerate(FACES[e]):
                self.faces.setdefault(frozenset(int(c[i]) for i in corners), []).append((k, fn))

    def element_size(self) -> float:
        """Median corner-to-corner extent of the part's elements."""
        if not hasattr(self, "_h"):
            sample = self.elems[:: max(1, len(self.elems) // 500)]
            self._h = float(np.median([np.ptp(self.inst.X[c[:N_CORNER[e]] - 1], axis=0).max() for e, c in sample]))
        return self._h

    def exterior(self):
        """Exterior facets: (element, face number) of faces owned by one element."""
        return [own[0] for own in self.faces.values() if len(own) == 1]


# ------------------------------------------------------------------ the receiving wall


def receiving_walls(instances, model, deck_info: dict, disp: dict, thickness: dict, topo: dict) -> list[dict]:
    """Battelle structural stress in the wall each section tie's member is bonded onto, for one load
    case. disp: {global node: (3,)}; thickness: {part: wall thickness or None}; topo: a cache
    {part: PartTopology}."""
    from scipy.spatial import cKDTree
    from .joints import _section, _traction_points
    by = {i.part_id: i for i in instances}
    coords = _coords_table(instances)
    out = []
    # every tie's bonded points on each receiving part: a free body must not swallow a neighbouring joint
    bonded: dict = {}
    for t in deck_info.get("ties", []):
        if t.get("facets"):
            bonded.setdefault(t["master_part"], []).append(
                (t["name"], coords[np.array(sorted({g for f in t["facets"] for g in f}), dtype=np.int64)]))
    for t in deck_info.get("ties", []):
        if not t.get("section") or not t.get("facets") or t["master_part"] not in by:
            continue
        M = by[t["master_part"]]
        tp = topo.setdefault(M.part_id, PartTopology(M))
        mat = model.material(M.material)
        slave = np.array(sorted({g for f in t["facets"] for g in f}), dtype=np.int64)
        S = coords[slave]
        h = _facet_size(t["facets"], coords)
        hw = tp.element_size()                    # the WALL's elements set how far the cut lies from the toe
        tw = thickness.get(M.part_id) or hw
        # the free body: the receiving part's elements under the bonded area, the whole wall deep, and
        # nearer this joint than any other joint on the part
        d, _ = cKDTree(S).query(tp.centroid, k=1)
        inside = d <= tw + 0.5 * hw
        for name, other in bonded.get(M.part_id, []):
            if name != t["name"]:
                d_o, _ = cKDTree(other).query(tp.centroid, k=1)
                inside &= d < d_o
        if not inside.any():
            continue
        cut, nodes, signs = [], set(), []
        for key, owners in tp.faces.items():
            ins = [o for o in owners if inside[o[0]]]
            if len(owners) == 2 and len(ins) == 1:
                k, fn = ins[0]
                e, c = tp.elems[k]
                g = [int(M.node0 + c[i]) for i in FACE_NODES[e][fn]]
                cut.append(g)
                nodes.update(g)
                # orient the facet's normal out of the free body
                P3 = coords[np.asarray(g[:3])]
                nf = np.cross(P3[1] - P3[0], P3[2] - P3[0])
                signs.append(1.0 if np.dot(nf, P3.mean(axis=0) - tp.centroid[k]) >= 0 else -1.0)
        if len(cut) < 2:
            continue
        # the forces across the section: the free body's element forces at the section's nodes
        F_tab: dict = {}
        ks = np.nonzero(inside)[0]
        for etype in {tp.elems[k][0] for k in ks}:
            sel = [k for k in ks if tp.elems[k][0] == etype]
            C = np.array([tp.elems[k][1] for k in sel])
            X = M.X[C - 1]
            U = np.array([[disp.get(int(M.node0 + n), np.zeros(3)) for n in row] for row in C])
            fe = element_forces(etype, X, U, mat.E, mat.nu)
            for row, fr in zip(C, fe):
                for n, v in zip(row, fr):
                    g = int(M.node0 + n)
                    if g in nodes:
                        F_tab[g] = F_tab.get(g, np.zeros(3)) + v
        if not thickness.get(M.part_id):
            continue                              # a solid has no wall to assess this way
        pts = _traction_points(cut, F_tab, coords, signs=signs, want_normals=True)
        if pts is None:
            continue
        P, Tg, Wg, Ng = pts
        # the cut follows element faces up to an element from the toe; no load acts in between, so the
        # forces across it are carried to the toe line exactly by taking their moment about the toe
        rec = _section(P, Tg, Wg, thickness.get(M.part_id), h, planar=False, normals=Ng, toe=S)
        if rec is None:
            continue
        rec.update(tie=t["name"], connection=t["connection"], part=M.part_id, other=t["slave_part"], side="receiving",
                   fos=mat.yield_ / rec["structural_stress"] if rec["structural_stress"] > 0 else None)
        out.append(rec)
    return out


def _coords_table(instances) -> np.ndarray:
    n = max(int(i.node0 + i.X.shape[0]) for i in instances) + 1
    tab = np.full((n, 3), np.nan)
    for i in instances:
        tab[i.node0 + 1: i.node0 + 1 + i.X.shape[0]] = i.X
    return tab


def _facet_size(facets, coords) -> float:
    d = []
    for f in facets:
        C = coords[np.asarray(f[:3 if len(f) == 6 else 4])]
        d.append(float(np.max(np.linalg.norm(C[:, None] - C[None], axis=2))))
    return float(np.median(d)) if d else 1.0


# ------------------------------------------------------------------ membrane plus bending (SCL)


def membrane_bending(inst, tp: "PartTopology", vm_nodes: np.ndarray, stress, exclude: set,
                     n_candidates: int | None = None, thin: float | None = None) -> dict | None:
    """The largest von Mises stress of membrane plus bending over this part's most stressed surface
    nodes (outside ``exclude``, the joint band). stress: {global node: (6,) sxx syy szz sxy syz szx}, or the
    same as an array indexed by global node id;
    vm_nodes: (global id, von Mises) of the part's nodes.

    thin: the wall thickness of a sheet or member. Its stress through the wall is linear, so a line
    through the wall reports a notch's in-plane concentration as membrane: there the line runs across the
    ligament from the edge (side faces: the outline, holes, notches), face points away from the edges
    keep the line through the wall (plate bending and membrane), and face points near an edge are left
    to the edge's line."""
    from scipy.spatial import cKDTree
    n_candidates = n_candidates or defaults.SCL_CANDIDATES
    ext = tp.exterior()
    # outward unit normal of each exterior facet, and the facets as triangles (for the ray casts)
    C, N, fnodes_of, tri = _exterior_facets(inst, tp, ext)
    nrm_node: dict = {}
    for fnodes, n in zip(fnodes_of, N):           # facet order kept: it decides which near-equal direction wins
        for a in fnodes:
            nrm_node.setdefault(a, []).append(n)
    rays = _Triangles(tri)
    h = float(np.median([np.linalg.norm(t[1] - t[0]) for t in tri[:: max(1, len(tri) // 2000)]]))
    side_dirs: dict = {}                          # thin parts: the side-face normals at each edge node
    skip: set = set()
    if thin:
        # A skin face has a face on the opposite skin a wall thickness away, facing the other way; a side face
        # has none. One spatial lookup for all faces: a ray per face against every face took 55 minutes on a
        # 106 000-face column (verification C4, found).
        d, j = cKDTree(C).query(C - N * thin, k=1)
        skin = (d <= max(0.75 * thin, 0.75 * h)) & ((N * N[j]).sum(axis=1) < -0.5)
        side_nodes = set()
        for fnodes, n, is_skin in zip(fnodes_of, N, skin):
            if not is_skin:
                side_nodes.update(fnodes)
                for a in fnodes:
                    side_dirs.setdefault(a, []).append(n)
        if side_nodes:
            sn_ids = np.array(sorted(side_nodes), dtype=np.int64)
            d, _ = cKDTree(inst.X[sn_ids - 1]).query(inst.X[np.array(sorted(nrm_node), dtype=np.int64) - 1], k=1)
            for a, da in zip(sorted(nrm_node), d):
                if a not in side_nodes and da < defaults.SCL_EDGE_T * thin:
                    skip.add(a)
    local = {int(g - inst.node0): v for g, v in vm_nodes}
    cands = [a for a in sorted(nrm_node, key=lambda a: -local.get(a, 0.0))
             if int(inst.node0 + a) not in exclude and a in local and a not in skip][:n_candidates]
    if not cands:
        return None
    ids = np.array(sorted(local), dtype=np.int64)
    Xn = inst.X[ids - 1]
    tree = cKDTree(Xn)
    if isinstance(stress, np.ndarray):            # a table indexed by global node id
        S_nodes = stress[inst.node0 + ids]
    else:
        S_nodes = np.array([stress[int(inst.node0 + a)] for a in ids])
    reach = 1.5 * (thin or h)                     # where the first ray search looks (any value is exact)
    worst = None
    for a in cands:
        p0 = inst.X[a - 1]
        # one SCL per distinct face direction at the point; the longest is the section that carries the
        # load (across the ligament at a notch in a plate's outline, through the wall on a face)
        dirs = []
        for v in (side_dirs.get(a) or nrm_node[a]):    # an edge node of a thin part: across the plate only
            if not any(np.dot(v, u) > 0.95 for u in dirs):
                dirs.append(v)
        best = None
        for n in dirs:
            Ln = rays.exit(p0 - 1e-6 * n, -n, reach)
            if Ln is not None and Ln > 0 and (best is None or Ln > best[0]):
                best = (Ln, n)
        if best is None:
            continue
        L, n = best
        if thin and a in side_dirs:               # across the ligament: no further than a notch's reach
            L = min(L, defaults.SCL_LIGAMENT_T * thin)
        s = np.linspace(0.0, L, defaults.SCL_POINTS)
        Ssc = _scl_profile(p0, n, L, s, Xn, S_nodes, tree)
        sm = np.trapezoid(Ssc, s, axis=0) / L
        sb = 6.0 / L ** 2 * np.trapezoid(Ssc * (L / 2 - s)[:, None], s, axis=0)
        v = _vm(sm + sb)
        if worst is None or v > worst["membrane_bending"]:
            worst = {"membrane_bending": float(v), "membrane": float(_vm(sm)), "at": p0.round(3).tolist(),
                     "scl_length": float(L), "peak": float(local[a])}
    return worst


def _scl_profile(p0, n, L, s, Xn, S_nodes, tree) -> np.ndarray:
    """Stress (len(s), 6) along the SCL from p0 in direction -n: from the nodes lying close to the line
    (within a few tenths of its length), interpolated along it. Nearest-node averaging reached sideways
    in a thin wall and pulled in a joint's high stresses (found on the wall case); on a swept wall the
    nodes near a through-thickness line are its own column."""
    r_lat = max(defaults.SCL_LATERAL * L, 1e-6)
    # only the nodes within reach of the line (a ball round its middle): every node of a 680 000-node part
    # took a quarter of a second per line
    reach = np.asarray(tree.query_ball_point(p0 - 0.5 * L * n, np.hypot(0.55 * L, r_lat) * 1.001), dtype=np.int64)
    Xn, S_all, S_nodes = Xn[reach], S_nodes, S_nodes[reach]
    v = Xn - p0
    sn = -(v @ n)
    lat = np.linalg.norm(v + np.outer(sn, n), axis=1)
    near = (sn >= -0.05 * L) & (sn <= 1.05 * L) & (lat <= r_lat)
    if near.sum() >= 2 and np.ptp(sn[near]) > 0.5 * L:
        order = np.argsort(sn[near])
        xs, ys = sn[near][order], S_nodes[near][order]
        # nodes at (nearly) the same depth are averaged, nearer the line weighing more
        key = np.round(xs / (L / 50.0)).astype(int)
        w = 1.0 / (lat[near][order] + 0.05 * r_lat)
        uk, grp = np.unique(key, return_inverse=True)
        wsum = np.bincount(grp, w)                # weighted mean per depth, all depths at once
        xm = np.bincount(grp, w * xs) / wsum
        ym = np.column_stack([np.bincount(grp, w * ys[:, c]) for c in range(ys.shape[1])]) / wsum[:, None]
        if len(uk) < defaults.SCL_INTERP_DEPTHS:
            # few depths (a thin wall a quadratic element or two deep): a straight or quadratic fit; piecewise
            # interpolation between borrowed off-line nodes made an S-shaped profile that inflated bending
            deg = 1 if len(uk) < 3 else 2
            coef = np.polyfit(xm, ym, deg)
            return np.column_stack([np.polyval(coef[:, c], s) for c in range(6)])
        return np.column_stack([np.interp(s, xm, ym[:, c]) for c in range(6)])
    pts = p0[None] - np.outer(s, n)               # too few nodes near the line: nearest-node averaging
    dist, nb = tree.query(pts, k=4)
    w = 1.0 / np.maximum(dist, 1e-9) ** 2
    return (w[:, :, None] * S_all[nb]).sum(axis=1) / w.sum(axis=1)[:, None]


def estimate_wall_thickness(inst, tp: "PartTopology") -> float | None:
    """A part's wall thickness from its mesh: the median distance through the part from its largest outer
    faces (the skins of a sheet or a tube). None when that is not thin compared with the part (a solid)."""
    ext = tp.exterior()
    tri, cand = [], []
    for k, fn in ext:
        e, c = tp.elems[k]
        corners = [int(c[i]) for i in FACES[e][fn]]
        P = inst.X[np.asarray(corners) - 1]
        n = np.cross(P[1] - P[0], P[2] - P[0])
        if np.dot(n, P.mean(axis=0) - tp.centroid[k]) < 0:
            n = -n
        a = np.linalg.norm(n) * (0.5 if len(corners) == 3 else 1.0)
        cand.append((a, P.mean(axis=0), n / max(np.linalg.norm(n), 1e-30)))
        tri.append(P[[0, 1, 2]])
        if len(corners) == 4:
            tri.append(P[[0, 2, 3]])
    if not cand:
        return None
    tri = np.asarray(tri)
    cand.sort(key=lambda x: -x[0])
    L = [_ray_exit(c - 1e-6 * n, -n, tri) for _, c, n in cand[:defaults.WALL_SAMPLE_FACETS]]
    L = np.array([x for x in L if x])
    if len(L) == 0:
        return None
    t = float(np.median(L))
    dims = np.sort(np.ptp(inst.X, axis=0))
    return t if t < defaults.WALL_THIN_FRACTION * dims[1] else None


def _vm(s):
    sx, sy, sz, txy, tyz, tzx = s
    return np.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2) + 3 * (txy ** 2 + tyz ** 2 + tzx ** 2))


def _exterior_facets(inst, tp: "PartTopology", ext: list):
    """The exterior facets' centres (F, 3), outward unit normals (F, 3) and node lists (local ids), in the
    order of ``ext``, and their triangles (T, 3, 3) (a quad as two, in facet order). Vectorised per element
    type and face number: a small cross product per facet was most of a part's classification time."""
    F = len(ext)
    C, N = np.zeros((F, 3)), np.zeros((F, 3))
    fnodes_of: list = [None] * F
    tri_of: list = [None] * F
    groups: dict = {}
    for pos, (k, fn) in enumerate(ext):
        groups.setdefault((tp.elems[k][0], fn), []).append(pos)
    for (e, fn), pos in groups.items():
        ks = np.array([ext[p][0] for p in pos])
        conn = np.array([tp.elems[k][1] for k in ks])
        P = inst.X[conn[:, FACES[e][fn]] - 1]                    # (m, corners, 3)
        n = np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0])
        cen = P.mean(axis=1)
        n[(n * (cen - tp.centroid[ks])).sum(axis=1) < 0] *= -1.0
        n /= np.maximum(np.linalg.norm(n, axis=1), 1e-30)[:, None]
        C[pos], N[pos] = cen, n
        fn_nodes = conn[:, FACE_NODES[e][fn]]
        quad = P.shape[1] == 4
        for i, p in enumerate(pos):
            fnodes_of[p] = [int(a) for a in fn_nodes[i]]
            tri_of[p] = (P[i, [0, 1, 2]], P[i, [0, 2, 3]]) if quad else (P[i, [0, 1, 2]],)
    tri = np.array([t for ts in tri_of for t in ts]).reshape(-1, 3, 3)
    return C, N, fnodes_of, tri


class _Triangles:
    """A part's surface triangles prepared for many ray casts: the edges are computed once, and a ray first
    tests only the triangles near its origin. That is exact: a triangle hit at distance t <= R has its
    centre within R + (its largest corner distance) of the origin, so if the nearest hit among those is
    within R, no other triangle can be nearer; otherwise R grows, ending with every triangle."""

    def __init__(self, tri: np.ndarray):
        from scipy.spatial import cKDTree
        self.v0 = tri[:, 0]
        self.e1, self.e2 = tri[:, 1] - self.v0, tri[:, 2] - self.v0
        centre = tri.mean(axis=1)
        self.rmax = float(np.linalg.norm(tri - centre[:, None], axis=2).max()) if len(tri) else 0.0
        self.tree = cKDTree(centre) if len(tri) else None

    def exit(self, o: np.ndarray, d: np.ndarray, reach: float) -> float | None:
        """Distance along unit d from o to the first triangle hit, as _ray_exit."""
        n = len(self.v0)
        R = max(float(reach), 1e-9)
        while self.tree is not None:
            idx = self.tree.query_ball_point(o, R + self.rmax)
            if len(idx) > n // 2:
                break                                            # most of the part: test it all
            idx = np.asarray(idx, dtype=np.int64)
            t = _ray_hits(o, d, self.v0[idx], self.e1[idx], self.e2[idx])
            if t is not None and t <= R:
                return t
            R *= 4.0
        return _ray_hits(o, d, self.v0, self.e1, self.e2)


def _ray_exit(o: np.ndarray, d: np.ndarray, tri: np.ndarray) -> float | None:
    """Distance along unit d from o to the first triangle hit (Moller-Trumbore, all triangles at once)."""
    v0 = tri[:, 0]
    return _ray_hits(o, d, v0, tri[:, 1] - v0, tri[:, 2] - v0)


def _cross(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Row-wise cross product of (n, 3) arrays: np.cross's own formula without its axis handling, which
    cost more than the arithmetic on the small arrays of a ray cast."""
    out = np.empty(b.shape)
    out[:, 0] = a[:, 1] * b[:, 2] - a[:, 2] * b[:, 1]
    out[:, 1] = a[:, 2] * b[:, 0] - a[:, 0] * b[:, 2]
    out[:, 2] = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    return out


def _ray_hits(o, d, v0, e1, e2) -> float | None:
    if len(v0) == 0:
        return None
    p = _cross(np.broadcast_to(d, e2.shape), e2)
    det = (e1 * p).sum(axis=1)
    ok = np.abs(det) > 1e-12
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    tv = o - v0
    u = (tv * p).sum(axis=1) * inv
    q = _cross(tv, e1)
    v = (q * d).sum(axis=1) * inv
    t = (q * e2).sum(axis=1) * inv
    hit = ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t > 1e-6)
    return float(t[hit].min()) if hit.any() else None
