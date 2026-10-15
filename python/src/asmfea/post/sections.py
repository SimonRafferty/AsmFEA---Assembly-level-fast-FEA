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
                     n_candidates: int | None = None, thin: float | None = None, zones: dict | None = None) -> dict | None:
    """The largest von Mises stress of membrane plus bending over this part's most stressed surface
    nodes (outside ``exclude``, the joint band). stress: {global node: (6,) sxx syy szz sxy syz szx}, or the
    same as an array indexed by global node id;
    vm_nodes: (global id, von Mises) of the part's nodes.

    thin: the wall thickness of a sheet or member. Its stress through the wall is linear, so a line
    through the wall reports a notch's in-plane concentration as membrane: there the line runs across the
    ligament from the edge (side faces: the outline, holes, notches), face points away from the edges
    keep the line through the wall (plate bending and membrane), and face points near an edge are left
    to the edge's line.

    zones: {"edge", "line", "inside"} sets of global node ids from pipeline.joint_zones. The joints are assessed too
    (the user's request, 2026-10-10: the stress round a joint matters), in "joint": a thick part by sections through
    the material in its joint bands (a section's average and linear part are finite at a sharp edge); a thin part by
    the hot-spot stress at its joint edges (hot_spots): through a thin wall the stress is linear, so a section there
    returns the edge's own spike, which grows with every mesh (153 MPa on a folding step's plate, its peak 145)."""
    from scipy.spatial import cKDTree
    n_candidates = n_candidates or defaults.SCL_CANDIDATES
    zones = zones or {}
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
    # the bands round loaded faces' edges (D87): the load's idealisation, read nowhere (sections, hot spots)
    loaded = {int(g - inst.node0) for g in (zones.get("load") or ())}
    skip |= loaded
    local = {int(g - inst.node0): v for g, v in vm_nodes}
    ranked = [a for a in sorted(nrm_node, key=lambda a: -local.get(a, 0.0)) if a in local and a not in skip]
    # The most stressed points away from the joints; on a thick part as many again in the joint bands (``exclude``),
    # off the edge's own elements: a section through the material at a joint averages out the sharp edge's spike,
    # which has no finite value, and keeps the membrane and bending the joint really puts in (ASME VIII-2 5.A). The
    # bands' spikes would take every place in one list, so they get their own, spread over the joints: at most
    # SCL_PER_JOINT_CELL points per cell 3 walls wide. A thin part's sections through the wall return the surface
    # stress itself, so there no point nearer the edge line than HOT_SPOT_NEAR_T walls is read on its own (the IIW
    # read-out's nearest point): the edge itself is read by hot_spots.
    cands = [a for a in ranked if int(inst.node0 + a) not in exclude][:n_candidates]
    edge = zones.get("edge") or set()
    in_band = [a for a in ranked if int(inst.node0 + a) in exclude and int(inst.node0 + a) not in edge]
    line_l = np.array(sorted(g - inst.node0 for g in (zones.get("line") or ())), dtype=np.int64)
    if thin and in_band and len(line_l):
        r, _ = cKDTree(inst.X[line_l - 1]).query(inst.X[np.asarray(in_band) - 1], k=1)
        in_band = [a for a, ra in zip(in_band, r) if ra >= defaults.HOT_SPOT_NEAR_T * thin]
    if in_band:
        cell = 2.0 * max(thin or 0.0, h) * defaults.JOINT_BAND_T
        keys = np.floor(inst.X[np.asarray(in_band) - 1] / cell).astype(np.int64)
        seen: dict = {}
        for a, k in zip(in_band, map(tuple, keys)):
            if seen.get(k, 0) < defaults.SCL_PER_JOINT_CELL:
                seen[k] = seen.get(k, 0) + 1
                cands.append(a)
                if len(cands) >= 2 * n_candidates:
                    break
    hot = None
    if thin and zones.get("line"):
        hot = hot_spots(inst, nrm_node, local, stress, thin, zones["line"],
                        (zones.get("inside") or set()) | (zones.get("load") or set()), _joint_element_size(inst, tri, zones["line"], thin, h))
    if not cands and hot is None:
        return None
    ids = np.array(sorted(local), dtype=np.int64)
    Xn = inst.X[ids - 1]
    tree = cKDTree(Xn)
    if isinstance(stress, np.ndarray):            # a table indexed by global node id
        S_nodes = stress[inst.node0 + ids]
    else:
        S_nodes = np.array([stress[int(inst.node0 + a)] for a in ids])
    reach = 1.5 * (thin or h)                     # where the first ray search looks (any value is exact)
    worst = joint = None
    band_set = set(in_band)
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
        if a in band_set and (joint is None or v > joint["membrane_bending"]):
            joint = {"membrane_bending": float(v), "at": p0.round(3).tolist()}
    if hot is not None and (joint is None or hot["membrane_bending"] > joint["membrane_bending"]):
        joint = hot
    if joint is not None and (worst is None or joint["membrane_bending"] > worst["membrane_bending"]):
        # a joint governs the part: its stress is the part's (a hot spot has no separate membrane)
        worst = {"membrane_bending": joint["membrane_bending"], "membrane": joint.get("membrane", 0.0),
                 "at": joint["at"], "scl_length": 0.0, "peak": 0.0}
    if worst is not None:
        worst["joint"] = joint            # the worst at the joints (None: no joint, or none read there)
    return worst


def _joint_element_size(inst, tri: np.ndarray, line: set, t: float, h_part: float) -> float:
    """The median surface element edge near a part's joint edges (triangle centres within 3 x HOT_SPOT_FAR_T wall
    thicknesses of the edge line), which sets how far the hot-spot read-out reaches; the part's own median when too
    few lie there. The part's median followed its longest elements: a member's long span layers (D92) widened the
    read-out at its joints, whose mesh had not changed (D95)."""
    from scipy.spatial import cKDTree
    ids = np.array(sorted(a - inst.node0 for a in line if 0 < a - inst.node0 <= len(inst.X)), dtype=np.int64)
    if len(ids) == 0 or len(tri) == 0:
        return h_part
    r, _ = cKDTree(inst.X[ids - 1]).query(tri.mean(axis=1), k=1)
    near = tri[r <= 3.0 * defaults.HOT_SPOT_FAR_T * t]
    if len(near) < 10:
        return h_part
    return float(np.median(np.linalg.norm(near[:, 1] - near[:, 0], axis=1)))


def hot_spots(inst, nrm_node: dict, local: dict, stress, t: float, line: set, inside: set,
              h: float = 0.0) -> dict | None:
    """The worst stress beside a thin part's joint edges, read as the IIW structural hot-spot stress is (type "a", the
    coarse-mesh read-out for elements about a wall thick: Hobbacher, IIW recommendations, 2016, 4.2.3): the surface
    stress read between HOT_SPOT_NEAR_T and HOT_SPOT_FAR_T wall thicknesses from the edge and extrapolated in a
    straight line back to it. The edge's own spike (the notch, and the singular point a sharp modelled edge makes)
    is left out; the bending and membrane the joint puts into the wall stay, as in a weld's hot-spot stress.

    The read-out is direction-free: at each edge point (one per HOT_SPOT_SPACING_T of edge), the surface nodes on the
    same face (normals within 60 degrees), off the joint's own face (``inside``), within a wall thickness sideways and
    HOT_SPOT_NEAR_T..HOT_SPOT_FAR_T from the edge line give a straight-line fit of each stress component against
    their distance from the line; its value at the line is the hot-spot stress tensor, reported as von Mises.
    Elements longer than the wall (h, the median element edge) widen the read-out to at least two elements beyond
    its nearest point, as IIW's coarse read-out at the first two elements' midpoints does: a 7.5 mm mesh on a 5 mm
    wall left most of a joint edge with too few points in 0.4-1.6 t.
    The static value is the fit's stress at the read-out's nearest point (HOT_SPOT_NEAR_T from the edge, D83); the
    value extrapolated to the edge (the hot spot proper, a fatigue measure) is returned beside it.
    line, inside: global node ids. Returns {"membrane_bending": von Mises, "at", "method", "hot_spot"} or None."""
    from scipy.spatial import cKDTree
    n0 = inst.node0
    line_l = np.array(sorted(a - n0 for a in line if (a - n0) in nrm_node), dtype=np.int64)
    if len(line_l) == 0:
        return None
    skip = {a - n0 for a in inside} | set(line_l.tolist())
    surf = np.array([a for a in sorted(nrm_node) if a not in skip and a in local], dtype=np.int64)
    if len(surf) < 3:
        return None

    def unit_normal(a):
        v = np.mean(nrm_node[a], axis=0)
        return v / max(np.linalg.norm(v), 1e-30)
    XL, XS = inst.X[line_l - 1], inst.X[surf - 1]
    r, _ = cKDTree(XL).query(XS, k=1)                       # each surface node's distance from the edge line
    lo = defaults.HOT_SPOT_NEAR_T * t
    hi = max(defaults.HOT_SPOT_FAR_T * t, lo + 2.0 * h)
    wide = max(t, h)                                         # how far sideways along the edge a read-out reaches
    win = (r >= lo) & (r <= hi)
    if win.sum() < 3:
        return None
    surf, XS, r = surf[win], XS[win], r[win]
    NS = np.array([unit_normal(a) for a in surf])
    if isinstance(stress, np.ndarray):
        S = stress[n0 + surf]
    else:
        S = np.array([stress[int(n0 + a)] for a in surf])
    tree = cKDTree(XS)
    # one read-out per HOT_SPOT_SPACING_T of edge: the first edge node in each cell of that size
    cell = max(defaults.HOT_SPOT_SPACING_T * t, 1e-6)
    _, first = np.unique(np.floor(XL / cell).astype(np.int64), axis=0, return_index=True)
    best = None
    for i in first:
        p, nL = XL[i], unit_normal(int(line_l[i]))
        near = np.asarray(tree.query_ball_point(p, float(np.hypot(hi, wide))), dtype=np.int64)
        if len(near) < 3:
            continue
        d = np.linalg.norm(XS[near] - p, axis=1)
        side = np.sqrt(np.maximum(d ** 2 - r[near] ** 2, 0.0))
        keep = near[(NS[near] @ nL > 0.5) & (side <= wide)]
        if len(keep) < 3 or np.ptp(r[keep]) < 0.3 * (hi - lo):
            continue
        A = np.column_stack([np.ones(len(keep)), r[keep]])
        coef, *_ = np.linalg.lstsq(A, S[keep], rcond=None)   # (2, 6): the value at the line, the slope
        # the static assessment (D83): the fitted stress at the read-out's nearest point (HOT_SPOT_NEAR_T from the
        # edge), not extrapolated into it. The extrapolated hot spot is a weld fatigue measure (IIW): taken as the
        # static stress it read above any stress in the part (181 MPa where the part's highest was 158, the
        # a pinned cradle frame). It is kept beside it for a fatigue check.
        v = _vm(coef[0] + coef[1] * lo)
        if best is None or v > best["membrane_bending"]:
            best = {"membrane_bending": float(v), "at": p.round(3).tolist(), "method": "beside the joint",
                    "hot_spot": float(_vm(coef[0])), "points": int(len(keep))}
    return best


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
    if not ext:
        return None
    # each exterior facet's area (a quad's from its first three corners, as a parallelogram), centre and
    # outward unit normal, vectorised per element type and face number (a small cross product per facet was
    # most of a deck's time on a 174-part frame); the triangles in facet order, a quad as two
    F = len(ext)
    A, C, N = np.zeros(F), np.zeros((F, 3)), np.zeros((F, 3))
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
        nn = np.linalg.norm(n, axis=1)
        quad = P.shape[1] == 4
        A[pos] = nn * (1.0 if quad else 0.5)
        C[pos], N[pos] = cen, n / np.maximum(nn, 1e-30)[:, None]
        for i, p in enumerate(pos):
            tri_of[p] = (P[i, [0, 1, 2]], P[i, [0, 2, 3]]) if quad else (P[i, [0, 1, 2]],)
    tri = np.array([t for ts in tri_of for t in ts]).reshape(-1, 3, 3)
    tris = _Triangles(tri)
    reach = float(np.sqrt(A.max())) if A.max() > 0 else 1.0
    order = np.argsort(-A, kind="stable")[:defaults.WALL_SAMPLE_FACETS]       # the largest facets first
    L = [tris.exit(C[i] - 1e-6 * N[i], -N[i], reach) for i in order]
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
