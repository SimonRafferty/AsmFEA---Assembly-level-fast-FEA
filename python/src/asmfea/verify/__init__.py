"""Verification against classical mechanics (PLAN-VERIFICATION-REPORT.md): `asmfea verify`.

A verification folder (cases/verification) holds one folder per case with:
  case.json      what the case is, where its full-chain study folder is, what to read and compare
  theory.json    the approved classical results (written by theory.py, committed before any run)
  model/         the exported neutral model, for regression runs without SolidWorks
  sw_mass.json   SolidWorks' mass properties of the assembly (optional; the mass check uses it)
  sw_simulation.json   SolidWorks Simulation values supplied by the user (optional)
and solver_checks/results.json (CalculiX's own examples and NAFEMS LE1/LE10, run on the installed build).

`evaluate()` reads each case's results (the full-chain run, or a fresh regression run from model/),
compares them with theory under the plan's acceptance criteria, and `report.build()` writes the report.
"""
from __future__ import annotations

import ast
import json
import operator
import shutil
import time
from pathlib import Path
from typing import Optional

from . import readout

# Acceptance criteria (PLAN-VERIFICATION-REPORT "Acceptance criteria"): |difference| at most this
CRITERIA = {"deflection": 0.05, "stress": 0.05, "buckling": 0.05, "reaction": 0.01, "mass": 0.005}

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _expr(text: str, names: dict) -> float:
    """A theory value: a key of theory.json, or simple arithmetic on keys (e.g. "2 * reaction_each_N")."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.Name):
            return float(names[n.id])
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        raise ValueError(f"not allowed in a theory expression: {text}")
    return ev(ast.parse(text, mode="eval"))


def load_cases(root: Path) -> list[dict]:
    out = []
    for d in sorted(p for p in Path(root).iterdir() if (p / "case.json").exists()):
        c = json.loads((d / "case.json").read_text(encoding="utf-8"))
        c["dir"] = d
        c["theory"] = json.loads((d / "theory.json").read_text(encoding="utf-8"))
        for name in ("sw_mass.json", "sw_simulation.json"):
            f = d / "sw" / name if (d / "sw" / name).exists() else d / name
            c[name[:-5]] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        out.append(c)
    return out


def _read(case: dict, study: Path) -> dict:
    run = readout.Run(study)
    spec = case["readout"]
    kind = spec["kind"]
    if kind == "cantilever":
        values = readout.cantilever(run, spec["z_stress"])
    elif kind == "bar":
        values = readout.bar(run, {})
    elif kind == "column":
        values = readout.column(run)
    else:
        raise ValueError(f"unknown read-out {kind}")
    values.setdefault("mass_kg", readout._out(run.results["mesh"][run.level]["total_mass_t"] * 1000.0, 0,
                                              "total mass of the mesh", "kg"))
    conv = run.results.get("convergence") or {}
    warnings = [w["code"] for w in run.results.get("warnings", [])]
    return {"values": values, "dof": run.dof(), "level": run.level, "study": str(study),
            "job": run.results.get("job"), "convergence_worst": conv.get("worst_change"),
            "convergence_by_case": conv.get("by_case"), "warnings": warnings,
            "images": run.results.get("images", []), "inp": str(run.inp),
            "timings": run.results.get("timings", {})}


def compare(case: dict, measured: dict) -> list[dict]:
    """One row per compared quantity: theory, AsmFEA, signed difference, pass/fail."""
    names = case["theory"]["results"]
    rows = []
    for q in case["compare"]:
        if q.get("against") == "solidworks_mass":
            if not case.get("sw_mass"):
                continue
            ref = float(case["sw_mass"]["solidworks_mass_kg"])
            ref_label = "SolidWorks mass properties"
        else:
            ref = _expr(q["theory"], names)
            ref_label = "theory"
        got = measured["values"][q["readout"]]["value"]
        diff = (got - ref) / ref
        tol = CRITERIA[q["criterion"]]
        sws = (case.get("sw_simulation") or {}).get(q["readout"])
        rows.append({"case": case["id"], "quantity": q["name"], "unit": q["unit"], "reference": ref,
                     "reference_label": ref_label, "asmfea": got, "difference": diff, "criterion": q["criterion"],
                     "tolerance": tol, "pass": abs(diff) <= tol,
                     "solidworks_simulation": sws, "sws_difference": None if sws is None else (sws - ref) / ref,
                     "where": measured["values"][q["readout"]]["where"],
                     "nodes": measured["values"][q["readout"]]["nodes"]})
    return rows


def _rerun(case: dict, work: Path) -> Path:
    """A regression run: the stored neutral model through the whole pipeline, without SolidWorks."""
    from .. import api
    study = work / case["id"]
    shutil.rmtree(study, ignore_errors=True)
    shutil.copytree(case["dir"] / "model", study)
    job = api.run(study).job_id
    while True:
        j = api.job(study, job, wait_s=60)
        if j.state not in ("running", "queued"):
            break
    if j.state != "done":
        raise RuntimeError(f"{case['id']}: regression run {j.state}: " + "; ".join(d.what for d in j.diagnostics))
    return study


def evaluate(root: Path, source: str = "sw", work: Optional[Path] = None, only: Optional[list[str]] = None) -> dict:
    """source 'sw': the full-chain study folders (the published report); 'rerun': fresh regression runs
    from each case's stored neutral model into `work` (no SolidWorks)."""
    root = Path(root)
    out = {"source": source, "cases": [], "rows": [], "mesh_sensitivity": []}
    for case in load_cases(root):
        if only and case["id"] not in only:
            continue
        t0 = time.time()
        study = _rerun(case, Path(work)) if source == "rerun" else case["dir"] / case["study"]
        measured = _read(case, study)
        rows = compare(case, measured)
        out["cases"].append({"case": {k: v for k, v in case.items() if k not in ("dir",)} | {"dir": str(case["dir"])},
                             "measured": measured, "rows": rows, "seconds": time.time() - t0})
        out["rows"] += rows
        for level, rel in (case.get("mesh_studies") or {}).items():
            p = case["dir"] / rel
            if (p / "results" / "results.json").exists():
                m = _read(case, p)
                out["mesh_sensitivity"].append({"case": case["id"], "fidelity": level, "measured": m,
                                                "rows": compare(case, m)})
    solver = root / "solver_checks" / "results.json"
    out["solver"] = json.loads(solver.read_text(encoding="utf-8")) if solver.exists() else None
    # the methods for joints and notches, on hand-written models (D33, D34)
    from .methods import run as run_methods
    out["methods"] = [] if only else run_methods(root.parent, Path(work) if work else root / "_regression")
    out["method_rows"] = [r for m in out["methods"] for r in m["rows"]]
    out["all_pass"] = all(r["pass"] for r in out["rows"] + out["method_rows"])
    return out
