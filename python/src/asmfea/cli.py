"""asmfea <verb> <study folder> — mirrors asmfea.api one for one.

With --json every command prints exactly one JSON document and nothing else.
Exit codes: 0 ok, 1 error, 3 job blocked/failed (for `job --wait`).
"""
from __future__ import annotations

import argparse
import json
import sys

from pydantic import BaseModel

from . import api


def _emit(result, as_json: bool) -> None:
    if as_json:
        print(result.model_dump_json() if isinstance(result, BaseModel) else json.dumps(result, default=str))
        return
    if isinstance(result, api.JobResult):
        print(f"job {result.job_id}: {result.state}  [{result.stage}] {result.activity}")
        if result.progress:
            print(f"  progress {result.progress['done']}/{result.progress['total']}")
        for line in result.log_tail[-3:]:
            print(f"  | {line}")
        for line in result.results:
            print(f"  RESULT {line}")
        for line in result.verify:
            print(f"  VERIFY {line}")
        for d in result.warnings + result.diagnostics:
            print(f"  {d.severity.upper()} {d.code}: {d.what}")
            for a in d.actions:
                print(f"    -> {a}")
    elif isinstance(result, api.ExceptionsResult):
        if not result.exceptions:
            print("no open exceptions")
        for d in result.exceptions:
            print(f"{d.severity.upper()} [{d.id}] {d.what}")
            for c in d.likely_causes:
                print(f"  cause? {c}")
            for a in d.actions:
                print(f"  -> {a}")
    elif isinstance(result, api.SummaryResult):
        for row in result.checks:
            print(f"{row['status']:4s} {row['check']}: {row['value']} (limit {row['limit']})")
    else:
        for k, v in (result.model_dump() if isinstance(result, BaseModel) else result).items():
            print(f"{k}: {v}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="asmfea")
    ap.add_argument("--json", action="store_true", help="print one JSON document")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("status")
    p = sub.add_parser("recheck", help="apply new pass/fail criteria to the last results, without solving again")
    p.add_argument("study")
    p.add_argument("--limits", default=None,
                   help="the study's limits, replacing study.json's: name=value pairs separated by commas, e.g. "
                        "min_fos=2,max_deflection=5 (an empty string clears them)")
    p = sub.add_parser("plan", help="how a run would be set up on this computer now (run planner, D60)")
    p.add_argument("study")
    p.add_argument("--fast", action="store_true", help="the plan for a Fast run")
    p.add_argument("--favour", choices=["speed", "balanced", "accuracy"], default=None,
                   help="plan for this favour instead of the study's (the task pane's unsaved choice)")
    p.add_argument("--fidelity", choices=["coarse", "standard", "fine"], default=None, help="Advanced: mesh level")
    p.add_argument("--convergence", choices=["on", "off"], default=None, help="Advanced: convergence check")
    p.add_argument("--refine", choices=["on", "off"], default=None,
                   help="Advanced: refine where not converged (a third, local pass)")
    p.add_argument("--solver", choices=["auto", "calculix", "hybrid"], default=None, help="Advanced: solver")
    p = sub.add_parser("run")
    p.add_argument("study")
    p.add_argument("--stages", nargs="+", default=None)
    p.add_argument("--cases", nargs="+", default=None, help="solve only these load cases (default: all)")
    p.add_argument("--force", action="store_true", help="redo every stage: ignore every cache (unchanged stages are otherwise reused)")
    p.add_argument("--fast", action="store_true", help="a fast run: approximate results for iterative design (no report)")
    p = sub.add_parser("job")
    p.add_argument("study")
    p.add_argument("job_id", nargs="?")
    p.add_argument("--wait", type=float, default=0.0, help="seconds to wait for completion")
    p = sub.add_parser("cancel")
    p.add_argument("study")
    p.add_argument("job_id")
    for verb in ("summary", "exceptions"):
        sub.add_parser(verb).add_argument("study")
    p = sub.add_parser("decide")
    p.add_argument("study")
    p.add_argument("exception_id")
    p.add_argument("decision", help='JSON, e.g. {"parts":["A","B"],"connection":"none"}')
    p = sub.add_parser("image")
    p.add_argument("study")
    p.add_argument("name", help="e.g. setup_iso or <case>_vonmises_iso")
    p = sub.add_parser("report")
    p.add_argument("study")
    p.add_argument("--pdf", action="store_true")
    p.add_argument("--summary", action="store_true", help="the half-page summary instead of the full report")
    p = sub.add_parser("replot", help="redraw plots and reports of the last run from a new view (no re-solve)")
    p.add_argument("study")
    p.add_argument("--toward", required=True, help="direction towards the viewer, x,y,z (assembly frame)")
    p.add_argument("--up", required=True, help="screen up direction, x,y,z")
    p = sub.add_parser("view", help="interactive 3-D window of a result field of the last run")
    p.add_argument("study")
    p.add_argument("--case", default=None)
    p.add_argument("--field", default="vonmises", choices=["vonmises", "displacement", "fos"])
    p = sub.add_parser("verify", help="run the verification cases and write the verification report")
    p.add_argument("root", help="the verification folder, e.g. cases/verification")
    p.add_argument("--rerun", action="store_true", help="re-run every case from its stored neutral model (no SolidWorks)")
    p.add_argument("--work", default=None, help="where regression runs go (default <root>/_regression)")
    p.add_argument("--out", default=None, help="report path (.docx)")
    p.add_argument("--only", nargs="*", default=None, help="case ids, e.g. C1 C3")
    p = sub.add_parser("config", help="show the configuration, or set where PrePoMax.exe is")
    p.add_argument("--prepomax", default=None, help="path of PrePoMax.exe, remembered in the user's asmfea.toml")
    p = sub.add_parser("prepomax")
    p.add_argument("study")
    p.add_argument("job_id", nargs="?")
    a = ap.parse_args(argv)
    try:
        if a.verb == "status":
            r = api.status()
        elif a.verb == "run":
            r = api.run(a.study, a.stages, a.cases, a.force, a.fast)
        elif a.verb == "job":
            r = api.job(a.study, a.job_id, a.wait)
        elif a.verb == "cancel":
            r = api.cancel(a.study, a.job_id)
        elif a.verb == "summary":
            r = api.summary(a.study)
        elif a.verb == "recheck":
            lims = None
            if a.limits is not None:
                lims = {k.strip(): float(v) for k, v in (kv.split("=", 1) for kv in a.limits.split(",") if "=" in kv)}
            r = api.recheck(a.study, lims)
        elif a.verb == "plan":
            mesh = {k: v for k, v in (("favour", a.favour), ("fidelity", a.fidelity), ("solver", a.solver)) if v}
            if a.convergence:
                mesh["convergence_check"] = a.convergence == "on"
            if a.refine:
                mesh["refine_unconverged"] = a.refine == "on"
            r = api.plan(a.study, a.fast, mesh or None)
        elif a.verb == "exceptions":
            r = api.exceptions(a.study)
        elif a.verb == "decide":
            r = api.decide(a.study, a.exception_id, json.loads(a.decision))
        elif a.verb == "image":
            r = api.image(a.study, a.name)
        elif a.verb == "report":
            r = api.report(a.study, a.pdf, a.summary)
        elif a.verb == "replot":
            r = api.replot(a.study, [float(x) for x in a.toward.split(",")], [float(x) for x in a.up.split(",")])
        elif a.verb == "view":
            r = {"opened": api.view(a.study, a.case, a.field)}
        elif a.verb == "verify":
            r = api.verify(a.root, "rerun" if a.rerun else "sw", a.work, a.out, a.only)
        elif a.verb == "config":
            written = api.set_prepomax(a.prepomax) if a.prepomax else None
            from .config import load_config
            c = load_config()
            r = {"written": written, "config_file": c.source, "solver": c.ccx, "gmsh": c.gmsh, "prepomax": c.prepomax}
        elif a.verb == "prepomax":
            r = {"opened": api.prepomax(a.study, a.job_id)}
    except Exception as e:
        if a.json:
            print(json.dumps({"error": type(e).__name__, "message": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1
    _emit(r, a.json)
    if isinstance(r, api.JobResult) and r.state in ("blocked", "failed"):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
