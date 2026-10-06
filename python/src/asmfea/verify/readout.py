"""Read-outs for the verification cases (PLAN-VERIFICATION-REPORT "How each case is run", step 4).

Every value is a nodal average over a node set defined by coordinates in the assembly frame (mm), read
from the reported mesh of the last solved run: CalculiX's nodal results (stresses extrapolated from
the integration points to the nodes and averaged there). Global quantities (mass, reaction, buckling
factor) come from results.json. Each read-out returns its value, the node count and where it was read,
so the report can state exactly how it was measured.
"""
from __future__ import annotations

import json
import string
from pathlib import Path

import numpy as np

from asmfea.post.frd import read_frd

TOL = 0.01      # mm: a node is on a plane when within this


def _coords(inp: Path) -> dict[int, np.ndarray]:
    text = inp.read_text().split("*ELEMENT")[0]
    out = {}
    for line in text.splitlines():
        if line and line[0] in string.digits:
            v = line.split(",")
            out[int(v[0])] = np.array([float(x) for x in v[1:4]])
    return out


class Run:
    """The reported mesh of a study's last solved run: node coordinates and nodal fields per load case."""

    def __init__(self, study: Path):
        self.study = Path(study)
        self.results = json.loads((self.study / "results" / "results.json").read_text())
        job = (self.study / "run" / "latest_solve.txt").read_text().strip()
        self.level = self.results["reported_mesh"]
        self.dir = self.study / "run" / job / self.level
        runs = json.loads((self.dir / "runs.json").read_text())
        rd = self.dir / runs[0]["dir"]
        self.inp = rd / "study.inp"
        xyz = _coords(self.inp)
        self.frd = read_frd(rd / "study.frd")
        info = json.loads((rd / "deck_info.json").read_text())
        self.step_of = {s["case"]: s["step"] for s in info["steps"] if s["kind"] == "static"}
        ids, _ = self.frd[self.step_of[self.results_case()]]["DISP"]
        self.ids = np.asarray(ids, dtype=np.int64)
        self.X = np.array([xyz[int(i)] for i in self.ids])

    def results_case(self) -> str:
        return next(iter(self.results["load_cases"]))

    def field(self, name: str, case: str | None = None) -> np.ndarray:
        """DISP (n x 3) or STRESS (n x 6: xx yy zz xy yz zx), rows in self.ids order."""
        ids, v = self.frd[self.step_of[case or self.results_case()]][name]
        pos = {int(i): k for k, i in enumerate(ids)}
        return np.array([v[pos[int(i)]] for i in self.ids])

    def layer(self, mask: np.ndarray, axis: int, target: float) -> tuple[np.ndarray, float]:
        """Of the nodes in mask, those on the node layer (along axis) closest to target."""
        vals = self.X[mask, axis]
        if len(vals) == 0:
            raise ValueError("no nodes in the read-out region")
        at = float(vals[np.argmin(np.abs(vals - target))])
        return mask & (np.abs(self.X[:, axis] - at) < TOL), at

    def dof(self) -> int:
        return int(self.results["mesh"][self.level]["dof"])


def _out(value, nodes, where, unit):
    return {"value": float(value), "nodes": int(nodes), "where": where, "unit": unit}


# ------------------------------------------------------------------------------------- per case

def cantilever(run: Run, z_stress: float, length: float = 2000.0, half: float = 50.0) -> dict:
    """C1 / C3: tip deflection (-Uy, mean over the tip end face) and bending stress (σzz, mean over the
    flat part of the top outer face, |x| <= 40, on the node layer nearest z_stress)."""
    X, U, S = run.X, run.field("DISP"), run.field("STRESS")
    tip = X[:, 2] > X[:, 2].max() - TOL
    top = (np.abs(X[:, 1] - half) < TOL) & (np.abs(X[:, 0]) <= half - 10.0 + TOL)
    sel, z = run.layer(top, 2, z_stress)
    return {
        "tip_deflection_mm": _out(-U[tip, 1].mean(), tip.sum(),
                                  f"mean -Uy over the tip end face (z = {X[:, 2].max():.1f})", "mm"),
        "bending_stress_MPa": _out(S[sel, 2].mean(), sel.sum(),
                                   f"mean σzz over the top outer face, |x| ≤ 40, y = {half:g}, z = {z:.2f}", "MPa"),
    }


def bar(run: Run, theory: dict) -> dict:
    """C2: mass, total reaction, mid-span deflection (-Uy, mean over the section nearest z = 1000) and
    bottom-fibre stress (σzz, mean over the bottom face, |x| <= 20, on that layer)."""
    X, U, S = run.X, run.field("DISP"), run.field("STRESS")
    allm = np.ones(len(X), bool)
    sec, z = run.layer(allm, 2, 1000.0)
    bottom = np.abs(X[:, 1] + 40.0) < TOL
    bsel, zb = run.layer(bottom, 2, 1000.0)
    lc = run.results["load_cases"][run.results_case()]
    reaction = np.asarray(lc.get("support_reaction") or lc["reaction"])
    return {
        "mass_kg": _out(run.results["mesh"][run.level]["total_mass_t"] * 1000.0, 0, "total mass of the mesh", "kg"),
        "reaction_total_N": _out(reaction[1], 0, "total vertical reaction over both supports (results.json support_reaction)", "N"),
        "midspan_deflection_mm": _out(-U[sec, 1].mean(), sec.sum(), f"mean -Uy over the whole section at z = {z:.2f}", "mm"),
        "midspan_stress_MPa": _out(S[bsel, 2].mean(), bsel.sum(), f"mean σzz over the bottom face (y = -40), z = {zb:.2f}", "MPa"),
    }


def column(run: Run) -> dict:
    """C4: the lowest buckling factor x the 1 kN reference load."""
    bk = next(iter(run.results["buckling"].values()))
    return {"P_cr_N": _out(bk["lowest"] * 1000.0, 0, "lowest buckling factor x 1000 N (results.json)", "N"),
            "buckling_factors": bk["factors"]}
