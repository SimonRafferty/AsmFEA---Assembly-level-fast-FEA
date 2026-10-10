"""Jobs: a detached worker process per run, all state in run/<job id>/job.json.

Any client (CLI, MCP, task pane) starts a job and then polls job.json; no client holds
the worker. The worker refreshes a heartbeat every HEARTBEAT_S so a running job always
looks alive; a stale heartbeat with a dead worker is reported as JOB_STALLED.
Cancel by creating run/<job id>/cancel (api.cancel does this).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psutil

from .. import defaults
from ..diagnostics import Diagnostic, make

TERMINAL = ("done", "failed", "blocked", "cancelled")
ALL_STAGES = ["resolve", "mesh", "solve", "post", "report"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finite(v):
    """NaN and infinities as null, through dicts and lists."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_finite(x) for x in v]
    return v


def write_json_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    # strict JSON (no NaN / Infinity): the add-in's reader rejects them, and its progress display would stall
    tmp.write_text(json.dumps(_finite(doc), indent=1, default=str, allow_nan=False))
    for _ in range(20):  # a reader on Windows can briefly lock the target
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.05)
    tmp.replace(path)


def read_json(path: Path, retries: int = 20) -> dict:
    for _ in range(retries):
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, PermissionError):
            time.sleep(0.05)
    return json.loads(path.read_text())


def new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def job_dir(study: Path, job_id: str) -> Path:
    return study / "run" / job_id


class JobState:
    """Owned by the worker. Thread-safe; a background thread writes the heartbeat."""

    def __init__(self, study: Path, job_id: str, stages: list[str]):
        self.dir = job_dir(study, job_id)
        self.path = self.dir / "job.json"
        self._lock = threading.Lock()
        self._t0 = time.time()
        self.doc = read_json(self.path) if self.path.exists() else {}
        self.doc.update({
            "id": job_id, "study": str(study), "stages": stages, "state": "running",
            "stage": None, "activity": "starting", "progress": None, "pid": os.getpid(),
            "started": self.doc.get("started", _now()), "heartbeat": _now(), "elapsed_s": 0.0,
            "results": [], "verify": [], "warnings": [], "diagnostics": [], "timings": {},
            "solver": None, "log_tail": [],
        })
        self._stop = threading.Event()
        self._timed = None              # (text, start, estimate_s): an activity whose elapsed time is shown live
        self._flush()
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()

    # -- heartbeat ---------------------------------------------------------
    def _beat(self):
        while not self._stop.wait(defaults.HEARTBEAT_S):
            try:
                self._watch_memory()
            except Exception:                    # noqa: BLE001 - the heartbeat must never die of the watch
                pass
            self._flush()

    @staticmethod
    def _own_gb() -> float:
        """The memory this run itself holds: this process and everything it started (the solver), GB."""
        import psutil
        try:
            me = psutil.Process(os.getpid())
            procs = [me] + me.children(recursive=True)
        except psutil.Error:
            return 0.0
        total = 0
        for p in procs:
            try:
                total += p.memory_info().rss
            except psutil.Error:
                pass
        return total / 2 ** 30

    def _watch_memory(self):
        """Memory pressure (D60, the user's rule 2026-10-09): other programs have taken more than PRESSURE_DROP_GB
        of the memory that was free when the run started, for PRESSURE_TICKS beats in a row. The run's own growth
        does not count, and what was already so at the start never warns (there is nothing to do about it then).
        The run carries on (more slowly); the user is told once, with what to do, and job.json's memory_pressure
        says whether it still holds."""
        import psutil
        avail = psutil.virtual_memory().available / 2 ** 30
        own = self._own_gb()
        if not hasattr(self, "_avail0"):
            self._avail0, self._own0, self._press, self._calm = avail, own, 0, 0
        taken = (self._avail0 - avail) - (own - self._own0)      # what others took since the start
        pressed = taken > defaults.PRESSURE_DROP_GB
        self._press, self._calm = (self._press + 1, 0) if pressed else (0, self._calm + 1)
        mp = self.doc.get("memory_pressure") or {}
        if self._press >= defaults.PRESSURE_TICKS and not mp.get("active"):
            first = not mp
            with self._lock:
                self.doc["memory_pressure"] = {"active": True, "since": _now(), "available_gb": round(avail, 1),
                                               "taken_gb": round(taken, 1), "start_available_gb": round(self._avail0, 1)}
            if first:
                from ..diagnostics import make
                self.add_diags([make("MEMORY_PRESSURE", available_gb=avail, taken_gb=taken,
                                     evidence={"available_gb": round(avail, 2), "taken_gb": round(taken, 2),
                                               "start_available_gb": round(self._avail0, 2)})])
        elif mp.get("active") and self._calm >= defaults.PRESSURE_TICKS:
            with self._lock:
                self.doc["memory_pressure"] = {**mp, "active": False, "cleared": _now()}

    def _flush(self):
        with self._lock:
            if self._timed is not None:
                text, t0, est = self._timed
                el = time.time() - t0
                if not est:
                    tail = f"{el:.0f} s"
                elif el <= est:
                    tail = f"{el:.0f} s of ~{est:.0f} s (estimate)"
                else:
                    tail = f"{el:.0f} s (longer than the ~{est:.0f} s estimate)"
                self.doc["activity"] = f"{text}: {tail}"
            self.doc["heartbeat"] = _now()
            self.doc["elapsed_s"] = round(time.time() - self._t0, 1)
            write_json_atomic(self.path, self.doc)

    # -- updates -----------------------------------------------------------
    def stage(self, name: str):
        with self._lock:
            self.doc["stage"] = name
            self.doc["activity"] = f"{name}: starting"
            self.doc["progress"] = None
            self.doc["solver"] = None
            self.doc["timings"][name] = {"start": time.time()}
        self.log(f"== stage {name}")

    def stage_done(self, name: str):
        with self._lock:
            t = self.doc["timings"][name]
            t["seconds"] = round(time.time() - t.pop("start"), 2)

    @contextmanager
    def timed(self, text: str, estimate_s: Optional[float] = None):
        """An activity shown with its elapsed time, refreshed with the heartbeat, against an estimate when there
        is one: for a long step that reports no progress of its own (assembling the deck)."""
        with self._lock:
            self._timed = (text, time.time(), estimate_s)
            self.doc["activity"] = text
            self.doc["progress"] = None
        try:
            yield
        finally:
            with self._lock:
                self._timed = None

    def activity(self, text: str, done: Optional[int] = None, total: Optional[int] = None):
        with self._lock:
            self._timed = None
            self.doc["activity"] = text
            self.doc["progress"] = None if total is None else {"done": done, "total": total}

    def solver(self, status: dict, estimate_s: Optional[float]):
        with self._lock:
            status = dict(status)
            status["estimate_s"] = estimate_s
            status["note"] = ("A direct solve has no percentage; elapsed time is shown against an estimate "
                              "from earlier runs, with live CPU and memory as proof of life.")
            self.doc["solver"] = status
            self.doc["log_tail"] = status.get("log_tail", [])
            est = f" of ~{estimate_s:.0f} s (estimate)" if estimate_s else ""
            self.doc["activity"] = (f"solve: {status.get('solver', 'CalculiX')} running {status['elapsed']:.0f} s{est}, "
                                    f"{status['cpu_cores_busy']:.1f} cores busy, {status['rss_gb']:.1f} GB")

    def result(self, line: str):
        with self._lock:
            self.doc["results"].append(line)
        self.log("RESULT " + line)

    def verify(self, line: str):
        with self._lock:
            self.doc["verify"].append(line)
        self.log("VERIFY " + line)

    def add_diags(self, diags: list[Diagnostic]):
        added = []
        with self._lock:
            seen = {x["id"] for x in self.doc["diagnostics"] + self.doc["warnings"]}
            for d in diags:
                if d.id in seen:
                    continue
                seen.add(d.id)
                bucket = "diagnostics" if d.severity == "blocking" else "warnings"
                self.doc[bucket].append(d.model_dump())
                added.append(d)
        for d in added:
            self.log(f"{d.severity.upper()} {d.code}: {d.what}")

    def reuse(self, stage: str, text: str):
        """What a stage reused from earlier runs (stage caching), e.g. "mesh reused for 19 of 19"."""
        with self._lock:
            self.doc.setdefault("reuse", {})[stage] = text
        self.log(f"REUSE {stage}: {text}")

    def log(self, line: str):
        with open(self.dir / "log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%H:%M:%S')} {line}\n")

    def cancel_requested(self) -> bool:
        return (self.dir / "cancel").exists()

    def finish(self, state: str, activity: str):
        self._stop.set()
        self._thread.join(timeout=5)
        with self._lock:
            self.doc["state"] = state
            self.doc["activity"] = activity
            self.doc["finished"] = _now()
        self._flush()


# --------------------------------------------------------------------------- #
# Client side

def start(study: Path, stages: list[str], cases: list[str] | None = None, force: bool = False,
          mode: str = "full") -> str:
    job_id = new_job_id()
    d = job_dir(study, job_id)
    d.mkdir(parents=True, exist_ok=False)
    write_json_atomic(d / "job.json", {"id": job_id, "study": str(study), "stages": stages, "state": "queued",
                                       "stage": None, "activity": "queued", "started": _now(),
                                       "heartbeat": _now(), "results": [], "verify": [], "warnings": [],
                                       "diagnostics": [], "cases": list(cases or []), "force": bool(force), "mode": mode})
    flags = 0
    if sys.platform == "win32":
        # CREATE_NO_WINDOW gives the worker its own hidden console, which everything it starts
        # (the meshing process pool, Gmsh, CalculiX) inherits. DETACHED_PROCESS (no console at
        # all) is not used: a console child of a console-less process gets a NEW, visible
        # console, the black window that popped up over the desktop with focus.
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                 | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    out = open(d / "worker.out", "w")
    cmd = [sys.executable, "-m", "asmfea.jobs.worker", str(study), job_id, *stages]
    # The worker records its own pid in job.json (a venv python.exe is a launcher, so
    # the Popen pid is not the worker's).
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         creationflags=flags, close_fds=True)
    except OSError:
        # CREATE_BREAKAWAY_FROM_JOB is refused inside some job objects; retry without it.
        flags &= ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         creationflags=flags, close_fds=True)
    return job_id


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def status(study: Path, job_id: str, wait_s: float = 0.0) -> dict:
    """Read job.json, waiting up to wait_s for a terminal state. Adds `stalled`."""
    path = job_dir(study, job_id) / "job.json"
    if not path.exists():
        raise FileNotFoundError(f"no job {job_id} in {study}")
    deadline = time.time() + max(0.0, wait_s)
    while True:
        doc = read_json(path)
        if doc.get("state") in TERMINAL or time.time() >= deadline:
            break
        time.sleep(min(1.0, max(0.05, deadline - time.time())))
    doc["stalled"] = False
    if doc.get("state") not in TERMINAL:
        hb = datetime.fromisoformat(doc["heartbeat"]).timestamp()
        age = time.time() - hb
        doc["heartbeat_age_s"] = round(age, 1)
        if age > defaults.STALE_HEARTBEAT_S and not _alive(doc.get("pid")):
            doc["stalled"] = True
            doc.setdefault("warnings", []).append(make("JOB_STALLED", job=job_id, age=age).model_dump())
    return doc


def cancel(study: Path, job_id: str, grace_s: Optional[float] = None) -> None:
    """Ask the job to stop; if it has not stopped within grace_s (CANCEL_GRACE_S), end its worker's whole process
    tree (the worker, its threads, and any solver or mesher it started) and mark it cancelled. A long native call
    (a PARDISO factorisation, a part's condensation) does not look at the request, so Cancel waited for it to
    finish (found live on a water tank: still running minutes later; the user asked for this)."""
    d = job_dir(study, job_id)
    (d / "cancel").write_text(_now())
    grace = defaults.CANCEL_GRACE_S if grace_s is None else grace_s
    t_end = time.time() + grace
    while time.time() < t_end:
        doc = read_json(d / "job.json") if (d / "job.json").exists() else {}
        if doc.get("state") not in ("running", "queued"):
            return
        time.sleep(0.5)
    doc = read_json(d / "job.json")
    pid = doc.get("pid")
    try:
        import psutil
        proc = psutil.Process(int(pid))
        cmd = " ".join(proc.cmdline())
        # only this job's own worker: a process number can be reused by something else
        if "asmfea.jobs.worker" in cmd and job_id in cmd:
            for p in proc.children(recursive=True) + [proc]:
                try:
                    p.kill()
                except psutil.Error:
                    pass
    except Exception:
        pass                                    # gone already
    doc.update(state="cancelled", activity="cancelled: the job did not stop on request, so its process was ended",
               heartbeat=_now())
    write_json_atomic(d / "job.json", doc)


def list_jobs(study: Path) -> list[str]:
    run = study / "run"
    return sorted(p.name for p in run.iterdir() if (p / "job.json").exists()) if run.exists() else []
