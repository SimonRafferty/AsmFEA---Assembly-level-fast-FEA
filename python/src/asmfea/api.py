"""The only public Python surface of asmfea. The CLI and MCP layers wrap these 1:1.

Every function takes a study folder and returns a typed (pydantic) result. Nothing here
touches SolidWorks except `extract` (and later `decide` / highlighting), which go through
an SwSession supplied by the caller.
"""
from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any, Optional, Protocol

from pydantic import BaseModel

from . import jobs
from .config import load_config
from .diagnostics import Diagnostic


class SwSession(Protocol):
    """The single path to SolidWorks: one method that calls the add-in automation object."""

    def call(self, method: str, *args: Any) -> Any: ...


# --------------------------------------------------------------------------- #
# Result types

class StatusResult(BaseModel):
    asmfea_version: str
    python: str
    config_file: Optional[str]
    solver: Optional[str]
    solver_found: bool
    prepomax: Optional[str]
    addin_reachable: Optional[bool]   # None = no SolidWorks session supplied


class RunResult(BaseModel):
    study: str
    job_id: str
    stages: list[str]


class JobResult(BaseModel):
    job_id: str
    state: str
    stage: Optional[str] = None
    activity: Optional[str] = None
    progress: Optional[dict] = None
    elapsed_s: Optional[float] = None
    heartbeat_age_s: Optional[float] = None
    stalled: bool = False
    solver: Optional[dict] = None
    log_tail: list[str] = []
    results: list[str] = []
    verify: list[str] = []
    warnings: list[Diagnostic] = []
    diagnostics: list[Diagnostic] = []
    timings: dict = {}
    reuse: dict = {}            # what each stage reused from earlier runs (stage caching)


class SummaryResult(BaseModel):
    study: str
    job: Optional[str]
    checks: list[dict]
    load_cases: dict
    plot_view: Optional[dict] = None    # main view of the plots: {toward_viewer, up}
    run_mode: str = "full"              # "fast": approximate results of a fast run (ADDENDUM-FAST-RUN-MODE)


class ExceptionsResult(BaseModel):
    study: str
    exceptions: list[Diagnostic]


class DecideResult(BaseModel):
    study: str
    exception_id: str
    written_to: str


# --------------------------------------------------------------------------- #

def _study(study: str | Path) -> Path:
    p = Path(study).resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"study folder not found: {p}")
    return p


def status(sw_session: Optional[SwSession] = None, check_addin: bool = False) -> StatusResult:
    """check_addin=True asks SolidWorks whether the add-in is loaded; that attaches to the
    running SolidWorks, so it is never done implicitly."""
    from .pipeline import asmfea_version
    cfg = load_config()
    reachable = None
    if sw_session is not None and check_addin:
        try:
            sw_session.call("Version")
            reachable = True
        except Exception:
            reachable = False
    return StatusResult(asmfea_version=asmfea_version(), python=platform.python_version(),
                        config_file=cfg.source, solver=cfg.ccx,
                        solver_found=bool(cfg.ccx and Path(cfg.ccx).is_file()),
                        prepomax=cfg.prepomax, addin_reachable=reachable)


def extract(study: str | Path, sw_session: SwSession) -> Path:
    """Ask the add-in to write the neutral model into the study folder (milestone 2)."""
    p = Path(study).resolve()
    out = sw_session.call("ExportModel", p.name, str(p))
    return Path(out)


def run(study: str | Path, stages: Optional[list[str]] = None, cases: Optional[list[str]] = None,
        force: bool = False, fast: bool = False) -> RunResult:
    """Start a job. ``cases``: solve only these load cases (default: all). A stage whose inputs are
    unchanged since an earlier successful run reuses that run's outputs; ``force`` redoes everything
    (every cache ignored and refilled). ``fast``: a fast run (approximate, for iterative design: coarser mesh,
    one mesh level, results in results_fast/, no report)."""
    p = _study(study)
    stages = stages or list(jobs.ALL_STAGES)
    bad = [s for s in stages if s not in jobs.ALL_STAGES]
    if bad:
        raise ValueError(f"unknown stage(s) {bad}; valid: {jobs.ALL_STAGES}")
    return RunResult(study=str(p), job_id=jobs.start(p, stages, cases, force, "fast" if fast else "full"), stages=stages)


def job(study: str | Path, job_id: Optional[str] = None, wait_s: float = 0.0) -> JobResult:
    p = _study(study)
    if job_id is None:
        ids = jobs.list_jobs(p)
        if not ids:
            raise FileNotFoundError(f"no jobs in {p}")
        job_id = ids[-1]
    d = jobs.status(p, job_id, wait_s)
    return JobResult(job_id=job_id, **{k: v for k, v in d.items() if k in JobResult.model_fields and k != "job_id"})


def cancel(study: str | Path, job_id: str) -> JobResult:
    p = _study(study)
    jobs.cancel(p, job_id)
    return job(p, job_id)


def summary(study: str | Path) -> SummaryResult:
    """The newer of the last full and the last fast run's results (run_mode says which)."""
    from .runmode import latest_results
    p = _study(study)
    out, mode = latest_results(p)
    path = out / "results.json"
    if not path.exists():
        raise FileNotFoundError("no results yet: run the post stage first")
    r = json.loads(path.read_text())
    view = r.get("plot_view")
    if view is None and r.get("images"):
        # results from before plot_view was recorded: the view the plots took (post.plots.views_for)
        for src in (p / "view.json", p / "model.json"):
            if src.exists():
                d = json.loads(src.read_text(encoding="utf-8-sig"))
                v = d if src.name == "view.json" else (d.get("source") or {}).get("view")
                if v:
                    view = {"toward_viewer": v["toward_viewer"], "up": v["up"]}
                    break
    return SummaryResult(study=str(p), job=r.get("job"), checks=r.get("checks", []),
                         load_cases=r.get("load_cases", {}), plot_view=view, run_mode=mode)


def exceptions(study: str | Path) -> ExceptionsResult:
    """Open diagnostics: exceptions.json plus blocking diagnostics of the latest job."""
    p = _study(study)
    out: dict[str, Diagnostic] = {}
    exc = p / "exceptions.json"
    if exc.exists():
        for e in json.loads(exc.read_text())["exceptions"]:
            d = Diagnostic.model_validate(e)
            out[d.id] = d
    ids = jobs.list_jobs(p)
    if ids:
        doc = jobs.read_json(jobs.job_dir(p, ids[-1]) / "job.json")
        for e in doc.get("diagnostics", []):
            d = Diagnostic.model_validate(e)
            out.setdefault(d.id, d)
    return ExceptionsResult(study=str(p), exceptions=list(out.values()))


class ImageResult(BaseModel):
    study: str
    name: str
    path: str
    available: list[str] = []


class ReportResult(BaseModel):
    study: str
    path: str
    pdf: Optional[str] = None


def image(study: str | Path, name: str) -> ImageResult:
    """Path to a rendered plot, e.g. 'setup_iso' or '<case>_vonmises_iso' (.png optional). "iso" is the main
    3-D view: the SolidWorks view at export when the model records one."""
    from .runmode import latest_results
    p = _study(study)
    res = latest_results(p)[0]
    names = sorted(x.stem for x in res.glob("*.png")) if res.exists() else []
    stem = name[:-4] if name.lower().endswith(".png") else name
    if stem not in names:
        raise FileNotFoundError(f"no image {stem!r}; available: {names[:40]}")
    return ImageResult(study=str(p), name=stem, path=str(res / f"{stem}.png"), available=names)


def report(study: str | Path, pdf: bool = False, summary: bool = False) -> ReportResult:
    """Path to the Word report, or with summary=True to the half-page summary (project
    details, a stress plot, max stress / displacement and min factor of safety) for risk
    assessments; writes it from results.json if it is missing or older."""
    from .report import build_report, build_summary, to_pdf
    from .model import load_model
    from .runmode import FAST, model_key, results_dir
    p = _study(study)
    results = p / "results" / "results.json"
    if not results.exists():
        if (results_dir(p, FAST) / "results.json").exists():
            raise FileNotFoundError("only a fast run exists: a report needs a full run (Run, not Fast run)")
        raise FileNotFoundError("no results yet: run the post stage first")
    full = json.loads(results.read_text())
    if full.get("model_key") and full["model_key"] != model_key(load_model(p)):
        raise FileNotFoundError("the model has changed since the last full run: do a full run before producing a report")
    pattern = "*_summary.docx" if summary else "*_report.docx"
    existing = sorted((p / "report").glob(pattern)) if (p / "report").exists() else []
    if existing and existing[0].stat().st_mtime >= results.stat().st_mtime:
        out = existing[0]
    else:
        out = build_summary(p) if summary else build_report(p)
    pdf_path = to_pdf(out) if pdf else None
    return ReportResult(study=str(p), path=str(out), pdf=str(pdf_path) if pdf_path else None)


def replot(study: str | Path, toward_viewer: list[float], up: list[float]) -> RunResult:
    """Redraw the plots, report and summary of the last solved run from a new main view, without
    meshing or solving again (a post + report job; poll it with `job`). The view is in the
    assembly frame: the direction towards the viewer and the screen's up, as SolidWorks reports
    them. It is kept in view.json until the next full run, whose export records the view then."""
    import math
    p = _study(study)
    from .runmode import FAST, latest_results, latest_solve_file
    mode = latest_results(p)[1]
    if not latest_solve_file(p, mode).exists():
        raise FileNotFoundError("no solved run yet: run the study first")

    def unit(v, what):
        if len(v) != 3:
            raise ValueError(f"{what} needs three components")
        n = math.sqrt(sum(float(x) * float(x) for x in v))
        if n < 1e-9:
            raise ValueError(f"{what} is zero")
        return [float(x) / n for x in v]
    t = unit(toward_viewer, "toward_viewer")
    u = unit(up, "up")
    d = sum(a * b for a, b in zip(t, u))
    u = unit([b - d * a for a, b in zip(t, u)], "up (parallel to toward_viewer)")   # make it square to t
    jobs.write_json_atomic(p / "view.json", {"schema_version": "1.0", "toward_viewer": t, "up": u})
    return run(p, ["post", "report"], fast=mode == FAST)        # the results on show: fast or full


def view(study: str | Path, case: Optional[str] = None, field: str = "vonmises") -> str:
    """Open an interactive 3-D window of one result field of the last run (von Mises stress,
    displacement or factor of safety): rotate, pan and zoom with the mouse, Reset view returns to
    the main view. Starts the window as its own process and returns at once; needs a desktop."""
    import subprocess
    import sys
    from .post.viewer import FIELDS, field_files
    p = _study(study)
    if field not in FIELDS:
        raise ValueError(f"field must be one of {sorted(FIELDS)}")
    from .runmode import latest_results
    results = latest_results(p)[0] / "results.json"
    if not results.exists():
        raise FileNotFoundError("no results yet: run the study first")
    cases = list(json.loads(results.read_text()).get("load_cases", {}))
    case = case or (cases[0] if cases else None)
    if case not in cases:
        raise ValueError(f"no load case {case!r}; the last run has {cases}")
    vtp, meta = field_files(p, case)
    if not (vtp.exists() and meta.exists()):
        raise FileNotFoundError("no 3-D result data for this run yet: redraw the plots (replot) or run again")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)      # no console; the viewer's own window shows
    subprocess.Popen([sys.executable, "-m", "asmfea.post.viewer", str(p), case, field], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    return f"{field} of {case}"


def prepomax(study: str | Path, job_id: Optional[str] = None, level: str = "refined") -> str:
    """Open a run's deck in PrePoMax for manual inspection (never part of the automated path)."""
    import subprocess
    p = _study(study)
    cfg = load_config()
    if not cfg.prepomax or not Path(cfg.prepomax).is_file():
        raise FileNotFoundError("PrePoMax not configured: install it (https://prepomax.fs.um.si/downloads/) "
                                "and set its PrePoMax.exe with set_prepomax (CLI: asmfea config --prepomax <path>)")
    latest = p / "run" / "latest_solve.txt"
    if job_id is None and not latest.exists():
        raise FileNotFoundError("no solved run yet: run the study first")
    job_id = job_id or latest.read_text().strip()
    run = p / "run" / job_id
    # the refined mesh when the run has one (it is the reported result), else the base mesh;
    # contact runs keep one deck per load case below the level folder
    for lv in (level, "base", "refined"):
        decks = sorted((run / lv).glob("study.inp")) + sorted((run / lv).glob("*/study.inp"))
        if decks:
            inp = decks[0]
            break
    else:
        raise FileNotFoundError(f"no CalculiX deck in run {job_id}")
    # PrePoMax opens a file given with -f (PrePoMax.com --help): the results when the run has
    # them, else the deck (imported as a model)
    frd = inp.with_suffix(".frd")
    target = frd if frd.exists() and frd.stat().st_size > 200 else inp
    subprocess.Popen([cfg.prepomax, "-f", str(target)], cwd=inp.parent)
    return str(target)


def verify(root: str | Path, source: str = "sw", work: Optional[str | Path] = None, out: Optional[str | Path] = None,
           only: Optional[list[str]] = None) -> dict:
    """Run the verification cases in `root` (cases/verification) and write the verification report.
    source 'sw' reads the full-chain SolidWorks runs (the published report); 'rerun' re-runs every case
    from its stored neutral model in `work` (regression, no SolidWorks). Returns the comparison rows,
    whether all pass, and the report's path."""
    import hashlib
    import subprocess
    from .pipeline import asmfea_version
    from .verify import evaluate
    from .verify.report import build
    root = Path(root).resolve()
    if source not in ("sw", "rerun"):
        raise ValueError("source must be 'sw' or 'rerun'")
    work = Path(work) if work else root / "_regression"
    result = evaluate(root, source, work, only)
    cfg = load_config()
    versions = {"asmfea": asmfea_version(), "calculix": "2.22"}
    if cfg.ccx and Path(cfg.ccx).is_file():
        versions["calculix_md5"] = hashlib.md5(Path(cfg.ccx).read_bytes()).hexdigest()
    if cfg.gmsh and Path(cfg.gmsh).is_file():
        r = subprocess.run([cfg.gmsh, "-version"], capture_output=True, text=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        versions["gmsh"] = (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else "-"
    out = Path(out) if out else root / ("AsmFEA_Verification_Report.docx" if source == "sw"
                                        else "AsmFEA_Verification_Regression.docx")
    build(result, root, out, versions)
    # the same report as a web page (icon from the add-in's source when it is there) and its PDF copy
    from .verify.html import build as build_html, print_pdf
    icon = root.parents[1] / "addin" / "AsmFea.AddIn" / "AsmFEA_Icon.png"
    page = build_html(result, root, out.with_suffix(".html"), versions, icon if icon.exists() else None)
    pdf = print_pdf(page, out.with_suffix(".pdf"))
    (out.with_suffix(".json")).write_text(json.dumps({k: v for k, v in result.items() if k != "cases"} |
                                                     {"versions": versions}, indent=1, default=str))
    return {"report": str(out), "html": str(page), "pdf": str(pdf) if pdf else None, "all_pass": result["all_pass"],
            "rows": [{k: r[k] for k in ("case", "quantity", "reference", "asmfea", "difference", "pass")}
                     for r in result["rows"]]}


def set_prepomax(exe: str | Path) -> str:
    """Remember where PrePoMax.exe is (the user's own asmfea.toml); returns that file's path."""
    from .config import set_prepomax as _set
    return str(_set(exe))


def decide(study: str | Path, exception_id: str, decision: dict,
           sw_session: Optional[SwSession] = None) -> DecideResult:
    """Record a connection decision. Through the add-in when SolidWorks is reachable,
    otherwise into pending_overrides.json (merged by the pipeline and by the add-in at
    the next extract)."""
    p = _study(study)
    # Fill parts / interface from the exception when the decision gives only the type,
    # e.g. decide(study, "LINE_CONTACT:...", {"connection": "tie"}).
    exc = {d.id: d for d in exceptions(p).exceptions}
    if exception_id not in exc and "parts" not in decision:
        raise KeyError(f"no open exception {exception_id!r}; give 'parts' explicitly")
    d = exc.get(exception_id)
    override = {"id": "OV-" + "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in exception_id)[:60]}
    if d is not None and "parts" not in decision:
        if len(d.where.parts) != 2:
            raise ValueError(f"exception {exception_id} involves {len(d.where.parts)} parts; give 'parts' explicitly")
        override["parts"] = list(d.where.parts)
        if len(d.where.connections) == 1:
            override["interface"] = d.where.connections[0]
    override.update(decision)
    if "connection" not in override:
        raise ValueError("decision needs 'connection': tie, contact, none, revolute, sliding or bolted")
    if sw_session is not None:
        sw_session.call("AddOverride", p.name, json.dumps(override))
        return DecideResult(study=str(p), exception_id=exception_id, written_to="assembly")
    path = p / "pending_overrides.json"
    doc = json.loads(path.read_text()) if path.exists() else {"schema_version": "1.0", "overrides": []}
    doc["overrides"] = [o for o in doc["overrides"] if o["id"] != override["id"]] + [override]
    jobs.write_json_atomic(path, doc)
    return DecideResult(study=str(p), exception_id=exception_id, written_to=str(path))
