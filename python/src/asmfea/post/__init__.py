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
              singular_by_imbalance: bool = True,
              edge_nodes: dict[str, np.ndarray] | None = None,
              load_nodes: dict[str, np.ndarray] | None = None) -> tuple[dict, list[Diagnostic]]:
    """tie_nodes: per part, global ids of nodes within the joint band (a part wholly inside is not assessed).
    edge_nodes: per part, the joint edges (the elements on a joint's edge line), left out of the peak; the band
    when not given. load_nodes: per part, the bands round the edges of loaded faces (D87), left out of the peak too.
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
        # each part's nodes by binary search in the ids sorted once (D93: a mask over every node for each part
        # was most of the summary of a 174-part frame); the results' ids are sorted already, so nothing moves
        s_ord = np.argsort(sn, kind="stable")
        sn_s, vm_s = sn[s_ord], vm[s_ord]
        u_ord = np.argsort(un, kind="stable")
        un_s, umag_s = un[u_ord], umag[u_ord]
        parts = {}
        for inst in deck_info["instances"]:
            lo, hi = inst["node0"] + 1, inst["node0"] + inst["n_nodes"]
            a, b = np.searchsorted(sn_s, [lo, hi + 1])
            if a == b:
                continue
            ids, v = sn_s[a:b], vm_s[a:b]
            i = int(np.argmax(v))
            none = np.empty(0, dtype=np.int64)
            in_band = np.isin(ids, tie_nodes.get(inst["part"], none))
            excl = in_band if edge_nodes is None else np.isin(ids, edge_nodes.get(inst["part"], none))
            if load_nodes:                                  # a load face's edge: the load's idealisation (D87)
                excl = excl | np.isin(ids, load_nodes.get(inst["part"], none))
            # a part lying wholly within joint bands (a bush between hinges) has no stress to assess: its peaks
            # are all at its joints, so it gives no factor of safety (bearing on bushes and pins is a hand check);
            # its raw peak stays for reference. It used to fall back to the raw peak and governed (2026-10-09)
            joint_only = not (~in_band).any() or not (~excl).any()
            j = i if joint_only else int(np.argmax(np.where(excl, -np.inf, v)))
            mat = model.material(inst["material"])
            ua, ub = np.searchsorted(un_s, [lo, hi + 1])
            has_u = ub > ua
            ju = int(u_ord[ua + int(np.argmax(umag_s[ua:ub]))]) if has_u else 0
            parts[inst["part"]] = {
                "peak_vm": float(v[i]), "peak_vm_node": int(ids[i]), "peak_vm_at": at(ids[i]),
                "peak_vm_excl_ties": None if joint_only else float(v[j]),
                "peak_vm_excl_ties_at": None if joint_only else at(ids[j]),
                "joint_only": joint_only,
                "min_fos": mat.yield_ / float(v[j]) if v[j] > 0 and not joint_only else None,
                # on ultimate too (the user's request, 2026-10-10): the margin left before fracture, beside the one
                # before yield; whether some yielding is acceptable is the engineer's judgement
                "min_fos_ultimate": (mat.ultimate / float(v[j]) if mat.ultimate and v[j] > 0 and not joint_only
                                     else None),
                "max_disp": float(umag[ju]) if has_u else 0.0,
                "max_disp_at": at(un[ju]) if has_u else None,
            }
        # kept apart: they have no stress to assess (D96); their motion from the printed reference nodes
        rigid = rigid_parts(deck_info, dat["nodal"].get(k, {}).get(("displacements", "RGREF"), {}))
        applied = np.asarray(deck_info["load_cases"][case]["applied"])
        # CalculiX's RF omits load applied straight onto constrained DOFs; the deck
        # writer computed that share, so the balance compares like with like.
        supports = np.asarray(deck_info["load_cases"][case].get("into_supports", [0.0, 0.0, 0.0]))
        reaction = dat["totals"].get(k, {}).get("FIXALL")
        # load passed into a pin through a joint on it goes to the pin's reference node, where no reaction is
        # printed: the joint's side B nodes print it instead (deck "pin_joint_sets")
        # ... and the rigid groups' coupled nodes (D96): the force each group exerts on them, less the load on them
        # and the group's own load (both counted in into_supports), is the group's reaction to the ground
        for pset in deck_info.get("pin_joint_sets", []) + deck_info.get("rigid_support_sets", []):
            more = dat["totals"].get(k, {}).get(pset)
            if more is not None:
                reaction = (np.asarray(reaction, float) if reaction is not None else np.zeros(3)) + np.asarray(more, float)
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
            # scaled by the loads' sizes added up, not their resultant: loads that balance each other (two equal and
            # opposite forces, a closed vessel's pressure) have a resultant near zero, and dividing by it made
            # rounding noise read as a singular model (the user, 2026-10-18). Older decks: the resultant.
            gross = deck_info["load_cases"][case].get("applied_gross")
            denom = max(np.linalg.norm(applied), float(gross or 0.0), 1e-12)
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
            **({"rigid_parts": rigid} if rigid else {}),
        }
        far = max(rigid.items(), key=lambda kv: kv[1]["max_disp"], default=None)
        if far is not None and far[1]["max_disp"] > umax:
            # a rigid part moving further than any meshed point (a long rigid arm): the model's largest displacement
            out_cases[case].update(max_disp=far[1]["max_disp"], max_disp_at=far[1]["max_disp_at"], max_disp_part=far[0],
                                   max_disp_vector=far[1]["max_disp_vector"])
    return {"load_cases": out_cases}, diags


def rigid_parts(deck_info: dict, printed: dict) -> dict:
    """Results of the rigid parts (D96): no stress (not meshed: no peak, no factor of safety), and the largest
    displacement of each, its group's rigid motion (the reference node's translation t, the rotation node's turn
    theta) at the corners of its convex hull."""
    from ..deck.rigid import motion_at
    out = {}
    groups = (deck_info.get("rigid") or {}).get("groups", [])
    if not groups:
        return out
    for g in groups:
        t = np.asarray(printed.get(int(g["ref_node"]), np.zeros(3)), float)
        th = np.asarray(printed.get(int(g["rot_node"]), np.zeros(3)), float)
        for pid, d in g.get("parts_data", {}).items():
            X = np.asarray(d.get("hull") or [d["cog"]], float).reshape(-1, 3)
            U = motion_at(t, th, np.asarray(g["at"], float), X)
            m = np.linalg.norm(U, axis=1)
            i = int(np.argmax(m))
            out[pid] = {"rigid_group": g["id"], "max_disp": float(m[i]), "max_disp_at": np.round(X[i], 4).tolist(),
                        "max_disp_vector": U[i].tolist(),
                        "rigid_translation": t.tolist(), "rigid_rotation": th.tolist()}
    return out


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
        # bolt tension = k x the spring's stretch (a pre-tension bolt, D103), or preload + k x elongation (decks
        # written before it, with the preload as a force pair). The reference nodes follow the weighted
        # mean of their bearing annuli (equations in the deck); the annulus mean is the
        # fallback when the results do not carry the reference nodes.
        pos = {int(n): i for i, n in enumerate(un)}
        # the .frd leaves out nodes on no element (a pre-tension bolt's nut reference node and its adjustment):
        # every bolt node is printed to the .dat in every step (BOLTNODES)
        printed = dat.get("nodal", {}).get(step, {}).get(("displacements", "BOLTNODES"), {})
        bolts = {}
        for b in info["bolts"]:
            hn = [pos[n] for n in b.get("head_nodes", []) if n in pos]
            nn = [pos[n] for n in b.get("nut_nodes", []) if n in pos]
            axis = np.asarray(b["axis"], float)
            ends = [b.get(k) for k in ("head_node", "nut_node", "spring_node")]
            if b.get("spring_node") is not None and all(n in printed for n in ends):
                # a pre-tension bolt (D103): the spring runs from the head to its own end node, so its stretch is
                # the tension (exactly the preload after tightening); the elongation stays head to nut
                uh, un_, us = (np.asarray(printed[n], float)[:3] for n in ends)
                elong = float(np.dot(uh - un_, axis))
                stretch = float(np.dot(uh - us, axis))
                bolts[b["fastener"]] = {"preload": b["preload"], "elongation": elong,
                                        "force": b["k"] * stretch, "stiffness": b["k"]}
            elif b.get("spring_node") is not None:
                bolts[b["fastener"]] = {"preload": b["preload"], "force": None}     # never the force-pair formula
            elif b.get("head_node") in pos and b.get("nut_node") in pos:
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


def assessed_stress(p: dict) -> float | None:
    """A part's stress as the factor of safety has it: membrane plus bending where it was classified (D34; at
    joints too, 2026-10-10), else the peak away from the joint edges. None for a part not assessed (joint only)."""
    if p.get("joint_only"):
        return None
    if p.get("singular_fallback"):                       # the read-out rose with the mesh: the steady peak (D88)
        return p.get("peak_vm_excl_ties")
    v = p.get("membrane_bending")
    return v if v is not None else p.get("peak_vm_excl_ties")


def convergence(base: dict, refined: dict) -> tuple[dict, list[Diagnostic]]:
    """Change in max displacement and assessed stress per part between the two meshes.

    The stress graded is the one the factor of safety uses (assessed_stress: membrane plus bending), not the peak:
    a peak at a joint's sharp edge or a re-entrant corner has no finite value and moves with every mesh (20.6 % on
    the folding step's plates, worse on the finest mesh; the user's run, 2026-10-10). The peaks' own changes are
    kept for reference (peak_vm_excl_ties, peak_vm_raw).
    """
    out, worst, offenders, singular = {}, 0.0, {}, {}
    # a load case whose stresses are a tiny share of the largest case's is not graded: its changes between the meshes
    # are rounding noise on next to nothing (two balanced loads, a case of 0.04 MPa beside one of 13 MPa changed
    # "24 %" and sent every part to a third, local pass; the user, 2026-10-18, D131)
    case_vm = {case: max([v for lc_ in (r, base["load_cases"].get(case) or {}) for x in (lc_.get("parts") or {}).values()
                          if (v := assessed_stress(x)) is not None] or [0.0])
               for case, r in refined["load_cases"].items()}
    top_vm = max(case_vm.values(), default=0.0)
    for case, r in refined["load_cases"].items():
        b = base["load_cases"].get(case)
        if b is None:
            continue
        rows = {}
        negligible = len(case_vm) > 1 and top_vm > 0 and case_vm[case] < defaults.CONVERGENCE_NEGLIGIBLE_CASE * top_vm
        # a part's displacement change is measured against the model's largest displacement, not the part's own:
        # a small part that barely moves (a plastic bush on a hinge) changed by 68 % of its own 0.01 mm and made a
        # whole folding step "Indicative", while every structural part had settled within 6 % (the user's
        # observation, 2026-10-09: the deflections were right). The part's own change is kept as max_disp_own
        model_disp = max(abs(b["max_disp"]), abs(r["max_disp"]), 1e-30)
        # and a part's stress change against the model's highest assessed stress, for the grade: a step plate at
        # 20 MPa (factor of safety 10) changed by 20 % (4 MPa) while the governing 125 MPa cheek changed 4 %; the
        # part's own change is kept (stress) and still finds singular points
        model_vm = max([v for lc_ in (b, r) for x in lc_["parts"].values()
                        if (v := assessed_stress(x)) is not None] or [1e-30])
        for part, p in r["parts"].items():
            q = b["parts"].get(part)
            if q is None:
                continue
            sp, sq = assessed_stress(p), assessed_stress(q)
            assessed = sp is not None and sq is not None
            peaks = p.get("peak_vm_excl_ties") is not None and q.get("peak_vm_excl_ties") is not None
            d = {"max_disp": abs(p["max_disp"] - q["max_disp"]) / model_disp,
                 "max_disp_own": _rel(q["max_disp"], p["max_disp"]),
                 # a part wholly within joint bands has no assessed stress to converge (see results)
                 "stress": _rel(sq, sp) if assessed else 0.0,
                 "stress_model": abs(sp - sq) / model_vm if assessed else 0.0,
                 "peak_vm_excl_ties": _rel(q["peak_vm_excl_ties"], p["peak_vm_excl_ties"]) if peaks else 0.0,
                 "peak_vm_raw": _rel(q["peak_vm"], p["peak_vm"])}
            rows[part] = d
            if negligible:
                continue                                 # noise on a case that carries next to nothing
            # deflection settled but the stress keeps rising with refinement: a singular point (D32)
            rise = (sp - sq) / max(sq, 1e-30) if assessed else 0.0
            if d["max_disp"] <= defaults.CONVERGENCE_TOL and rise > defaults.SINGULAR_RISE:
                at = p.get("membrane_bending_at") or p.get("peak_vm_excl_ties_at")
                note = f"{case}: +{rise:.0%} at {at}"
                d["singular"] = True
                # the read-out rose with the mesh, so it has no settled value: where the part's peak away from the
                # joint and load edges held steady, the factor of safety uses that instead (the user's decision,
                # D88). A pinned cradle frame's governing tube read 82 then 135 MPa beside its joint, its steady peak 90.9
                # then 89.5. Otherwise the read-out stays, flagged singular (a warning, not a fail)
                pk_p, pk_q = p.get("peak_vm_excl_ties"), q.get("peak_vm_excl_ties")
                if peaks and pk_p and pk_p > 0 and _rel(pk_q, pk_p) <= defaults.CONVERGENCE_TOL:
                    scale = sp / pk_p
                    p.update(singular_fallback=True, read_out_singular=sp, read_out_singular_at=at)
                    if p.get("min_fos"):
                        p["min_fos"] *= scale
                    if p.get("min_fos_ultimate"):
                        p["min_fos_ultimate"] *= scale
                    note += f"; factor of safety from the steady peak away from the edges, {pk_p:.1f} MPa"
                    d["stress"] = _rel(pk_q, pk_p)
                    d["stress_model"] = abs(pk_p - pk_q) / model_vm
                    singular.setdefault(part, []).append(note)
                    w = max(d["max_disp"], d["stress_model"])
                    worst = max(worst, w)
                    if w > defaults.CONVERGENCE_TOL:
                        offenders.setdefault(part, []).append(f"{case}: {w:.1%}")
                    continue
                p["singular"] = True
                singular.setdefault(part, []).append(note)
                continue
            w = max(d["max_disp"], d["stress_model"])
            worst = max(worst, w)
            if w > defaults.CONVERGENCE_TOL:
                offenders.setdefault(part, []).append(f"{case}: {w:.1%}")
        out[case] = {"global_max_disp": _rel(b["max_disp"], r["max_disp"]), "parts": rows}
        if negligible:
            out[case]["negligible"] = True
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


# Poor convergence on the finest fidelity already (Optimise for Accuracy): "use a finer mesh" is no help there (the
# user's point, 2026-10-10)
CONVERGENCE_AT_FINEST = [
    "The study is already on the finest mesh (Optimise for Accuracy). Look at the parts named in the convergence "
    "warning: the change is usually at one place, a joint or a notch where the elements are large next to the wall "
    "or the detail.",
    "The refined mesh's result is the one reported, and it is the more accurate of the two. Where the stress there "
    "rose on the finer mesh, the true value may be somewhat higher again: allow for the change shown before relying "
    "on a small margin.",
    "If the place is a sharp corner, a fillet in the model (as made) settles it: a sharp corner has no finite stress.",
]


# What each check means and what to do when it fails, for the task pane's More Info (the user's request) and the
# report. {case}, {part}, {tol} are filled in per row.
CHECK_TEXT = {
    "reaction": ("The forces at the supports add up to the loads applied, to within {tol}. This proves the solve "
                 "is sound: no load is lost through a part that is not connected, and nothing is missing.",
                 ["If it fails: look for parts that are not connected (Connections tab, exceptions) or loads applied "
                  "on fixed faces."]),
    "deflection": ("The largest displacement anywhere in the model under load case {case}, found in {part}, against "
                   "the maximum deflection set under How to solve and pass/fail criteria on the Run tab.",
                   ["If it fails: stiffen the part that moves most (a deeper section, a brace, a shorter span) or "
                    "relax the limit if it was too strict."]),
    "fos": ("The lowest factor of safety under load case {case}, in {part}: the material's yield strength divided by "
            "the von Mises stress of membrane plus bending: the stress averaged through the wall plus its linear "
            "part, everywhere including round the joints. Local peaks (notches, the spikes at sharp joint edges, "
            "which have no finite value in a model with sharp corners) are left out, as static design codes do. "
            "Compared with the minimum set under How to solve and pass/fail criteria on the Run tab.",
            ["If it fails: look at the stress plot for where it peaks, then strengthen there (thicker, a larger "
             "section, a gusset or a bigger weld) or use a stronger material.",
             "A peak marked singular is at a sharp corner or edge where stress has no finite value: refine there "
             "or judge it by the stress just away from it."]),
    "fos_ultimate": ("The lowest factor of safety on ultimate strength under load case {case}, in {part}: the material's "
                     "ultimate tensile strength divided by the same assessed stress (membrane plus bending). It shows the "
                     "margin left before fracture beside the one before yield; for information, with no pass or fail. "
                     "The analysis is linear: past yield its stresses are no longer the real ones, so this is an index of "
                     "the reserve, not a predicted failure load (on the safe side for ductile metals).",
                     ["Whether some yielding before that is acceptable is the engineer's judgement: compare the factor "
                      "on yield (no permanent set) with this one (no fracture)."]),
    "joint_fos": ("The lowest factor of safety at a bonded joint by its structural stress under load case {case}, in "
                  "{part}: the yield strength divided by the stress the joint's line force and moment put into the wall "
                  "at a member's end (the measure weld fatigue methods use). For information, with no pass or fail: "
                  "the static check above uses the stress beside the joint, as static design codes do; this one is the "
                  "starting point for a weld or fatigue check.",
                  ["Check the weld at the named part's end by hand (throat size, EN 1993-1-8), or with a fatigue "
                   "assessment if the load cycles."]),
    "stress": ("The highest von Mises stress of membrane plus bending under load case {case}, in {part}, against "
               "the allowable stress set under How to solve and pass/fail criteria on the Run tab.",
               ["If it fails: strengthen where the stress plot peaks, or use a material with a higher allowable."]),
    "buckling": ("The lowest buckling load factor for load case {case}: how many times the load the structure can "
                 "carry before it buckles. Below 1 it buckles under the load as applied.",
                 ["If it fails: brace the member that buckles (see the buckling mode plot), shorten its free "
                  "length or use a stiffer section."]),
    "convergence": ("How much the results changed between the base mesh and a finer one: deflections, and the "
                    "assessed stress (membrane plus bending, the factor of safety's own). Under {tol} the mesh is "
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


def convergence_matters(rows: list, change: float) -> bool:
    """Whether a change of ``change`` (a fraction) in the results could turn any limit check: a factor of safety or
    buckling factor within that share above its limit, a deflection or stress within it below its limit (D132). A
    10.6 % change on a part with a factor of safety of 12 against 1.5 asks nothing of the user, so it is a note, not
    a warning (the user, 2026-10-18: "I found a problem and fixed it... I didn't need to know")."""
    for r in rows:
        v, lim, kind = r.get("value"), r.get("limit"), r.get("kind")
        if v is None or lim is None or not isinstance(v, (int, float)) or not isinstance(lim, (int, float)):
            continue
        if kind in ("fos", "buckling") and v / (1 + change) < lim:
            return True
        if kind in ("deflection", "stress") and v * (1 + change) > lim:
            return True
    return False


def checks(model: Model, results: dict, open_exceptions: int, mesh_failed: int = 0,
           floating: int = 0, fidelity: str | None = None) -> list[dict]:
    """PASS / WARN / FAIL rows with measured value and limit, and for More Info what each means (`explain`), where
    it governs (`where`: load case and part) and what to do (`actions`)."""
    rows = []
    lim = model.study.limits if model.study else None

    def row(name, value, limit, ok, bad="WARN", kind=None, case=None, part=None, tol=None):
        # ok None: for information only, no pass or fail (status INFO)
        r = {"check": name, "value": value, "limit": limit, "status": "INFO" if ok is None else "PASS" if ok else bad}
        if kind:
            r["kind"] = kind
            text, actions = CHECK_TEXT[kind]
            if kind == "convergence" and fidelity == "fine":
                actions = CONVERGENCE_AT_FINEST          # a finer mesh is not on offer: say what to do instead
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
            # the parts' assessed stress (membrane plus bending, beside the joints too). The joints' structural stress
            # (D33) is a weld and fatigue measure: shown below for information, not in the static check (D83)
            fos = [(p["min_fos"], bool(p.get("singular")), n) for n, p in r["parts"].items() if p["min_fos"]]
            v, sing, where = min(fos) if fos else (None, False, None)
            # a governing peak flagged singular (D32) is not a real stress: a warning, not a fail
            row(f"Min factor of safety [{case}]" + (" (singular peak, see warning)" if sing else ""), v, lim.min_fos,
                v is not None and v >= lim.min_fos, "WARN" if sing else "FAIL", kind="fos", case=case, part=where)
        jf = [(p["joint_fos"], n) for n, p in r["parts"].items() if p.get("joint_fos")]
        if jf:
            v, where = min(jf)
            row(f"Joint structural stress factor [{case}]", v, None, None, kind="joint_fos", case=case, part=where)
        # on ultimate too, for information: the margin before fracture; whether some yield is acceptable is the
        # engineer's judgement (the user's request, 2026-10-10)
        fu = [(p["min_fos_ultimate"], n) for n, p in r["parts"].items() if p.get("min_fos_ultimate")]
        if fu:
            v, where = min(fu)
            row(f"Min factor of safety on ultimate [{case}]", v, None, None, kind="fos_ultimate", case=case, part=where)
        if lim and lim.max_stress:
            # the stress min_fos uses (membrane plus bending, at joints too; D53, 2026-10-10); a singular one warns
            pk = [(v, bool(p.get("singular")), n) for n, p in r["parts"].items()
                  if (v := assessed_stress(p)) is not None]
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
    if conv is not None and conv["worst_change"] > defaults.CONVERGENCE_TOL and not convergence_matters(rows, conv["worst_change"]):
        rows[-1]["status"] = "INFO"           # no check's outcome hangs on it (D132)
    row("Parts that failed to mesh", mesh_failed, 0, mesh_failed == 0, "FAIL", kind="mesh")
    row("Floating parts", floating, 0, floating == 0, "FAIL", kind="floating")
    row("Open exceptions", open_exceptions, 0, open_exceptions == 0, kind="exceptions")
    return rows
