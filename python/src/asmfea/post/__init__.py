"""Post-processing: results.json from the solver output, convergence, and the checks.

Per load case and part: peak von Mises (raw, and excluding nodes within one element of a
tie boundary) with location, minimum factor of safety on yield, maximum displacement.
Per load case: maximum displacement with location, applied and reaction totals.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .. import defaults
from ..diagnostics import Diagnostic, make
from ..model import Model
from .frd import read_dat_buckling, read_dat_steps, read_frd, von_mises


def _coords_lookup(frd: dict):
    ids, xyz = frd.get(0, {}).get("COORDS", (np.empty(0, np.int64), np.empty((0, 3))))
    table = np.full((int(ids.max()) + 1 if len(ids) else 1, 3), np.nan)
    table[ids] = xyz[:, :3]
    return lambda n: table[int(n)].round(4).tolist() if 0 <= int(n) < len(table) else None


def _wall_thickness(model: Model, deck_info: dict | None = None) -> dict:
    """Wall thickness per part: the sheet's or the member profile's, else the one measured from the mesh
    (deck_info "wall_thickness"); None for solids (D33, D34)."""
    measured = (deck_info or {}).get("wall_thickness", {})
    out = {}
    for p in model.parts:
        t = None
        if getattr(p, "sheet", None) is not None and p.sheet.thickness:
            t = float(p.sheet.thickness)
        elif getattr(p, "member", None) is not None and isinstance(p.member.profile, dict) and p.member.profile.get("t"):
            t = float(p.member.profile["t"])
        out[p.id] = t if t else measured.get(p.id)
    return out


def summarise(model: Model, deck_info: dict, frd_path: Path, dat_path: Path,
              tie_nodes: dict[str, np.ndarray], model_size: float,
              singular_by_imbalance: bool = True) -> tuple[dict, list[Diagnostic]]:
    """tie_nodes: per part, global ids of nodes within one element of a tie boundary.
    model_size: diagonal of the assembly bounding box, mm. singular_by_imbalance: an unbalanced reaction on
    its own declares the model singular (the full run); a fast run needs huge displacements as well, because
    a tie whose master face holds fixed nodes passes load into the supports that CalculiX's RF omits (a 17 %
    imbalance with 0.5 mm deflection on the speed investigation's truss); the imbalance stays a warning."""
    frd = read_frd(frd_path)
    at = _coords_lookup(frd)
    dat = read_dat_steps(dat_path) if dat_path.exists() else {"totals": {}, "nodal": {}}
    diags: list[Diagnostic] = []
    out_cases = {}
    step_of = {s["case"]: s["step"] for s in deck_info.get("steps", []) if s["kind"] == "static"}
    for k, case in enumerate(deck_info["load_cases"], start=1):
        k = step_of.get(case, k)   # bolt preload steps come first in contact decks
        blocks = frd.get(k, {})
        un, uv = blocks["DISP"]
        sn, sv = blocks["STRESS"]
        umag = np.linalg.norm(uv[:, :3], axis=1)
        imax = int(np.argmax(umag))
        vm = von_mises(sv[:, :6])
        parts = {}
        for inst in deck_info["instances"]:
            lo, hi = inst["node0"] + 1, inst["node0"] + inst["n_nodes"]
            sel = (sn >= lo) & (sn <= hi)
            if not sel.any():
                continue
            ids, v = sn[sel], vm[sel]
            i = int(np.argmax(v))
            excl = np.isin(ids, tie_nodes.get(inst["part"], np.empty(0, dtype=np.int64)))
            if (~excl).any():
                j = int(np.argmax(np.where(excl, -np.inf, v)))
            else:
                j = i
            mat = model.material(inst["material"])
            usel = (un >= lo) & (un <= hi)
            ju = int(np.argmax(np.where(usel, umag, -np.inf)))
            parts[inst["part"]] = {
                "peak_vm": float(v[i]), "peak_vm_node": int(ids[i]), "peak_vm_at": at(ids[i]),
                "peak_vm_excl_ties": float(v[j]), "peak_vm_excl_ties_at": at(ids[j]),
                "min_fos": mat.yield_ / float(v[j]) if v[j] > 0 else None,
                "max_disp": float(umag[ju]) if usel.any() else 0.0,
                "max_disp_at": at(un[ju]) if usel.any() else None,
            }
        applied = np.asarray(deck_info["load_cases"][case]["applied"])
        # CalculiX's RF omits load applied straight onto constrained DOFs; the deck
        # writer computed that share, so the balance compares like with like.
        supports = np.asarray(deck_info["load_cases"][case].get("into_supports", [0.0, 0.0, 0.0]))
        reaction = dat["totals"].get(k, {}).get("FIXALL")
        extra = _contact_and_bolts(deck_info, blocks, dat, k, applied, case, un, uv, diags,
                                   frd.get(0, {}).get("COORDS"))
        # RF also misses forces that reach the supports through equations (a bolt end on a
        # fixed face) and through the stabilising springs to ground: add them back.
        held = np.zeros(3)
        for b in deck_info.get("bolts", []):
            T = extra.get("bolts", {}).get(b["fastener"], {}).get("force")
            if T is not None:
                axis = np.asarray(b["axis"], float)        # nut towards head
                held -= b.get("nut_fixed", 0.0) * T * axis   # the bolt pulls its nut end towards the head
                held += b.get("head_fixed", 0.0) * T * axis
        held += np.asarray(extra.get("stabilising_spring_force", [0.0, 0.0, 0.0]))   # springs act like supports
        balance = None
        if reaction is not None:
            denom = max(np.linalg.norm(applied), 1e-12)
            balance = float(np.linalg.norm(applied - supports + reaction + held) / denom)
        # PARDISO does not stop on a singular matrix: it returns huge displacements and
        # unbalanced reactions. Treat that signature as singular (milestone 0/1 finding).
        umax = float(umag[imax])
        mover = max(parts, key=lambda p: parts[p]["max_disp"]) if parts else None
        direction = (uv[imax, :3] / max(umax, 1e-300)).round(3).tolist()
        if umax > defaults.SINGULAR_DISP_FRACTION * model_size or (
                singular_by_imbalance and balance is not None and balance > defaults.SINGULAR_IMBALANCE):
            diags.append(make("SOLVE_SINGULAR", where={"parts": [mover] if mover else []},
                              detail=f" (load case {case}: part {mover} moves {umax:.3g} mm along {direction})",
                              evidence={"max_disp": umax, "model_size": model_size, "direction": direction,
                                        "reaction_balance_err": balance}))
            continue
        if umax > defaults.LARGE_DISP_FRACTION * model_size:
            diags.append(make("DISPLACEMENT_LARGE", where={"parts": [mover]}, case=case, disp=umax,
                              frac=umax / model_size, evidence={"model_size": model_size}))
        if balance is not None and balance > defaults.REACTION_BALANCE_TOL:
            diags.append(make("REACTION_IMBALANCE", err=balance, case=case,
                              evidence={"applied": applied.tolist(), "reaction": reaction.tolist()}))
        # structural stress at bonded joints, from the forces they carry (D33)
        joints = []
        if any(t.get("section") for t in deck_info.get("ties", [])):
            from .joints import joint_stresses
            ids_c, xyz_c = frd.get(0, {}).get("COORDS", (np.empty(0, np.int64), np.empty((0, 3))))
            table = np.full((int(ids_c.max()) + 1 if len(ids_c) else 1, 3), np.nan)
            table[ids_c] = xyz_c[:, :3]
            joints = joint_stresses(deck_info, dat["nodal"].get(k, {}), table, _wall_thickness(model, deck_info))
            for j in joints:
                pr = parts.get(j["part"])
                if pr is None:
                    continue
                mat = model.material(next(i["material"] for i in deck_info["instances"] if i["part"] == j["part"]))
                j["fos"] = mat.yield_ / j["structural_stress"] if j["structural_stress"] > 0 else None
                if j["structural_stress"] > pr.get("joint_stress", 0.0):
                    pr.update(joint_stress=j["structural_stress"], joint_stress_at=j["at"], joint_fos=j["fos"],
                              joint_with=j["other"])
        out_cases[case] = {
            **extra,
            "joints": joints,
            "max_disp": umax, "max_disp_node": int(un[imax]), "max_disp_at": at(un[imax]),
            "max_disp_vector": uv[imax, :3].tolist(),
            "applied": applied.tolist(),
            "applied_directly_to_supports": supports.tolist(),
            "reaction": None if reaction is None else reaction.tolist(),
            # everything the supports carry: CalculiX's RF plus the load applied straight onto held
            # DOFs (RF leaves it out; self-weight on a fixed face, for example) and the forces that
            # reach supports through bolt equations and stabilising springs (verification C2 finding)
            "support_reaction": None if reaction is None else (reaction - supports + held).tolist(),
            "reaction_balance_err": balance,
            "parts": parts,
        }
    return {"load_cases": out_cases}, diags


def _contact_and_bolts(info: dict, blocks: dict, dat: dict, step: int, applied: np.ndarray, case: str,
                       un: np.ndarray, uv: np.ndarray, diags: list, coords=None) -> dict:
    """Contact status, bolt forces and stabilising-spring load share for one load case."""
    out = {}
    if info.get("contacts"):
        cont = blocks.get("CONTACT")
        status = {"pairs": [c["connection"] for c in info["contacts"]]}
        if cont is not None:
            ids, v = cont
            # CDIS components: COPEN, CSLIP1, CSLIP2. CalculiX stores only negative COPEN
            # (penetration) and sets clearance to zero, so this gives penetration only.
            copen = v[:, 0]
            status["max_penetration"] = float(-copen.min()) if len(copen) else 0.0
        status.update(_openings(info["contacts"], un, uv, coords))
        cstr = blocks.get("CSTR") or blocks.get("CONTACT_STRESS")
        if cstr is not None:
            status["max_pressure"] = float(cstr[1][:, 0].max())
        out["contact"] = status
    if info.get("bolts"):
        # bolt tension = preload + k x elongation. The reference nodes follow the weighted
        # mean of their bearing annuli (equations in the deck); the annulus mean is the
        # fallback when the results do not carry the reference nodes.
        pos = {int(n): i for i, n in enumerate(un)}
        bolts = {}
        for b in info["bolts"]:
            hn = [pos[n] for n in b.get("head_nodes", []) if n in pos]
            nn = [pos[n] for n in b.get("nut_nodes", []) if n in pos]
            axis = np.asarray(b["axis"], float)
            if b.get("head_node") in pos and b.get("nut_node") in pos:
                elong = float(np.dot(uv[pos[b["head_node"]], :3] - uv[pos[b["nut_node"]], :3], axis))
                bolts[b["fastener"]] = {"preload": b["preload"], "elongation": elong,
                                        "force": b["preload"] + b["k"] * elong, "stiffness": b["k"]}
            elif hn and nn:
                elong = float(np.dot(uv[hn, :3].mean(axis=0) - uv[nn, :3].mean(axis=0), axis))
                bolts[b["fastener"]] = {"preload": b["preload"], "elongation": elong,
                                        "force": b["preload"] + b["k"] * elong, "stiffness": b["k"]}
            else:
                bolts[b["fastener"]] = {"preload": b["preload"], "force": None}
        out["bolts"] = bolts
    if info.get("springs"):
        tot = np.zeros(3)
        for pid, s in info["springs"].items():
            sel = (un > s["node0"]) & (un <= s["node0"] + s["nodes"])
            tot += s["k_per_node"] * uv[sel, :3].sum(axis=0)
        share = float(np.linalg.norm(tot) / max(np.linalg.norm(applied), 1e-12))
        out["stabilising_spring_share"] = share
        out["stabilising_spring_force"] = (-tot).tolist()     # what the springs exert on the parts
        if share > defaults.STAB_SPRING_WARN:
            d = make("STAB_SPRING_LOAD", where={"parts": sorted(info["springs"])}, parts=", ".join(sorted(info["springs"])),
                     share=share, case=case, evidence={"spring_force": tot.tolist()})
            if share > defaults.STAB_SPRING_BLOCK:
                d.severity = "blocking"
            diags.append(d)
    return out


def _openings(contacts: list[dict], un: np.ndarray, uv: np.ndarray, coords) -> dict:
    """Contact opening from displacements: each slave node against its nearest master node
    (the faces touch before loading), relative motion along the master face's outward
    normal. Positive = open. Returns max_opening, open_fraction and the value per pair."""
    if coords is None or not any(c.get("slave_nodes") for c in contacts):
        return {}
    from scipy.spatial import cKDTree
    cid, cxyz = coords
    cpos = {int(n): i for i, n in enumerate(cid)}
    upos = {int(n): i for i, n in enumerate(un)}
    per_pair, gaps = {}, []
    for c in contacts:
        S = [n for n in c.get("slave_nodes", []) if n in cpos and n in upos]
        M = [n for n in c.get("master_nodes", []) if n in cpos and n in upos]
        if not S or not M:
            continue
        _, j = cKDTree(cxyz[[cpos[n] for n in M], :3]).query(cxyz[[cpos[n] for n in S], :3])
        rel = uv[[upos[n] for n in S], :3] - uv[[upos[M[i]] for i in j], :3]
        g = rel @ np.asarray(c["master_normal"], float)
        per_pair[c["connection"]] = float(g.max())
        gaps.append(g)
    if not gaps:
        return {}
    g = np.concatenate(gaps)
    return {"max_opening": float(g.max()), "open_fraction": float(np.mean(g > defaults.CONTACT_OPEN_TOL)),
            "opening_by_pair": per_pair}


def buckling(deck_info: dict, dat_path: Path) -> tuple[dict, list[Diagnostic]]:
    """Buckling factors per buckling case (the .dat holds one block per *BUCKLE step)."""
    cases = [s["case"] for s in deck_info.get("steps", []) if s["kind"] == "buckle"]
    if not cases:
        return {}, []
    blocks = read_dat_buckling(dat_path) if dat_path.exists() else []
    out, diags = {}, []
    for case, step_info, factors in zip(cases, [s for s in deck_info["steps"] if s["kind"] == "buckle"], blocks):
        lowest = min(factors, key=abs) if factors else None
        out[case] = {"factors": factors, "lowest": lowest, "step": step_info["step"],
                     "scaled_loads": deck_info["buckling"][case]["scaled"],
                     "preloads": deck_info["buckling"][case]["preload"]}
        if lowest is not None and lowest < 1.0:
            diags.append(make("BUCKLING_FACTOR_LOW", where={"loads": deck_info["buckling"][case]["scaled"]},
                              case=case, factor=lowest, evidence={"factors": factors}))
    return out, diags


def _rel(a: float, b: float) -> float:
    """Relative change from base a to refined b."""
    ref = max(abs(a), abs(b))
    return abs(b - a) / ref if ref > 0 else 0.0


def convergence(base: dict, refined: dict) -> tuple[dict, list[Diagnostic]]:
    """Change in max displacement and peak stress per part between the two meshes.

    The check uses the stress peak excluding tie boundaries; the raw peak is reported
    too but sits on singular points (tie edges, re-entrant corners) that never converge.
    """
    out, worst, offenders, singular = {}, 0.0, {}, {}
    for case, r in refined["load_cases"].items():
        b = base["load_cases"].get(case)
        if b is None:
            continue
        rows = {}
        for part, p in r["parts"].items():
            q = b["parts"].get(part)
            if q is None:
                continue
            d = {"max_disp": _rel(q["max_disp"], p["max_disp"]),
                 "peak_vm_excl_ties": _rel(q["peak_vm_excl_ties"], p["peak_vm_excl_ties"]),
                 "peak_vm_raw": _rel(q["peak_vm"], p["peak_vm"])}
            rows[part] = d
            # deflection settled but the peak keeps rising with refinement: a singular point (D32)
            rise = (p["peak_vm_excl_ties"] - q["peak_vm_excl_ties"]) / max(q["peak_vm_excl_ties"], 1e-30)
            if d["max_disp"] <= defaults.CONVERGENCE_TOL and rise > defaults.SINGULAR_RISE:
                p["singular"] = True
                singular.setdefault(part, []).append(f"{case}: +{rise:.0%} at {p.get('peak_vm_excl_ties_at')}")
                d["singular"] = True
                continue
            w = max(d["max_disp"], d["peak_vm_excl_ties"])
            worst = max(worst, w)
            if w > defaults.CONVERGENCE_TOL:
                offenders.setdefault(part, []).append(f"{case}: {w:.1%}")
        out[case] = {"global_max_disp": _rel(b["max_disp"], r["max_disp"]), "parts": rows}
    for case, bk in refined.get("buckling", {}).items():
        q = base.get("buckling", {}).get(case)
        if q and q.get("lowest") and bk.get("lowest"):
            ch = _rel(q["lowest"], bk["lowest"])
            out.setdefault(f"buckling:{case}", {})["lowest_factor"] = ch
            worst = max(worst, ch)
            if ch > defaults.CONVERGENCE_TOL:
                offenders.setdefault(f"buckling {case}", []).append(f"{ch:.1%}")
    diags = []
    if singular:
        diags.append(make("STRESS_SINGULAR", where={"parts": sorted(singular)}, parts=", ".join(sorted(singular)),
                          rise=defaults.SINGULAR_RISE, evidence={"by_part": singular}))
    if offenders:
        diags.append(make("CONVERGENCE_POOR", where={"parts": sorted(offenders)}, worst=worst,
                          tol=defaults.CONVERGENCE_TOL, parts=", ".join(sorted(offenders)),
                          evidence={"by_part": offenders}))
    return {"worst_change": worst, "by_case": out}, diags


# What each check means and what to do when it fails, for the task pane's More Info (the user's request) and the
# report. {case}, {part}, {tol} are filled in per row.
CHECK_TEXT = {
    "reaction": ("The forces at the supports add up to the loads applied, to within {tol}. This proves the solve "
                 "is sound: no load is lost through a part that is not connected, and nothing is missing.",
                 ["If it fails: look for parts that are not connected (Connections tab, exceptions) or loads applied "
                  "on fixed faces."]),
    "deflection": ("The largest displacement anywhere in the model under load case {case}, found in {part}, against "
                   "the maximum deflection set under Pass / fail criteria on the Study tab.",
                   ["If it fails: stiffen the part that moves most (a deeper section, a brace, a shorter span) or "
                    "relax the limit if it was too strict."]),
    "fos": ("The lowest factor of safety under load case {case}, in {part}: the material's yield strength divided by "
            "the von Mises stress. Away from bonded joints the peak stress is used; at bonded joints the structural "
            "(membrane plus bending) stress, so the spikes at sharp tie edges, which are not real, do not count. "
            "Compared with the minimum set under Pass / fail criteria on the Study tab.",
            ["If it fails: look at the stress plot for where it peaks, then strengthen there (thicker, a larger "
             "section, a gusset or a bigger weld) or use a stronger material.",
             "A peak marked singular is at a sharp corner or edge where stress has no finite value: refine there "
             "or judge it by the stress just away from it."]),
    "stress": ("The highest von Mises stress under load case {case}, away from bonded joints, in {part}, against "
               "the allowable stress set under Pass / fail criteria on the Study tab.",
               ["If it fails: strengthen where the stress plot peaks, or use a material with a higher allowable."]),
    "buckling": ("The lowest buckling load factor for load case {case}: how many times the load the structure can "
                 "carry before it buckles. Below 1 it buckles under the load as applied.",
                 ["If it fails: brace the member that buckles (see the buckling mode plot), shorten its free "
                  "length or use a stiffer section."]),
    "convergence": ("How much the results changed between the base mesh and a finer one. Under {tol} the mesh is "
                    "fine enough for these results; more means the results depend on the mesh.",
                    ["If it fails: optimise for Accuracy (a finer mesh), or look at the part named in the "
                     "convergence warning."]),
    "mesh": ("Parts that could not be meshed at all. They are not in the analysis.",
             ["See the mesh warnings: usually a sliver face or a gap in the part's geometry; fix it in SolidWorks."]),
    "floating": ("Parts that are not connected to anything that holds them, so they would fly off under load.",
                 ["Connect them (a touching face, a mate or a weld), or fix them, on the Connections tab."]),
    "exceptions": ("Questions AsmFEA could not decide for itself (an ambiguous connection, a gap or an overlap) "
                   "that are still open.",
                   ["Review them on the Connections tab (Show exceptions only) and record a decision."]),
}


def checks(model: Model, results: dict, open_exceptions: int, mesh_failed: int = 0,
           floating: int = 0) -> list[dict]:
    """PASS / WARN / FAIL rows with measured value and limit, and for More Info what each means (`explain`), where
    it governs (`where`: load case and part) and what to do (`actions`)."""
    rows = []
    lim = model.study.limits if model.study else None

    def row(name, value, limit, ok, bad="WARN", kind=None, case=None, part=None, tol=None):
        r = {"check": name, "value": value, "limit": limit, "status": "PASS" if ok else bad}
        if kind:
            text, actions = CHECK_TEXT[kind]
            r["explain"] = text.format(case=case or "-", part=part or "-", tol=tol or "-")
            r["actions"] = actions
            r["where"] = {"load_case": case, "parts": [part] if part else []}
        rows.append(r)

    def worst(parts: dict, key: str, low: bool):
        vals = [(p[key], name) for name, p in parts.items() if p.get(key) is not None]
        return (min(vals) if low else max(vals))[1] if vals else None

    for case, r in results["load_cases"].items():
        b = r["reaction_balance_err"]
        row(f"Reaction balance [{case}]", b, defaults.REACTION_BALANCE_TOL,
            b is not None and b <= defaults.REACTION_BALANCE_TOL, kind="reaction", case=case,
            tol=f"{defaults.REACTION_BALANCE_TOL:.1%}")
        if lim and lim.max_deflection:
            row(f"Max deflection [{case}]", r["max_disp"], lim.max_deflection,
                r["max_disp"] <= lim.max_deflection, "FAIL", kind="deflection", case=case,
                part=worst(r["parts"], "max_disp", False))
        if lim and lim.min_fos:
            # members away from joints, and bonded joints by their structural stress (D33)
            fos = [(p["min_fos"], bool(p.get("singular")), n) for n, p in r["parts"].items() if p["min_fos"]]
            fos += [(p["joint_fos"], False, n) for n, p in r["parts"].items() if p.get("joint_fos")]
            v, sing, where = min(fos) if fos else (None, False, None)
            # a governing peak flagged singular (D32) is not a real stress: a warning, not a fail
            row(f"Min factor of safety [{case}]" + (" (singular peak, see warning)" if sing else ""), v, lim.min_fos,
                v is not None and v >= lim.min_fos, "WARN" if sing else "FAIL", kind="fos", case=case, part=where)
        if lim and lim.max_stress:
            # the peak away from bonded joints, as min_fos uses (D53); a singular peak warns, as there
            pk = [(p["peak_vm_excl_ties"], bool(p.get("singular")), n) for n, p in r["parts"].items()
                  if p.get("peak_vm_excl_ties") is not None]
            v, sing, where = max(pk) if pk else (None, False, None)
            row(f"Max stress [{case}]" + (" (singular peak, see warning)" if sing else ""), v, lim.max_stress,
                v is not None and v <= lim.max_stress, "WARN" if sing else "FAIL", kind="stress", case=case, part=where)
    for case, bk in results.get("buckling", {}).items():
        lim_b = lim.min_buckling_factor if lim and lim.min_buckling_factor else 1.0
        row(f"Lowest buckling factor [{case}]", bk["lowest"], lim_b,
            bk["lowest"] is not None and bk["lowest"] >= lim_b, "FAIL", kind="buckling", case=case)
    conv = results.get("convergence")
    if conv is not None:
        row("Convergence between the two solves", conv["worst_change"], defaults.CONVERGENCE_TOL,
            conv["worst_change"] <= defaults.CONVERGENCE_TOL, kind="convergence", tol=f"{defaults.CONVERGENCE_TOL:.0%}")
    row("Parts that failed to mesh", mesh_failed, 0, mesh_failed == 0, "FAIL", kind="mesh")
    row("Floating parts", floating, 0, floating == 0, "FAIL", kind="floating")
    row("Open exceptions", open_exceptions, 0, open_exceptions == 0, kind="exceptions")
    return rows
