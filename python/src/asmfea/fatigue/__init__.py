"""Fatigue on the static results (release 2; PLAN-FATIGUE.md, D102, D105): parent material (F2).

A spectrum row is a load case cycling between two percentages (or between two cases). At each node the row's two
states give a range tensor and a mean tensor. The range is the principal of largest magnitude of the range tensor
(EN 1993-1-9 Annex B, nearly proportional loading), the mean the largest principal of the mean tensor; a user curve
(fully reversed amplitude) takes a mean-stress correction (Goodman by default), a standard's curve (a range curve
that includes residual stress) none. Damage is Miner's sum over the rows, times the blocks in the design life; the
fatigue factor k is the factor on every stress at which the governing damage reaches 1.

Read only where a notch peak is finite and converged (D32, D88): joint bands (welds are assessed by the joint
method, F3), load-face edges and fixture edges are left out; a governing point at a sharp re-entrant edge (no fillet:
no finite peak) is refused and the next one reported (FATIGUE_SHARP_NOTCH); the governing range must agree between
the last two meshes within FATIGUE_PEAK_TOL (FATIGUE_HOTSPOT_UNCONVERGED).

Prototyped in spikes/fatigue (P1-P3, P7: the arithmetic exact against hand answers).
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .. import defaults
from ..diagnostics import Diagnostic, make
from . import tables


# ------------------------------------------------------------------ curves

def life(curve: dict, S: np.ndarray, variable: bool) -> np.ndarray:
    """Cycles to failure. S: the range (standards' curves) or the fully reversed amplitude (user curves).
    variable: a spectrum of more than one row (below a two-slope curve's knee: its second slope, else no damage)."""
    S = np.asarray(S, float)
    N = np.full(S.shape, np.inf)
    pos = S > 0
    k = curve["kind"]
    if k == "basquin":
        N[pos] = 0.5 * (S[pos] / curve["Sf"]) ** (1.0 / curve["b"])
        if curve.get("endurance") and not variable:
            N[S <= curve["endurance"]] = np.inf
    elif k == "points":
        lS = np.log10(np.where(pos, S, 1.0))
        logN, logS = np.asarray(curve["logN"]), np.asarray(curve["logS"])
        # amplitude falls with N: interpolate log N on log S (reversed); above the first point, the first segment's
        # slope continued; below the last point's amplitude, no damage
        slope = (logN[1] - logN[0]) / (logS[1] - logS[0])
        n = np.interp(lS, logS[::-1], logN[::-1])
        n = np.where(lS > logS[0], logN[0] + slope * (lS - logS[0]), n)
        N[pos] = 10 ** n[pos]
        N[S < 10 ** logS[-1]] = np.inf
    elif k == "one_slope":
        N[pos] = curve["N_ref"] * (curve["S_ref"] / S[pos]) ** curve["m"]
    else:                                                        # two_slope
        hi = pos & (S >= curve["S_knee"])
        N[hi] = curve["A1"] / S[hi] ** curve["m1"]
        if variable and curve["m2"]:
            lo = pos & (S < curve["S_knee"])
            if curve["S_cut"] is not None:
                lo &= S >= curve["S_cut"]
            N[lo] = curve["A2"] / S[lo] ** curve["m2"]
    return N


def is_amplitude(curve: dict) -> bool:
    return curve["kind"] in ("basquin", "points")


def equivalent(sa: np.ndarray, sm: np.ndarray, rule: str, Su: float | None) -> np.ndarray:
    """Fully reversed amplitude equivalent to amplitude sa at mean sm (a compressive mean gets no credit)."""
    if rule == "none":
        return sa
    sm = np.maximum(sm, 0.0)
    r = sm / Su
    with np.errstate(divide="ignore", invalid="ignore"):
        if rule == "goodman":
            out = sa / (1.0 - r)
        else:                                                    # gerber
            out = sa / (1.0 - r ** 2)
    return np.where(r >= 1.0, np.inf, out)


# ------------------------------------------------------------------ tensors

def principal(t6: np.ndarray) -> np.ndarray:
    """(n, 3) ascending principal values of (n, 6) tensors xx yy zz xy yz zx (CalculiX order)."""
    T = np.zeros((len(t6), 3, 3))
    T[:, 0, 0], T[:, 1, 1], T[:, 2, 2] = t6[:, 0], t6[:, 1], t6[:, 2]
    T[:, 0, 1] = T[:, 1, 0] = t6[:, 3]
    T[:, 1, 2] = T[:, 2, 1] = t6[:, 4]
    T[:, 0, 2] = T[:, 2, 0] = t6[:, 5]
    return np.linalg.eigvalsh(T)


def von_mises(t6: np.ndarray) -> np.ndarray:
    s = t6
    return np.sqrt(0.5 * ((s[:, 0] - s[:, 1]) ** 2 + (s[:, 1] - s[:, 2]) ** 2 + (s[:, 2] - s[:, 0]) ** 2)
                   + 3.0 * (s[:, 3] ** 2 + s[:, 4] ** 2 + s[:, 5] ** 2))


def row_states(row) -> tuple[tuple[str, float], tuple[str, float]]:
    return (row.case, row.max_pct / 100.0), (row.min_case or row.case, row.min_pct / 100.0)


def row_range(A: np.ndarray, B: np.ndarray, measure: str) -> tuple[np.ndarray, np.ndarray]:
    """(range >= 0, mean) per node between states A and B ((n, 6) tensors)."""
    if measure == "signed_von_mises":
        # the von Mises of the range tensor, its sign that of the range's largest-magnitude principal
        rng = von_mises(A - B)
    else:
        pr = principal(A - B)
        rng = np.max(np.abs(pr), axis=1)
    mean = principal(0.5 * (A + B))[:, 2]
    return rng, mean


# ------------------------------------------------------------------ damage

def damage(curve: dict, rows: list, states: dict, rule: str, Su: float | None, measure: str, gamma: float,
           blocks: float, k: float = 1.0) -> tuple[np.ndarray, np.ndarray, list]:
    """Per node: (Miner's sum over the design life, the governing row's index, the rows' (range, mean)).
    states: {(case, factor): (n, 6) tensors}; gamma: gamma_Ff x gamma_Mf on the stresses; k: the factor tried."""
    variable = len(rows) > 1
    D, worst, per = None, None, []
    for i, row in enumerate(rows):
        a, b = row_states(row)
        rng, mean = row_range(states[a], states[b], measure)
        per.append((rng, mean))
        rng_k, mean_k = rng * k * gamma, mean * k * gamma
        if is_amplitude(curve):
            S = equivalent(0.5 * rng_k, mean_k, rule, Su)
        else:
            S = rng_k
        with np.errstate(divide="ignore"):          # a mean past the ultimate: no life (damage infinite)
            d = row.cycles * blocks / life(curve, S, variable)
        if D is None:
            D, worst, dmax = d, np.zeros(len(d), dtype=np.int64), d.copy()
        else:
            worst = np.where(d > dmax, i, worst)
            dmax = np.maximum(dmax, d)
            D = D + d
    return D, worst, per


def factor_k(damage_at, lo: float = 1e-4, hi: float = 1e4) -> float:
    """The factor on every stress at which the governing damage reaches 1: bisection on log k (damage rises with k).
    inf when even hi x the stresses do no damage."""
    f = lambda k: math.log(max(damage_at(k), 1e-300))
    if f(hi) < 0:
        return math.inf
    if f(lo) > 0:
        return lo
    for _ in range(200):
        mid = math.sqrt(lo * hi)
        if f(mid) > 0:
            hi = mid
        else:
            lo = mid
        if hi / lo < 1 + 1e-9:
            break
    return math.sqrt(lo * hi)


# ------------------------------------------------------------------ the inputs checked

def curves_for(model, fat, parts: list[str]) -> tuple[dict, list[Diagnostic]]:
    """{material: curve} for the materials of the parts assessed, and the diagnostics of what is missing."""
    diags, out = [], {}
    by_mat = {m.material: m for m in fat.materials}
    users: dict[str, list[str]] = {}
    for p in model.parts:
        if p.id in parts:
            users.setdefault(p.material, []).append(p.id)
    for mat, pids in users.items():
        fm = by_mat.get(mat)
        m = model.material(mat)
        sw = next((c for c in (m.sn_curves if m else []) if c.r_ratio == -1.0), None)
        if (fm is None or not (fm.basquin or fm.points or fm.standard_class)) and sw is not None:
            # the S-N curve stored with the SolidWorks material (D109), fully reversed: used when the study gives none
            factor = (fm.surface_factor * fm.size_factor) if fm else 1.0
            out[mat] = tables.user_points([list(p) for p in sw.points], factor, f"{mat}: S-N curve of the SolidWorks material",
                                          (fm.source if fm and fm.source else None) or sw.source or "the SolidWorks material")
            if fat.parent.mean_stress != "none" and not (m and m.ultimate):
                diags.append(make("FATIGUE_NO_ULTIMATE", where={"parts": sorted(pids)}, material=mat,
                                  rule=fat.parent.mean_stress))
                out.pop(mat)
            continue
        if (fm is None or not (fm.basquin or fm.points or fm.standard_class)) and m is not None:
            # AsmFEA's built-in data (D111): measured where the name matches, else estimated from Rm by family
            from .sn_data import builtin_curve
            c = builtin_curve(m, fm.surface if fm else None)
            if c is not None:
                out[mat] = c
                if fat.parent.mean_stress != "none" and not m.ultimate:
                    diags.append(make("FATIGUE_NO_ULTIMATE", where={"parts": sorted(pids)}, material=mat,
                                      rule=fat.parent.mean_stress))
                    out.pop(mat)
                continue
        if fm is None or not (fm.basquin or fm.points or fm.standard_class):
            diags.append(make("FATIGUE_NO_SN_CURVE", where={"parts": sorted(pids)}, material=mat,
                              evidence={"materials_with_curves": sorted(by_mat),
                                        "solidworks_sn_curves": [c.r_ratio for c in (m.sn_curves if m else [])]}))
            continue
        factor = fm.surface_factor * fm.size_factor
        source = fm.source or "the user"
        if fm.standard_class:
            c = tables.parent_class(fat.standard, fm.standard_class, m.yield_ if m else 0.0)
            if c is None:
                diags.append(make("FATIGUE_CLASS_UNKNOWN", where={"parts": sorted(pids)}, material=mat,
                                  cls=fm.standard_class, standard=tables.STANDARD_NAMES[fat.standard],
                                  evidence={"classes": sorted(tables.PARENT_CLASSES.get(fat.standard, {}))}))
                continue
            out[mat] = c
        elif fm.basquin:
            out[mat] = tables.user_basquin(fm.basquin.Sf, fm.basquin.b, fm.basquin.endurance_limit, factor,
                                           f"{mat}: Basquin (the user's)", source)
        else:
            out[mat] = tables.user_points(fm.points, factor, f"{mat}: S-N points (the user's)", source)
        if is_amplitude(out[mat]) and fat.parent.mean_stress != "none" and not (m and m.ultimate):
            diags.append(make("FATIGUE_NO_ULTIMATE", where={"parts": sorted(pids)}, material=mat,
                              rule=fat.parent.mean_stress))
            out.pop(mat)
    return out, diags


def check_spectrum(model, fat, nonlinear: set[str]) -> list[Diagnostic]:
    """The spectrum's cases exist; a nonlinear case (contact, preload) is used only at 0 % or 100 %."""
    diags = []
    known = set(model.study.load_cases)
    for i, row in enumerate(fat.spectrum, start=1):
        for c in {row.case, row.min_case or row.case} - known:
            diags.append(make("FATIGUE_UNKNOWN_CASE", row=i, case=c, evidence={"load_cases": sorted(known)}))
        for c, pct in ((row.case, row.max_pct), (row.min_case or row.case, row.min_pct)):
            if c in nonlinear and pct not in (0.0, 100.0):
                diags.append(make("FATIGUE_NONLINEAR_LEVEL", row=i, case=c, pct=pct))
    used = sorted({c for r in fat.spectrum for c in (r.case, r.min_case or r.case)} & nonlinear)
    if used and not any(d.code == "FATIGUE_NONLINEAR_LEVEL" for d in diags):
        diags.append(make("FATIGUE_NONLINEAR_PAIR", cases=", ".join(used)))
    return diags


# ------------------------------------------------------------------ one level's stresses

def _point_segment(P: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    t = np.clip(((P - a) @ ab) / max(ab @ ab, 1e-30), 0.0, 1.0)
    return np.linalg.norm(P - (a + t[:, None] * ab), axis=1)


class LevelStresses:
    """A solved mesh level's stress tensors per (case, factor) and the nodes fatigue reads, per part."""

    def __init__(self, level_dir: Path, model):
        from ..jobs import read_json
        from ..post.frd import read_frd
        self.dir = level_dir
        self.cases: dict[str, np.ndarray] = {}           # case -> (n, 6) at every node, in self.ids order
        self.zero: dict[str, np.ndarray] = {}            # case -> its "zero" state (a preloaded deck's preload step)
        self.nonlinear: set[str] = set()
        self.case_src: dict[str, dict] = {}
        self.info = None
        self.ids = None
        for r in read_json(level_dir / "runs.json"):
            if not r.get("cases"):
                continue
            rd = level_dir / r["dir"]
            info = read_json(rd / "deck_info.json")
            self.info = self.info or info
            frd = read_frd(rd / "study.frd")
            step_of = {s["case"]: s["step"] for s in info.get("steps", []) if s["kind"] == "static"}
            pre = next((s["step"] for s in info.get("steps", []) if s["kind"] == "bolt_preload"), None)
            if self.ids is None:
                self.ids = np.asarray(frd[0]["COORDS"][0], dtype=np.int64)
                X = np.asarray(frd[0]["COORDS"][1])[:, :3]
                self.coords = dict(zip(self.ids.tolist(), X))
                self.X = X
                self.pos = {int(n): i for i, n in enumerate(self.ids)}
            dat = None
            for c in r["cases"]:
                self.cases[c] = self._full(frd[step_of[c]]["STRESS"])
                self.zero[c] = self._full(frd[pre]["STRESS"]) if pre else np.zeros_like(self.cases[c])
                if info.get("contacts") or info.get("bolts"):
                    self.nonlinear.add(c)
                if dat is None and (rd / "study.dat").exists():
                    from ..post.frd import read_dat_steps
                    dat = read_dat_steps(rd / "study.dat")
                # what the joints and bolts read (F3): the printed nodal forces and displacements of the case's step
                # and of its zero state (a preloaded deck's preload step, else nothing)
                self.case_src[c] = {"info": info, "dat": dat, "step": step_of[c], "pre": pre}
        if self.ids is not None:
            self.table = np.full((int(self.ids.max()) + 1, 3), np.nan)
            self.table[self.ids] = self.X

    def _full(self, block) -> np.ndarray:
        n, s = block
        out = np.zeros((len(self.ids), 6))
        idx = np.array([self.pos[int(i)] for i in n], dtype=np.int64)
        out[idx] = np.asarray(s)[:, :6]
        return out

    def state(self, case: str, factor: float, ref: str) -> np.ndarray:
        """The stress at factor x case. 0 % is the case's zero state (the preload step of a preloaded deck)."""
        if factor == 0.0:
            return self.zero[ref]
        if case in self.nonlinear:
            return self.cases[case]                        # 100 % only (check_spectrum)
        return factor * self.cases[case]

    def part_nodes(self, part: str, sharp: list[dict]) -> tuple[np.ndarray, np.ndarray]:
        """(rows of the part's nodes fatigue reads, of those the ones near a sharp re-entrant edge). Left out: the
        joint band, load-face edges and fixture edges (within FATIGUE_EXCLUDE_ELEMENTS elements of a fixed node)."""
        from scipy.spatial import cKDTree
        inst = next(i for i in self.info["instances"] if i["part"] == part)
        lo, hi = inst["node0"] + 1, inst["node0"] + inst["n_nodes"]
        rows = np.array([self.pos[n] for n in range(lo, hi + 1) if n in self.pos], dtype=np.int64)
        ids = self.ids[rows]
        drop = np.isin(ids, np.asarray(self.info.get("tie_zone_nodes", {}).get(part, []), dtype=np.int64))
        drop |= np.isin(ids, np.asarray((self.info.get("load_edge_nodes") or {}).get(part, []), dtype=np.int64))
        P = self.X[rows]
        tree = cKDTree(P)
        h = float(np.median(tree.query(P, k=2)[0][:, 1])) * 2.0 if len(P) > 2 else 0.0     # ~ one element
        reach = defaults.FATIGUE_EXCLUDE_ELEMENTS * h
        fixed = [n for fx in self.info.get("fixtures", {}).values() for n in fx.get("node_ids", [])
                 if lo <= n <= hi and n in self.pos]
        if fixed and reach > 0:
            near = tree.query_ball_point(self.X[[self.pos[n] for n in fixed]], reach)
            hit = np.zeros(len(P), dtype=bool)
            for lst in near:
                hit[lst] = True
            drop |= hit
        sharp_near = np.zeros(len(P), dtype=bool)
        for e in sharp:
            if "a" in e and "b" in e:
                sharp_near |= _point_segment(P, np.asarray(e["a"]), np.asarray(e["b"])) <= reach
        keep = ~drop
        return rows[keep], rows[keep & sharp_near]


# ------------------------------------------------------------------ the assessment

def assess_parent(model, fat, levels: dict[str, LevelStresses], reported: str, previous: str | None,
                  geometry: dict, curves: dict, parts: list[str], progress=None) -> tuple[dict, list[Diagnostic]]:
    """Per part: damage, life, factor k, the governing node, row and stresses, and the convergence of its range.
    Also {part: why} for the parts not assessed, and {part: (node ids, damage)} for the damage plot."""
    diags, out, skipped, fields = [], {}, {}, {}
    rows = fat.spectrum
    gamma = fat.gamma_Ff * fat.gamma_Mf
    lv = levels[reported]
    for n_done, part in enumerate(parts):
        if progress:
            progress(f"fatigue: parent material, part {part}", n_done, len(parts))
        p = next(q for q in model.parts if q.id == part)
        curve = curves.get(p.material)
        if curve is None or not any(i["part"] == part for i in lv.info["instances"]):
            continue
        m = model.material(p.material)
        sharp = (geometry.get(part) or {}).get("sharp_edges", [])
        nodes, near_sharp = lv.part_nodes(part, sharp)
        if not len(nodes):
            skipped[part] = "every node is in a joint band or next to a support or load edge (no finite peak to read)"
            continue
        states = {}
        for row in rows:
            for (c, f), ref in zip(row_states(row), (row.case, row.min_case or row.case)):
                states[(c, f)] = lv.state(c, f, ref)[nodes]
        Su = m.ultimate if m else None
        D, worst, per = damage(curve, rows, states, fat.parent.mean_stress, Su, fat.parent.range, gamma,
                               fat.design_blocks)
        fields[part] = (lv.ids[nodes], D)
        g = int(np.argmax(D))
        refused = None
        if np.isin(nodes[g], near_sharp):
            # a sharp re-entrant edge has no finite peak: refused, the next point away from it reported
            refused = {"at": lv.X[nodes[g]].round(4).tolist(), "damage": float(D[g])}
            diags.append(make("FATIGUE_SHARP_NOTCH", where={"parts": [part]}, part=part,
                              at=", ".join(f"{v:.1f}" for v in lv.X[nodes[g]]),
                              evidence=refused))
            ok = ~np.isin(nodes, near_sharp)
            if not ok.any():
                skipped[part] = "every node is next to a sharp inside corner"
                continue
            g = int(np.flatnonzero(ok)[np.argmax(D[ok])])
        node = nodes[g]
        sel = np.array([g])
        st1 = {k: v[sel] for k, v in states.items()}
        k = factor_k(lambda kk: float(damage(curve, rows, st1, fat.parent.mean_stress, Su, fat.parent.range,
                                             gamma, fat.design_blocks, kk)[0][0]))
        r = int(worst[g])
        rng, mean = per[r][0][g], per[r][1][g]
        rec = {"material": p.material, "curve": curve["name"], "curve_source": curve["source"],
               "curve_checked": curve.get("checked", True), "curve_estimated": bool(curve.get("estimated", False)),
               "damage": float(D[g]), "life_blocks": float(fat.design_blocks / D[g]) if D[g] > 0 else math.inf,
               "factor_k": float(k), "pass": bool(D[g] <= 1.0),
               "governing": {"node": int(lv.ids[node]), "at": lv.X[node].round(4).tolist(), "row": r + 1,
                             "range": float(rng), "mean": float(mean)},
               "max_range": float(max(pr[0].max() for pr in per)),
               "sharp_notch_refused": refused}
        # convergence: the part's largest range on the previous mesh against this one
        if previous is not None and previous in levels:
            pv = levels[previous]
            if any(i["part"] == part for i in pv.info["instances"]):
                n2, _ = pv.part_nodes(part, sharp)
                ok2 = ~np.isin(n2, pv.part_nodes(part, sharp)[1]) if refused else np.ones(len(n2), dtype=bool)
                best = 0.0
                for row in (rows if ok2.any() else []):     # nothing to read on that mesh: no comparison
                    (a, fa), (b, fb) = row_states(row)
                    A = pv.state(a, fa, row.case)[n2[ok2]]
                    B = pv.state(b, fb, row.min_case or row.case)[n2[ok2]]
                    best = max(best, float(row_range(A, B, fat.parent.range)[0].max()))
                change = (rec["max_range"] - best) / best if best > 0 else None
                rec["convergence"] = {"previous_level": previous, "previous_max_range": best, "change": change}
                if change is not None and abs(change) > defaults.FATIGUE_PEAK_TOL:
                    diags.append(make("FATIGUE_HOTSPOT_UNCONVERGED", where={"parts": [part]}, part=part,
                                      change=100 * change, tol=100 * defaults.FATIGUE_PEAK_TOL,
                                      evidence=rec["convergence"]))
        else:
            rec["convergence"] = None
        out[part] = rec
    return out, skipped, diags, fields
