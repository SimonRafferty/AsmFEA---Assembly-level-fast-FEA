"""Fatigue of the receiving wall of bonded joints (release 2, D110): the wall a member's end or a plate's edge is
bonded onto (a chord's face under a rung).

Two methods, in this order:
1. Hand formulae, for a square or rectangular hollow section brace butted square onto the face of a hollow section
   chord (a T-joint) inside their range: van Wingerde's SCFs (HERON 37(2), 1992, Table 18; the formulae of CIDECT
   Design Guide 8 and IIW XV-1035-99) on the chord's hot-spot lines B, C and D, for brace axial load and in-plane
   bending, plus the chord's own stress at the joint (the chord-load SCFs). The nominal stresses come from the forces
   the joint carries (the tie's section, as W1 and W2) and the chord's axial stress beside the joint. The terms are
   added as absolute values (the thesis's rule, conservative). These are formulae for welded joints, so the value is
   what a welded joint shows. Out-of-plane bending has no published formula for these joints: a joint whose brace
   carries it beyond FORMULA_OPB_FRACTION of its other stresses falls to method 2.
2. The IIW surface hot spot read from the bonded model round the joint's footprint (0.4 t and 1.0 t from the toe,
   linear extrapolation), on the reported mesh, as an indication: on a reference T-joint it read 13-40 % low on base
   and coarse meshes, and from 14 % low to 30 % high on refined meshes against a solid model and the formulae
   (spikes/fatigue P9-P13). The bonded model has no weld bead: against the welded formulae it read 14-30 % high on the
   refined solid; reading it a weld leg further out was 25-70 % low (P13), so no correction is made.
"""
from __future__ import annotations

import math

import numpy as np

from .. import defaults
from ..diagnostics import make
from . import factor_k, life, row_states, tables


# ------------------------------------------------------------------ van Wingerde's SCFs (thesis Table 18)

def scf_t_joint(beta: float, g: float, tau: float) -> dict:
    """{load: {line: SCF}} for an SHS T-joint, quadratic hot-spot extrapolation, butt welds; g = 2 gamma = b0/t0.
    Loads: "a1" brace axial (on N1/A1), "m1" brace in-plane bending (on M1/W1), "0" chord axial or in-plane bending
    (on N0/A0 or M0/W0). Checked against the thesis's worked example (beta 0.7, 2 gamma 16, tau 0.4: 4.50, 4.27, 2.11)."""
    b2 = beta * beta
    t75 = tau ** 0.75
    m1 = {"B": (-0.011 + 0.085 * beta - 0.073 * b2) * g ** (1.722 + 1.151 * beta - 0.697 * b2) * t75,
          "C": (0.952 - 3.062 * beta + 2.382 * b2 + 0.0228 * g) * g ** (-0.690 + 5.817 * beta - 4.685 * b2) * t75,
          "D": (-0.054 + 0.332 * beta - 0.258 * b2) * g ** (2.084 - 1.062 * beta + 0.527 * b2) * t75}
    a1 = {"B": (0.143 - 0.204 * beta + 0.064 * b2) * g ** (1.377 + 1.715 * beta - 1.103 * b2) * t75,
          "C": (0.077 - 0.129 * beta + 0.061 * b2 - 0.0003 * g) * g ** (1.565 + 1.874 * beta - 1.028 * b2) * t75,
          "D": (0.208 - 0.387 * beta + 0.209 * b2) * g ** (0.925 + 2.398 * beta - 1.881 * b2) * t75}
    c0 = {"B": 0.0, "C": 0.725 * g ** (0.248 * beta) * tau ** 0.19, "D": 1.373 * g ** (0.205 * beta) * tau ** 0.24}
    # IIW XV-1035-99 5.5.1: an SCF for a brace load not below 2.0
    return {"a1": {k: max(v, defaults.FORMULA_SCF_MIN) for k, v in a1.items()},
            "m1": {k: max(v, defaults.FORMULA_SCF_MIN) for k, v in m1.items()}, "0": c0}


FORMULA_RANGE = {"beta": (0.35, 1.0), "2gamma": (12.5, 25.0), "tau": (0.25, 1.0)}   # thesis / IIW validity


# ------------------------------------------------------------------ geometry of a joint

def _member_axis(model, part_id: str):
    p = next((q for q in model.parts if q.id == part_id), None)
    m = getattr(p, "member", None) if p else None
    if m is None or not isinstance(m.path, dict) or m.path.get("type") != "line" or not isinstance(m.profile, dict):
        return None, None
    T = np.asarray(p.transform, float).reshape(4, 4)
    a = T[:3, :3] @ (np.asarray(m.path["p1"], float) - np.asarray(m.path["p0"], float))
    return a / np.linalg.norm(a), m.profile


def joint_geometry(model, lv, tie: dict, wall_t: float | None, brace_t: float | None):
    """The T-joint's frame and parameters, or (None, why). n: the brace axis (the section's normal, out of the chord),
    e_c: the chord axis in the section plane, e_w: across the chord; beta, 2 gamma, tau from the geometry."""
    from .welds import _traction
    ca, cprof = _member_axis(model, tie["master_part"])
    ba, bprof = _member_axis(model, tie["slave_part"])
    if ca is None or ba is None:
        return None, "not two hollow-section members"
    if str(cprof.get("type", "")).upper() not in ("SHS", "RHS") or str(bprof.get("type", "")).upper() not in ("SHS", "RHS"):
        return None, "not two square or rectangular hollow sections"
    if abs(ca @ ba) > math.sin(math.radians(defaults.FORMULA_ANGLE_TOL_DEG)):
        return None, "the brace is not square to the chord (a Y- or K-joint)"
    S = lv.table[np.array(sorted({g for f in tie["facets"] for g in f}), dtype=np.int64)]
    c = S.mean(axis=0)
    _, _, vt = np.linalg.svd(S - c, full_matrices=False)
    n = vt[2] * np.sign(vt[2] @ ba) if abs(vt[2] @ ba) > 1e-9 else vt[2]
    e_c = ca - (ca @ n) * n
    e_c /= np.linalg.norm(e_c)
    e_w = np.cross(n, e_c)
    b1 = float(np.ptp((S - c) @ e_w))               # the brace's width across the chord (its bonded footprint)
    # the chord's width across: its nodes near the joint
    inst = next(i for i in lv.info["instances"] if i["part"] == tie["master_part"])
    ids = np.arange(inst["node0"] + 1, inst["node0"] + inst["n_nodes"] + 1)
    ids = ids[ids < len(lv.table)]
    P = lv.table[ids]
    ok = np.isfinite(P[:, 0])
    P = P[ok]
    near = np.abs((P - c) @ e_c) < 0.5 * b1
    if not near.any():
        return None, "the chord's section could not be measured"
    b0 = float(np.ptp((P[near] - c) @ e_w))
    t0, t1 = wall_t, brace_t
    if not (t0 and t1 and b0 > 0 and b1 > 0):
        return None, "wall thicknesses unknown"
    out = {"n": n, "e_c": e_c, "e_w": e_w, "centre": c, "b0": b0, "b1": b1, "t0": t0, "t1": t1,
           "beta": b1 / b0, "2gamma": b0 / t0, "tau": t1 / t0}
    bad = [f"{k} {out[k]:.3g} outside {lo:g}-{hi:g}" for k, (lo, hi) in FORMULA_RANGE.items()
           if not (lo - 1e-6 <= out[k] <= hi + 1e-6)]
    if bad:
        return None, "outside the formulae's range: " + ", ".join(bad)
    return out, None


def brace_nominal(X, Tr, W, geo: dict) -> tuple[float, float, float]:
    """(sigma_a1 = N / A1, sigma_m1 = M_in-plane / W1, sigma_op = M_out-of-plane / W1') of the brace's section."""
    F = Tr * W[:, None]
    c = (X * W[:, None]).sum(axis=0) / W.sum()
    r = (X - c) - np.outer((X - c) @ geo["n"], geo["n"])
    A = W.sum()
    N = float(F.sum(axis=0) @ geo["n"])
    M = np.cross(r, F).sum(axis=0)
    yc, yw = r @ geo["e_c"], r @ geo["e_w"]
    I_c, I_w = float((W * yc ** 2).sum()), float((W * yw ** 2).sum())
    # in-plane bending: in the plane of the brace and chord axes (moment about e_w), stress along e_c
    m_ip = float(M @ geo["e_w"])
    m_op = float(M @ geo["e_c"])
    half_c, half_w = 0.5 * geo["b1"], 0.5 * geo["b1"]
    return N / A, m_ip / (I_c / half_c), m_op / (I_w / half_w)


def chord_nominal(lv, case: str, factor: float, ref: str, tie: dict, geo: dict) -> float:
    """The chord's axial stress at its brace-side face, beside the joint (the larger of the two sides' means between
    0.5 and 1.5 chord widths clear of the brace): the chord axial plus in-plane bending stress the chord-load SCFs
    multiply."""
    inst = next(i for i in lv.info["instances"] if i["part"] == tie["master_part"])
    lo, hi = inst["node0"] + 1, inst["node0"] + inst["n_nodes"]
    rows = np.array([lv.pos[i] for i in range(lo, hi + 1) if i in lv.pos], dtype=np.int64)
    P = lv.X[rows]
    d = (P - geo["centre"])
    face = np.abs(d @ geo["n"]) < 0.25 * geo["t0"]
    s = d @ geo["e_c"]
    best = 0.0
    S = lv.state(case, factor, ref)[rows]
    e = geo["e_c"]
    sig = (S[:, 0] * e[0] ** 2 + S[:, 1] * e[1] ** 2 + S[:, 2] * e[2] ** 2
           + 2 * (S[:, 3] * e[0] * e[1] + S[:, 4] * e[1] * e[2] + S[:, 5] * e[0] * e[2]))
    for side in (-1, 1):
        lo_, hi_ = 0.5 * geo["b1"] + 0.5 * geo["b0"], 0.5 * geo["b1"] + 1.5 * geo["b0"]
        sel = face & (side * s > lo_) & (side * s < hi_)
        if sel.any():
            v = float(sig[sel].mean())
            if abs(v) > abs(best):
                best = v
    return best


# ------------------------------------------------------------------ the IIW read-out from the model (method 2)

def fe_hot_spots(lv, case_states: dict, tie: dict, t0: float, rows) -> list[float]:
    """Per row, the largest IIW surface hot-spot range round the joint's footprint on the receiving part
    (0.4 t0 and 1.0 t0 out from the footprint's edge, linear), or [] when it cannot be read."""
    from scipy.interpolate import LinearNDInterpolator
    from scipy.spatial import cKDTree
    facets = tie["facets"]
    S_ids = np.array(sorted({g for f in facets for g in f}), dtype=np.int64)
    S = lv.table[S_ids]
    c = S.mean(axis=0)
    _, _, vt = np.linalg.svd(S - c, full_matrices=False)
    n_s = vt[2]
    # the footprint's outline: facet edges (corner to corner) used by one facet only
    count = {}
    for f in facets:
        nc = 3 if len(f) == 6 else 4
        for i in range(nc):
            e = tuple(sorted((f[i], f[(i + 1) % nc])))
            count[e] = count.get(e, 0) + 1
    edges = [e for e, k in count.items() if k == 1]
    if not edges:
        return []
    # the receiving part's nodes near the footprint
    inst = next(i for i in lv.info["instances"] if i["part"] == tie["master_part"])
    lo, hi = inst["node0"] + 1, inst["node0"] + inst["n_nodes"]
    rows_m = np.array([lv.pos[i] for i in range(lo, hi + 1) if i in lv.pos], dtype=np.int64)
    P = lv.X[rows_m]
    reach = float(np.max(np.linalg.norm(S - c, axis=1))) + 3 * t0
    near = np.linalg.norm(P - c, axis=1) < reach
    if near.sum() < 10:
        return []
    P, rows_m = P[near], rows_m[near]
    # outward of the receiving face: away from the receiving part's nodes
    side = -np.sign(np.mean((P - c) @ n_s)) or 1.0
    n_out = n_s * side
    pts, dirs = [], []
    for a, b in edges:
        A, B = lv.table[a], lv.table[b]
        m = 0.5 * (A + B)
        t = B - A
        d = np.cross(t, n_out)
        d /= max(np.linalg.norm(d), 1e-12)
        if d @ (m - c) < 0:
            d = -d
        if d @ (m - c) <= 0:
            continue
        pts.append(m)
        dirs.append(d)
    if not pts:
        return []
    pts, dirs = np.array(pts), np.array(dirs)
    Q = np.vstack([pts + 0.4 * t0 * dirs - 0.05 * t0 * n_out, pts + 1.0 * t0 * dirs - 0.05 * t0 * n_out])
    # only the nodes round the read-out points go into the triangulation: a long seam's receiving part is mostly far
    # from its outline, and a 3-D triangulation of a whole thin sheet took minutes (found on a user's thin sheet-metal guard, 2026-10-17)
    from scipy.spatial import Delaunay
    tree = cKDTree(P)
    hood = sorted({i for lst in tree.query_ball_point(Q, 2.0 * t0 + 2.0 * _spacing(tree, P)) for i in lst})
    if len(hood) < 10:
        return []
    P, rows_m = P[hood], rows_m[hood]
    try:
        tri = Delaunay(P)
    except Exception:                                   # degenerate: nothing to read
        return []
    out = []
    npt = len(pts)
    for row in rows:
        (ca_, fa), (cb, fb) = row_states(row)
        dS = case_states[(ca_, fa)][rows_m] - case_states[(cb, fb)][rows_m]
        S6 = LinearNDInterpolator(tri, dS)(Q)                                   # (2 npt, 6)
        T = np.zeros((len(Q), 3, 3))
        T[:, 0, 0], T[:, 1, 1], T[:, 2, 2] = S6[:, 0], S6[:, 1], S6[:, 2]
        T[:, 0, 1] = T[:, 1, 0] = S6[:, 3]
        T[:, 1, 2] = T[:, 2, 1] = S6[:, 4]
        T[:, 0, 2] = T[:, 2, 0] = S6[:, 5]
        D2 = np.vstack([dirs, dirs])
        sn = np.einsum("ni,nij,nj->n", D2, T, D2)                              # across the toe
        v1, v2 = sn[:npt], sn[npt:]
        hs = np.abs(v1 + (v1 - v2) * 0.4 / 0.6)
        hs = hs[np.isfinite(hs)]
        out.append(float(hs.max()) if len(hs) else 0.0)
    return out


def _spacing(tree, P) -> float:
    """A typical node spacing of the receiving part's mesh near the joint."""
    if len(P) < 3:
        return 0.0
    k = min(len(P), 2000)
    d, _ = tree.query(P[np.linspace(0, len(P) - 1, k).astype(int)], k=2)
    return float(np.median(d[:, 1]))


# ------------------------------------------------------------------ assessment

def assess_walls(model, fat, levels: dict, reported: str, joints: dict, progress=None) -> tuple[dict, list]:
    """{connection: record} for the receiving walls of the section ties assessed on the member side (joints)."""
    from ..post import _wall_thickness
    from .welds import _traction
    lv = levels[reported]
    rows = fat.spectrum
    gamma = fat.gamma_Ff * fat.gamma_Mf
    variable = len(rows) > 1
    curve = tables.structural_curve(fat.standard, fat.welds.structural_category)
    diags, out = [], {}
    any_info = next(iter(lv.case_src.values()))["info"] if lv.case_src else lv.info
    thick = _wall_thickness(model, any_info)
    todo = [t for t in lv.info.get("ties", []) if t.get("section") and t.get("facets") and t["connection"] in joints]
    for n_done, t in enumerate(todo):
        conn = t["connection"]
        if progress:
            progress(f"fatigue: the walls under bonded joints, {conn}", n_done, len(todo))
        geo, why = joint_geometry(model, lv, t, thick.get(t["master_part"]), thick.get(t["slave_part"]))
        rec = None
        if geo is not None:
            rec = _formula(model, lv, t, geo, rows, curve, gamma, variable, fat.design_blocks)
            if rec is None:
                why = "out-of-plane bending of the brace (no published formula)"
        if rec is None:
            rec = _indicative(model, lv, t, thick.get(t["master_part"]), rows, curve, gamma, variable,
                              fat.design_blocks, reported)
            if rec is not None:
                rec["why_not_formulae"] = why
                diags.append(make("FATIGUE_RECEIVING_SIDE_INDICATIVE", where={"connections": [conn],
                                                                              "parts": [t["master_part"]]},
                                  connection=conn, part=t["master_part"], why=why))
        if rec is None:
            diags.append(make("FATIGUE_RECEIVING_SIDE_NOT_ASSESSED", where={"connections": [conn],
                                                                            "parts": [t["master_part"]]},
                              connection=conn, part=t["master_part"]))
            continue
        old = out.get(conn)
        if old is None or rec["damage"] > old["damage"]:
            out[conn] = rec
    return out, diags


def _row_ranges_to_damage(curve, ranges, rows, gamma, variable, blocks):
    D = sum(r.cycles * blocks / life(curve, np.array([s * gamma]), variable)[0] for s, r in zip(ranges, rows))
    k = factor_k(lambda kk: float(sum(r.cycles * blocks / life(curve, np.array([s * gamma * kk]), variable)[0]
                                      for s, r in zip(ranges, rows))))
    return float(D), float(k)


def _formula(model, lv, t, geo, rows, curve, gamma, variable, blocks):
    from .welds import _traction
    scf = scf_t_joint(geo["beta"], geo["2gamma"], geo["tau"])
    states, chord = {}, {}
    for row in rows:
        for (c, f), ref in zip(row_states(row), (row.case, row.min_case or row.case)):
            zero = f == 0.0
            src = _traction(lv, ref if zero else c, t, zero)
            if src is None:
                return None
            if isinstance(src, str):                       # an unloaded zero state
                states[(c, f)] = (0.0, 0.0, 0.0)
            elif zero:                                     # a preloaded deck's preload step, as it is
                states[(c, f)] = brace_nominal(*src, geo)
            else:
                states[(c, f)] = tuple(f * x for x in brace_nominal(*src, geo))
            chord[(c, f)] = chord_nominal(lv, c, f, ref, t, geo)
    ranges, lines_out = [], []
    for row in rows:
        a, b = row_states(row)
        da = states[a][0] - states[b][0]
        dm = states[a][1] - states[b][1]
        dop = states[a][2] - states[b][2]
        d0 = chord[a] - chord[b]
        if abs(dop) > defaults.FORMULA_OPB_FRACTION * max(abs(da) + abs(dm), 1e-9):
            return None
        per = {L: abs(scf["a1"][L] * da) + abs(scf["m1"][L] * dm) + abs(scf["0"][L] * d0) for L in ("B", "C", "D")}
        L = max(per, key=per.get)
        ranges.append(per[L])
        lines_out.append({"line": L, "range": per[L], "brace_axial": da, "brace_in_plane_bending": dm,
                          "brace_out_of_plane_bending": dop, "chord": d0})
    D, k = _row_ranges_to_damage(curve, ranges, rows, gamma, variable, blocks)
    g = int(np.argmax([r.cycles / life(curve, np.array([s * gamma]), variable)[0] for s, r in zip(ranges, rows)]))
    return {"method": "receiving wall: hand formulae (van Wingerde 1992 / CIDECT DG8 / IIW XV-1035-99, welded)",
            "part": t["master_part"], "other": t["slave_part"], "tie": t["name"], "side": "receiving",
            "curve": curve["name"], "curve_source": curve["source"], "curve_checked": curve.get("checked", True),
            "parameters": {k2: round(geo[k2], 4) for k2 in ("beta", "2gamma", "tau", "b0", "b1", "t0", "t1")},
            "scf": {k2: {L: round(v, 3) for L, v in d.items()} for k2, d in scf.items()},
            "damage": D, "life_blocks": blocks / D if D > 0 else math.inf, "factor_k": k, "pass": bool(D <= 1.0),
            "governing": {"row": g + 1, "range": float(ranges[g]), **lines_out[g],
                          "at": np.asarray(geo["centre"]).round(4).tolist()},
            "max_range": float(max(ranges)), "thin_wall": bool(geo["t0"] < 4.0 or geo["t1"] < 4.0)}


def _indicative(model, lv, t, t0, rows, curve, gamma, variable, blocks, level):
    if not t0:
        return None
    states = {}
    for row in rows:
        for (c, f), ref in zip(row_states(row), (row.case, row.min_case or row.case)):
            states[(c, f)] = lv.state(c, f, ref)
    ranges = fe_hot_spots(lv, states, t, t0, rows)
    if not ranges or max(ranges) <= 0:
        return None
    D, k = _row_ranges_to_damage(curve, ranges, rows, gamma, variable, blocks)
    g = int(np.argmax(ranges))
    return {"method": "receiving wall: IIW surface hot spot from the bonded model (indicative)",
            "part": t["master_part"], "other": t["slave_part"], "tie": t["name"], "side": "receiving",
            "curve": curve["name"], "curve_source": curve["source"], "curve_checked": curve.get("checked", True),
            "damage": D, "life_blocks": blocks / D if D > 0 else math.inf, "factor_k": k, "pass": bool(D <= 1.0),
            "governing": {"row": g + 1, "range": float(ranges[g])}, "max_range": float(max(ranges)),
            "level": level}
