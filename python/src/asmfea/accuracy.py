"""How accurate a run's results are likely to be, from how it was solved (D60): graded High, Good or Indicative,
with expected displacement and stress errors and the reasons, for the results, the task pane and the reports.

Every figure comes from a measurement, named beside it:
- CalculiX on the standard mesh against theory: C1 cantilever tip +0.4 %, C3 tied cantilever +0.7 %, bending
  stress 0.00 % (the verification report, D25);
- the hybrid solver against CalculiX on the same decks: exact joints within 0.12 % (D59: m4_frame, m11 weldments,
  m3 bolted bracket and portal frame); joints by smooth fields 1-3 % away from joints (D49) and up to 10 % at
  welded frame joints (D59);
- a run's own convergence check (base against refined mesh, D12), which bounds the mesh error of that model;
- thin walls meshed with tets are stiffer in local bending (milestone 0: up to 3.6 times at one element through
  the wall), so local stresses there are less certain;
- a Fast run's sizes are 2.5 x the standard ones (FAST_FIDELITY_FACTOR) with no convergence check: approximate.

The wording lives here, like the report's assumptions, so the pane and the reports say the same thing."""
from __future__ import annotations

from typing import Optional

from . import defaults

HIGH, GOOD, INDICATIVE = "High", "Good", "Indicative"
_ORDER = {HIGH: 0, GOOD: 1, INDICATIVE: 2}


def _worse(a: str, b: str) -> str:
    return a if _ORDER[a] >= _ORDER[b] else b


def _conv_split(conv: Optional[dict]) -> tuple[Optional[float], Optional[float]]:
    """(worst displacement change, worst stress change away from joints) between the two meshes."""
    if not conv:
        return None, None
    disp = stress = 0.0
    for case in (conv.get("by_case") or {}).values():         # post.convergence: {case: {"parts": {part: {...}}}}
        for d in (case.get("parts") or {}).values():
            if d.get("singular"):
                continue
            disp = max(disp, d.get("max_disp") or 0.0)
            # the assessed stress (membrane plus bending) against the model's highest (post.convergence); older
            # results graded the peak, against the model's or the part's own
            stress = max(stress, d.get("stress_model", d.get("peak_vm_model", d.get("peak_vm_excl_ties"))) or 0.0)
    if disp == 0.0 and stress == 0.0 and conv.get("worst_change") is not None:
        disp = stress = float(conv["worst_change"])
    return disp, stress


EXPECTED = {
    "speed": "Expected accuracy: Good for displacements (about 3 %), Indicative for local stresses: a coarse mesh and "
             "no convergence check. Quick, for comparing designs.",
    "balanced": "Expected accuracy: High (displacements within about 1 %, stresses away from joints within about "
                "3 %) when the convergence check agrees; the results say whether it did.",
    "accuracy": "Expected accuracy: High, with a finer mesh for local stresses (fillets, holes, joints); the "
                "convergence check confirms it, and parts that have not settled are meshed finer again. Slower.",
    "fast": "Fast run: approximate, for iterative design only (no report).",
}


def expected(favour: str, fast: bool = False, convergence: Optional[bool] = None, fidelity: Optional[str] = None) -> str:
    """What a run planned this way is likely to give, before it runs (the plan display). An override of the
    convergence check or mesh level changes what can be promised."""
    if fast:
        return EXPECTED["fast"]
    if convergence is False and favour != "speed":
        return ("Expected accuracy: Good at best: the convergence check is turned off (Advanced), so the mesh error "
                f"is not measured{' on a coarse mesh' if fidelity == 'coarse' else ''}.")
    return EXPECTED[favour]


def grade(run_mode: str, fidelity: Optional[str], conv: Optional[dict], runs: list[dict],
          fallback_parts: list[str], favour: Optional[str] = None) -> dict:
    """{"grade", "displacement_pct", "stress_pct", "reasons": [...], "summary"}: expected errors are about how far
    the results may be from the exact answer for this model as defined (loads, connections, materials as set),
    not from the real structure."""
    reasons = []
    g = HIGH
    disp_err, stress_err = 1.0, 3.0                  # CalculiX, standard mesh: verification report (D25)
    if run_mode == "fast":
        g = INDICATIVE
        disp_err, stress_err = 10.0, 25.0
        reasons.append("Fast run: a coarse mesh (2.5 x the standard element size) and no convergence check. "
                       "Use for comparing designs, not for sign-off.")
    elif fidelity == "coarse":
        g = _worse(g, GOOD)
        disp_err, stress_err = 3.0, 10.0
        reasons.append("Coarse mesh (optimised for Speed): displacements are reliable, local stresses less so.")
    solvers = {r.get("solver", "calculix") for r in runs}
    if "hybrid" in solvers:
        ex = sum((r.get("solver_info") or {}).get("joints_exact", 0) for r in runs)
        fi = sum((r.get("solver_info") or {}).get("joints_fields", 0) for r in runs)
        if fi == 0:
            reasons.append(f"Hybrid solver (the model was too large for CalculiX on this computer), every joint "
                           f"joined exactly ({ex}): within about 0.1 % of CalculiX on our reference models.")
        else:
            share = fi / max(ex + fi, 1)
            g = _worse(g, GOOD if share < 0.5 else INDICATIVE)
            disp_err = max(disp_err, 3.0 if share < 0.5 else 10.0)
            stress_err = max(stress_err, 10.0)
            reasons.append(f"Hybrid solver: {fi} of {ex + fi} joints joined by smooth fields (large joints, to keep "
                           "memory in bounds). Those joints can be up to 10 % too flexible at welded frame joints; "
                           "stresses right at them are less certain. Optimising for Accuracy, or a computer with more memory, "
                           "joins more of them exactly.")
    d_ch, s_ch = _conv_split(conv)
    if d_ch is not None:
        tol = defaults.CONVERGENCE_TOL
        disp_err = max(disp_err, 100 * d_ch)
        stress_err = max(stress_err, 100 * s_ch)
        if d_ch > tol or s_ch > tol:                      # as CONVERGENCE_POOR
            g = _worse(g, INDICATIVE)
            reasons.append(f"The refined mesh changed results by up to {100 * max(d_ch, s_ch):.1f} %: not yet "
                           "settled. Optimise for Accuracy (a finer mesh) before relying on them.")
        elif d_ch > defaults.ACCURACY_HIGH_DISP or s_ch > defaults.ACCURACY_HIGH_STRESS:
            g = _worse(g, GOOD)
            reasons.append(f"The refined mesh changed results by up to {100 * max(d_ch, s_ch):.1f} %.")
        else:
            reasons.append(f"Convergence check passed: the refined mesh changed results by at most "
                           f"{100 * max(d_ch, s_ch):.1f} %.")
    elif run_mode != "fast":
        g = _worse(g, GOOD)
        reasons.append("No convergence check (optimised for Speed, or turned off): the mesh error is not measured.")
    if fallback_parts:
        g = _worse(g, GOOD)
        stress_err = max(stress_err, 10.0)
        names = ", ".join(fallback_parts[:4]) + (f" and {len(fallback_parts) - 4} more" if len(fallback_parts) > 4 else "")
        reasons.append(f"Thin walls meshed with tetrahedra in {names}: local bending stresses there are less "
                       "certain (tets are stiffer in thin-wall bending).")
    summary = (f"Accuracy: {g}. Displacements within about {disp_err:.0f} %, stresses away from joints within "
               f"about {stress_err:.0f} %, for the model as defined.")
    return {"grade": g, "displacement_pct": round(disp_err, 1), "stress_pct": round(stress_err, 1),
            "reasons": reasons, "summary": summary, "favour": favour}
