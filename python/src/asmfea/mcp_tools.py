"""MCP tool registration: asmfea.mcp_tools.register(server, prefix, sw_session).

Adds one tool per public API function to any FastMCP (mcp 1.x) server. The tools add no
logic: each validates one typed parameter model (unknown fields rejected), calls
asmfea.api, and returns JSON. Conventions mirror the SolidWorks MCP (layoutcad):
  - tools never raise: failures come back as {"ok": false, "error", "hint"};
  - long work is a job: `run` starts it and waits, `job` polls, each call waits at most
    MAX_WAIT_S (Claude Desktop abandons a tool call after 240 s);
  - outcomes are RESULT / VERIFY lines, checks are PASS / WARN / FAIL;
  - images come back as MCP images after the JSON, within REPLY_BUDGET.

The core never imports this module (it is the only place that imports `mcp`).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from mcp.server.fastmcp import Image
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from . import api
from .jobs import ALL_STAGES
from .sw import SwUnavailable

MAX_WAIT_S = 220.0           # Claude Desktop abandons a tool call after 240 s (layoutcad)
REPLY_BUDGET = 900_000       # bytes; Claude Desktop rejects replies over 1 MB (layoutcad)

HINTS = {
    "FileNotFoundError": "check the study folder path (it must contain model.json), or run the earlier stages first",
    "KeyError": "check the id against asmfea_exceptions",
    "ValueError": "check the parameters against the tool description",
    "SwUnavailable": "SolidWorks or the asmfea add-in is not reachable; start SolidWorks with the add-in loaded",
}


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StudyIn(_In):
    study: str = Field(..., description="Absolute path to the study folder (<Assembly>_FEA/<study>), "
                                        "the one containing model.json")


class NoIn(_In):
    pass


class StatusIn(_In):
    check_addin: bool = Field(False, description="Also ask SolidWorks whether the asmfea add-in is loaded "
                                                 "(attaches to the running SolidWorks; only when the user agrees)")


class RunIn(StudyIn):
    stages: Optional[list[Literal["resolve", "mesh", "solve", "post", "report"]]] = Field(
        None, description=f"Stages to run, in order; default all: {ALL_STAGES}")
    wait_s: float = Field(200.0, ge=0, le=MAX_WAIT_S, description="Seconds to wait for the job before returning")
    force: bool = Field(False, description="Redo every stage, ignoring every cache. By default a stage whose inputs "
                                           "are unchanged since an earlier run reuses its outputs")
    fast: bool = Field(False, description="A fast run: approximate results for iterative design (coarser mesh, "
                                          "results in results_fast/, no report). Do a full run before relying on results")


class JobIn(StudyIn):
    job_id: Optional[str] = Field(None, description="Job id from asmfea_run; default the latest job")
    wait_s: float = Field(200.0, ge=0, le=MAX_WAIT_S, description="Seconds to wait for the job to finish")


class CancelIn(StudyIn):
    job_id: str = Field(..., description="Job id to cancel")


class DecideIn(StudyIn):
    exception_id: str = Field(..., description="The id of an open exception from asmfea_exceptions")
    connection: Literal["tie", "contact", "none", "revolute", "hinge", "slide", "sliding", "bolted", "screw"] = Field(
        ..., description="How the parts named by the exception are connected")
    friction: Optional[float] = Field(None, ge=0, le=2, description="Friction coefficient, for contact")
    note: Optional[str] = Field(None, description="Why; shown in the report's connection table")


class ImageIn(StudyIn):
    name: str = Field(..., description="Image name, e.g. 'setup_iso', '<case>_vonmises_iso', "
                                       "'<case>_displacement_front', '<case>_buckling_mode1_iso'")


class ReportIn(StudyIn):
    pdf: bool = Field(False, description="Also convert to PDF (Word or LibreOffice on this machine)")
    summary: bool = Field(False, description="The half-page summary for risk assessments (project details, a stress "
                                             "plot, max stress / displacement, min factor of safety) instead of the full report")


class ReplotIn(StudyIn):
    toward_viewer: list[float] = Field(..., min_length=3, max_length=3,
                                       description="Direction towards the viewer, assembly frame (SolidWorks: Y up)")
    up: list[float] = Field(..., min_length=3, max_length=3, description="Screen up direction, assembly frame")


def _j(obj) -> str:
    return json.dumps(obj, indent=1, default=str)


def _err(e: Exception) -> str:
    name = type(e).__name__
    return _j({"ok": False, "error": f"{name}: {e}", "hint": HINTS.get(name, "")})


def _job_payload(r: api.JobResult) -> dict:
    lines = [f"RESULT: {x}" for x in r.results] + [f"VERIFY: {x}" for x in r.verify]
    if r.state == "done":
        lines.insert(0, "RESULT: OK  job finished")
    elif r.state in ("failed", "blocked"):
        lines.insert(0, f"RESULT: ERROR  job {r.state}: {r.activity}")
    return {
        "ok": r.state not in ("failed", "blocked"),
        "job": r.job_id, "state": r.state, "stage": r.stage, "activity": r.activity,
        "progress": r.progress, "elapsed_s": r.elapsed_s, "stalled": r.stalled,
        "solver": {k: v for k, v in (r.solver or {}).items() if k not in ("log_tail", "note")} or None,
        "lines": lines,
        "warnings": [{"code": d.code, "what": d.what, "where": d.where.model_dump(exclude_defaults=True)}
                     for d in r.warnings],
        "diagnostics": [{"id": d.id, "code": d.code, "what": d.what, "where": d.where.model_dump(exclude_defaults=True),
                         "likely_causes": d.likely_causes, "actions": d.actions} for d in r.diagnostics],
        "log_tail": r.log_tail[-5:],
        "next": ("poll again with asmfea_job" if r.state in ("queued", "running") else
                 "read asmfea_summary and asmfea_image" if r.state == "done" else
                 "read the diagnostics; asmfea_exceptions lists what needs a decision"),
    }


def _image_reply(payload: dict, path: Path) -> list:
    data = path.read_bytes()
    if len(data) * 4 // 3 > REPLY_BUDGET:
        from io import BytesIO
        from PIL import Image as PILImage
        im = PILImage.open(path)
        scale = (REPLY_BUDGET / (len(data) * 4 / 3)) ** 0.5
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
        buf = BytesIO()
        im.save(buf, format="PNG")
        data = buf.getvalue()
        payload["downscaled"] = round(scale, 3)
    return [_j(payload), Image(data=data, format="png")]


def register(server: Any, prefix: str = "asmfea_", sw_session: Optional[api.SwSession] = None) -> list[str]:
    """Add the asmfea tools to a FastMCP server. Returns the tool names."""
    # FastMCP runs sync tools on worker threads, and a first `import numpy` on a worker
    # thread deadlocks inside NumPy's C-extension import (milestone 7). Import everything
    # heavy here, on the thread that registers the tools.
    import numpy  # noqa: F401
    from . import pipeline  # noqa: F401  (geometry/OCP, mesh, deck, post)
    names = []

    def tool(name: str, title: str, read_only: bool):
        full = prefix + name
        names.append(full)
        return server.tool(name=full, annotations=ToolAnnotations(
            title=title, readOnlyHint=read_only, destructiveHint=False, idempotentHint=read_only,
            openWorldHint=False))

    @tool("status", "asmfea status", True)
    def status(params: StatusIn) -> str:
        """Versions and whether the CalculiX solver is configured. With check_addin, also whether
        the asmfea add-in in SolidWorks is reachable. Call this first."""
        try:
            return _j({"ok": True, **api.status(sw_session, params.check_addin).model_dump()})
        except Exception as e:
            return _err(e)

    @tool("extract", "Export the neutral model from SolidWorks", False)
    def extract(params: StudyIn) -> str:
        """Ask the asmfea add-in in the running SolidWorks to write model.json and the part STEP
        files into the study folder. Needs SolidWorks running with the add-in loaded."""
        try:
            if sw_session is None:
                raise SwUnavailable("no SolidWorks session in this server")
            out = api.extract(params.study, sw_session)
            return _j({"ok": True, "model": str(out), "lines": [f"RESULT: OK  model written to {out}"]})
        except Exception as e:
            return _err(e)

    @tool("run", "Run the analysis", False)
    def run(params: RunIn) -> str:
        """Start a job for some or all of resolve, mesh, solve, post and report, then wait up to
        wait_s for it. A job outlives this call: poll with asmfea_job until state is done,
        blocked or failed. Results come back as RESULT and VERIFY lines."""
        try:
            r = api.run(params.study, list(params.stages) if params.stages else None, force=params.force, fast=params.fast)
            return _j(_job_payload(api.job(params.study, r.job_id, min(params.wait_s, MAX_WAIT_S))))
        except Exception as e:
            return _err(e)

    @tool("job", "Poll a job", True)
    def job(params: JobIn) -> str:
        """State of a job: stage, current activity, progress, solver CPU/memory as proof of
        life, RESULT/VERIFY lines, warnings and blocking diagnostics. Waits up to wait_s."""
        try:
            return _j(_job_payload(api.job(params.study, params.job_id, min(params.wait_s, MAX_WAIT_S))))
        except Exception as e:
            return _err(e)

    @tool("cancel", "Cancel a job", False)
    def cancel(params: CancelIn) -> str:
        """Cancel a running job; the mesher or solver process tree is stopped."""
        try:
            return _j(_job_payload(api.cancel(params.study, params.job_id)))
        except Exception as e:
            return _err(e)

    @tool("summary", "Check table and key results", True)
    def summary(params: StudyIn) -> str:
        """The PASS/WARN/FAIL check table and the key figures per load case (max displacement with
        location, peak stresses, factors of safety, buckling factors) from results.json."""
        try:
            s = api.summary(params.study)
            lines = [f"{c['status']}: {c['check']} = {c['value']} (limit {c['limit']})" for c in s.checks]
            key = {case: {"max_disp_mm": lc["max_disp"], "max_disp_at": lc.get("max_disp_at"),
                          "parts": {p: {"membrane_bending_MPa": v.get("membrane_bending"),
                                        "peak_vm_MPa": v["peak_vm_excl_ties"],
                                        "min_fos": v["min_fos"],
                                        "min_fos_ultimate": v.get("min_fos_ultimate")} for p, v in lc["parts"].items()}}
                   for case, lc in s.load_cases.items()}
            return _j({"ok": True, "job": s.job, "checks": lines, "load_cases": key})
        except Exception as e:
            return _err(e)

    @tool("exceptions", "Open diagnostics", True)
    def exceptions(params: StudyIn) -> str:
        """Open diagnostics: what happened, where (parts, faces, connections), evidence, likely
        causes and suggested actions. Connection exceptions are resolved with asmfea_decide."""
        try:
            ex = api.exceptions(params.study).exceptions
            return _j({"ok": True, "count": len(ex), "exceptions": [d.model_dump() for d in ex]})
        except Exception as e:
            return _err(e)

    @tool("decide", "Record a connection decision", False)
    def decide(params: DecideIn) -> str:
        """Record how the parts named by an exception are connected. Written into the assembly
        through the add-in when SolidWorks is reachable, else to pending_overrides.json. Run the
        resolve stage again afterwards."""
        try:
            dec = {"connection": params.connection}
            if params.friction is not None:
                dec["friction"] = params.friction
            if params.note:
                dec["note"] = params.note
            r = api.decide(params.study, params.exception_id, dec, sw_session)
            return _j({"ok": True, **r.model_dump(), "lines": [f"RESULT: OK  decision recorded in {r.written_to}"]})
        except Exception as e:
            return _err(e)

    @tool("image", "Show a result plot", True)
    def image(params: ImageIn) -> list:
        """Return one rendered plot so you can look at it (and the list of available plots)."""
        try:
            r = api.image(params.study, params.name)
            return _image_reply({"ok": True, "name": r.name, "path": r.path, "available": r.available},
                                Path(r.path))
        except Exception as e:
            return [_err(e)]

    @tool("report", "Write the report", False)
    def report(params: ReportIn) -> str:
        """Path to the Word report (written from results.json if missing or older), optionally
        with a PDF."""
        try:
            r = api.report(params.study, params.pdf, params.summary)
            return _j({"ok": True, **r.model_dump(), "lines": [f"RESULT: OK  report {r.path}"]})
        except Exception as e:
            return _err(e)

    @tool("replot", "Redraw plots from a new view", False)
    def replot(params: ReplotIn) -> str:
        """Redraw the plots, report and summary of the last solved run from a new main view,
        without re-solving. Starts a job: poll it with the job tool."""
        try:
            r = api.replot(params.study, params.toward_viewer, params.up)
            return _j({"ok": True, **r.model_dump(), "lines": [f"RESULT: OK  job {r.job_id} started (post, report)"]})
        except Exception as e:
            return _err(e)

    return names
