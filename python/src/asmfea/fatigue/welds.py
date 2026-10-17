"""Fatigue of bonded (welded) joints and of preloaded bolts (release 2 F3; PLAN-FATIGUE.md, D102, D106).

Welds, member side (the side whose section the tie is: a plate's edge, a member's end):
- W1 (default): Dong's structural stress (D33) in every window along the joint, for every case: a row's range per
  window is the larger surface range |d(sigma_m) +- d(sigma_b)|, read on the standard's structural stress curve
  (tables.structural_curve). Mesh-sensitive at tube corner radii (F0, P6: +17.6 % at standard base, +2.3 % after the
  local pass against a fused-solid hot-spot reference), so it is read on the reported (refined) mesh and its
  governing range must hold against the mesh before (FATIGUE_HOTSPOT_UNCONVERGED).
- W2: the joint's nominal stress, beam theory over the whole section (axial force over the area plus the two bending
  moments over the section's second moments, at each point of the section), on a detail category given per joint.
The receiving side (the wall the joint sits on) is not assessed yet (FATIGUE_RECEIVING_SIDE_NOT_ASSESSED).

Bolts: only in the preloaded mode, where the bolt is an element (D103): its tension in each case, its zero state the
preload step; the range on the tensile stress area, on the standard's bolt category. Bolts of the default mode (faces
tied, fastener removed) carry no force in the model and are listed (FATIGUE_BOLT_NOT_ASSESSED).
"""
from __future__ import annotations

import math

import numpy as np

from .. import defaults
from ..diagnostics import Diagnostic, make
from . import factor_k, life, row_states, tables


# ------------------------------------------------------------------ section read-outs

def _traction(lv, case: str, tie: dict, zero: bool):
    """(points, traction, weights) of a section tie in a case's step, or its zero state; None when not printed."""
    from ..post.joints import _traction_points
    src = lv.case_src.get(case)
    if src is None or src["dat"] is None:
        return None
    step = src["pre"] if zero else src["step"]
    if step is None:                                         # an unloaded zero state
        return "zero"
    nodal = src["dat"]["nodal"].get(step, {})
    F_tab = nodal.get(("forces", f"{tie['name']}S".upper())) or nodal.get(("forces", f"{tie['name']}S"))
    if not F_tab:
        return None
    return _traction_points(tie["facets"], F_tab, lv.table)


def windows(X, Tr, W, t_wall: float, h: float) -> list[dict]:
    """Every window of post.joints._section (planar sections): its membrane and bending."""
    from scipy.spatial import cKDTree
    c = X.mean(axis=0)
    _, s, vt = np.linalg.svd(X - c, full_matrices=False)
    n_s = vt[2]
    t = float(t_wall)
    R = max(0.5 * defaults.JOINT_PATCH_T * t, t, defaults.JOINT_WINDOW_ELEMENTS * h)
    F = Tr * W[:, None]
    out = []
    for i, sel in enumerate(cKDTree(X).query_ball_point(X, R, return_sorted=True)):
        sel = np.asarray(sel, dtype=np.int64)
        if len(sel) < 6:
            continue
        P, Fp, Wp = X[sel], F[sel], W[sel]
        cp = P.mean(axis=0)
        Q = P - cp
        Q -= np.outer(Q @ n_s, n_s)
        _, sv, wt = np.linalg.svd(Q, full_matrices=False)
        if sv[0] <= 1e-9:
            continue
        e_s = wt[0] - (wt[0] @ n_s) * n_s
        e_s /= np.linalg.norm(e_s)
        if e_s[np.argmax(np.abs(e_s))] < 0:
            e_s = -e_s
        e_t = np.cross(n_s, e_s)
        r0 = cp + e_t * float(((Q @ e_t) * Wp).sum() / Wp.sum())
        length = Wp.sum() / t
        f = float(Fp.sum(axis=0) @ n_s) / length
        m = float(np.cross(P - r0, Fp).sum(axis=0) @ e_s) / length
        out.append({"i": i, "at": r0, "sm": f / t, "sb": 6.0 * m / t ** 2})
    return out


def nominal(X, Tr, W, at: np.ndarray) -> np.ndarray:
    """The section's nominal normal stress at the points `at` (the section's nodes, on its faces: the integration
    points lie inside the wall and would miss the surface bending), beam theory over the section as it is:
    sigma(r) = N / A + a . r, r from the area centroid in the section's plane, with a = I+ q, q = n x M (the moment of
    the normal stresses about the centroid is q x n), I = sum W r r^T over the integration points."""
    F = Tr * W[:, None]
    c = (X * W[:, None]).sum(axis=0) / W.sum()
    _, _, vt = np.linalg.svd(X - c, full_matrices=False)
    n_s = vt[2]
    r = (X - c) - np.outer((X - c) @ n_s, n_s)
    A = W.sum()
    N = float(F.sum(axis=0) @ n_s)
    M = np.cross(r, F).sum(axis=0)
    I = (W[:, None, None] * r[:, :, None] * r[:, None, :]).sum(axis=0)
    a = np.linalg.pinv(I) @ np.cross(n_s, M)
    ra = (at - c) - np.outer((at - c) @ n_s, n_s)
    return N / A + ra @ a


# ------------------------------------------------------------------ welds

def _thickness(model, deck_info: dict, part: str):
    from ..post import _wall_thickness
    return _wall_thickness(model, deck_info).get(part)


def assess_welds(model, fat, levels: dict, reported: str, previous: str | None, progress=None) -> tuple[dict, list[Diagnostic]]:
    from ..post.joints import _element_size
    diags, out = [], {}
    lv = levels[reported]
    rows = fat.spectrum
    gamma = fat.gamma_Ff * fat.gamma_Mf
    variable = len(rows) > 1
    per_joint = {j.connection: j for j in fat.welds.joints}
    std = fat.standard
    ties = [t for t in lv.info.get("ties", []) if t.get("section") and t.get("facets")]
    for n_done, t in enumerate(ties):
        conn = t["connection"]
        if progress:
            progress(f"fatigue: bonded joints, {conn}", n_done, len(ties))
        jp = per_joint.get(conn)
        method = (jp.method if jp and jp.method else None) or fat.welds.method
        if method == "nominal":
            if not (jp and jp.category):
                diags.append(make("FATIGUE_NO_CATEGORY", where={"connections": [conn], "parts": [t["slave_part"]]},
                                  connection=conn))
                continue
            curve = tables.welded_category(std, jp.category)
        else:
            name = jp.category if jp and jp.category else fat.welds.structural_category
            curve = tables.structural_curve(std, name)
        if curve is None:
            choices = tables.welded_choices(std) if method == "nominal" else tables.STRUCTURAL_CHOICES[std]
            diags.append(make("FATIGUE_CATEGORY_WRONG_STANDARD",
                              where={"connections": [conn], "parts": [t["slave_part"]]}, connection=conn,
                              category=(jp.category if jp else None) or fat.welds.structural_category,
                              standard=tables.STANDARD_NAMES[std], evidence={"choices": choices}))
            continue
        rec = _one_joint(model, lv, t, rows, curve, method, gamma, variable, fat.design_blocks)
        if rec is None:
            continue
        if previous is not None and previous in levels:
            pv = levels[previous]
            tp = next((x for x in pv.info.get("ties", []) if x["name"] == t["name"] and x.get("facets")), None)
            if tp is not None:
                prev = _one_joint(model, pv, tp, rows, curve, method, gamma, variable, fat.design_blocks)
                if prev is not None and prev["max_range"] > 0:
                    change = (rec["max_range"] - prev["max_range"]) / prev["max_range"]
                    rec["convergence"] = {"previous_level": previous, "previous_max_range": prev["max_range"],
                                          "change": change}
                    if abs(change) > defaults.FATIGUE_PEAK_TOL:
                        diags.append(make("FATIGUE_HOTSPOT_UNCONVERGED", where={"parts": [t["slave_part"]],
                                                                               "connections": [conn]},
                                          part=f"{t['slave_part']} (joint {conn})", change=100 * change,
                                          tol=100 * defaults.FATIGUE_PEAK_TOL, evidence=rec["convergence"]))
        rec.setdefault("convergence", None)
        old = out.get(conn)
        if old is None or rec["damage"] > old["damage"]:
            out[conn] = rec
    return out, diags            # the receiving walls: fatigue/walls.py (D110)


def _one_joint(model, lv, t, rows, curve, method, gamma, variable, blocks):
    from ..post.joints import _element_size
    states, cache = {}, {}
    for row in rows:
        for (c, f), ref in zip(row_states(row), (row.case, row.min_case or row.case)):
            zero = f == 0.0
            key = (ref if zero else c, zero)
            if key not in cache:
                cache[key] = _traction(lv, ref if zero else c, t, zero)
            states[(c, f)] = (cache[key], 0.0 if zero else f)
    if any(v[0] is None for v in states.values()):
        return None
    pts = next((v[0] for v in states.values() if not isinstance(v[0], str)), None)
    if pts is None:
        return None
    X, _, W = pts
    if method == "dong":
        tw = _thickness(model, lv.case_src[rows[0].case]["info"], t["slave_part"])
        if not tw:
            return None
        h = _element_size(t["facets"], lv.table)
        base = windows(*pts, tw, h)

        def read(v):
            if isinstance(v[0], str):                       # an unloaded zero state
                return np.zeros(len(base)), np.zeros(len(base))
            w = windows(*v[0], tw, h)
            return np.array([x["sm"] for x in w]) * v[1], np.array([x["sb"] for x in w]) * v[1]
        at = np.array([x["at"] for x in base])
    else:
        at = lv.table[np.array(sorted({g for f in t["facets"] for g in f}), dtype=np.int64)]

        def read(v):
            if isinstance(v[0], str):
                return np.zeros(len(at)), None
            return nominal(*v[0], at) * v[1], None
    vals = {k: read(v) for k, v in states.items()}
    D, ranges = None, []
    for row in rows:
        a, b = row_states(row)
        (ma, ba), (mb, bb) = vals[a], vals[b]
        if method == "dong":
            dm, db = ma - mb, ba - bb
            rng = np.maximum(np.abs(dm + db), np.abs(dm - db))
        else:
            rng = np.abs(ma - mb)
        ranges.append(rng)
        d = row.cycles * blocks / life(curve, rng * gamma, variable)
        D = d if D is None else D + d
    g = int(np.argmax(D))
    per = np.array([r[g] for r in ranges])
    kk = factor_k(lambda k: float(sum(r.cycles * blocks / life(curve, np.array([per[i] * gamma * k]), variable)[0]
                                      for i, r in enumerate(rows))))
    return {"method": "W1 structural stress (Dong)" if method == "dong" else "W2 nominal stress",
            "part": t["slave_part"], "other": t["master_part"], "tie": t["name"], "side": "member",
            "curve": curve["name"], "curve_source": curve["source"], "curve_checked": curve.get("checked", True),
            "damage": float(D[g]), "life_blocks": float(blocks / D[g]) if D[g] > 0 else math.inf,
            "factor_k": float(kk), "pass": bool(D[g] <= 1.0),
            "governing": {"at": np.asarray(at[g]).round(4).tolist(), "row": int(np.argmax([r[g] for r in ranges])) + 1,
                          "range": float(max(r[g] for r in ranges))},
            "max_range": float(max(r.max() for r in ranges)),
            # the governing row's range averaged along the joint: what statics fixes (F0 P4: the hand check)
            "mean_range": float(ranges[int(np.argmax([r[g] for r in ranges]))].mean())}


# ------------------------------------------------------------------ bolts

def _bolt_tension(src: dict, b: dict, step: int):
    u = src["dat"]["nodal"].get(step, {}).get(("displacements", "BOLTNODES"), {}) if src["dat"] else {}
    if "spring_node" not in b or not all(n in u for n in (b["head_node"], b["spring_node"])):
        return None
    return b["k"] * float(np.dot(np.asarray(u[b["head_node"]][:3]) - np.asarray(u[b["spring_node"]][:3]), b["axis"]))


def assess_bolts(model, fat, lv, conns: list[dict]) -> tuple[dict, list[Diagnostic]]:
    diags, out = [], {}
    rows = fat.spectrum
    gamma = fat.gamma_Ff * fat.gamma_Mf
    variable = len(rows) > 1
    info = next((s["info"] for s in lv.case_src.values() if s["info"].get("bolts")), None)
    for b in (info or {}).get("bolts", []):
        T = {}
        for row in rows:
            for (c, f), ref in zip(row_states(row), (row.case, row.min_case or row.case)):
                src = lv.case_src.get(ref if f == 0.0 else c)
                cb = next((x for x in src["info"].get("bolts", []) if x["fastener"] == b["fastener"]), None) if src else None
                if cb is None:
                    T[(c, f)] = None
                    continue
                step = src["pre"] if f == 0.0 else src["step"]
                T[(c, f)] = _bolt_tension(src, cb, step) if step is not None else 0.0
        if any(v is None for v in T.values()) or not b.get("d"):
            continue
        As, how = tables.stress_area(float(b["d"]))
        curve = tables.bolt_curve(fat.standard, float(b["d"]), fat.bolts.thread)
        D, worst = 0.0, (0, 0.0)
        for i, row in enumerate(rows):
            a, bb = row_states(row)
            dS = abs(T[a] - T[bb]) / As
            d = row.cycles * fat.design_blocks / life(curve, np.array([dS * gamma]), variable)[0]
            D += d
            if dS > worst[1]:
                worst = (i, dS)
        ranges = [abs(T[row_states(r)[0]] - T[row_states(r)[1]]) / As for r in rows]
        kk = factor_k(lambda k: float(sum(r.cycles * fat.design_blocks /
                                          life(curve, np.array([ranges[i] * gamma * k]), variable)[0]
                                          for i, r in enumerate(rows))))
        out[b["fastener"]] = {"connection": b["connection"], "d": b["d"], "stress_area": As, "stress_area_from": how,
                              "curve": curve["name"], "curve_source": curve["source"],
                              "damage": float(D), "life_blocks": float(fat.design_blocks / D) if D > 0 else math.inf,
                              "factor_k": float(kk), "pass": bool(D <= 1.0),
                              "governing": {"row": worst[0] + 1, "stress_range": worst[1],
                                            "force_range": worst[1] * As},
                              "tension": {f"{c} {100 * f:g} %": v for (c, f), v in T.items()}}
    preloaded = {b["connection"] for b in (info or {}).get("bolts", [])}
    tied = sorted(c["id"] for c in conns if c.get("type") == "bolted" and c["id"] not in preloaded)
    if tied:
        diags.append(make("FATIGUE_BOLT_NOT_ASSESSED", where={"connections": tied}, count=len(tied)))
    return out, diags
