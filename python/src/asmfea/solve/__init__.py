"""Run CalculiX with live monitoring and turn known failures into diagnostics."""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import psutil

from .. import defaults
from ..config import Config
from ..diagnostics import Diagnostic, make

# Known failure patterns: (regex on the log, code, extra params builder).
# Every entry needs a broken case in tests/test_diagnostics_cases.py.
FAILURES = [
    # nonlinear (contact) incrementation giving up
    (re.compile(r"(increment size smaller than minimum|too many cutbacks|no convergence|"
                r"best solution and residual are|divergence)", re.I), "CONTACT_NOT_CONVERGED"),
    (re.compile(r"(singular|zero pivot|factorization failed|not positive definite|pardiso.*error[^\n]*-4)", re.I),
     "SOLVE_SINGULAR"),
    (re.compile(r"(u_calloc|u_realloc|could not allocate|out of memory|insufficient memory)", re.I),
     "SOLVE_OUT_OF_MEMORY"),
    # CalculiX only WARNS on a card it cannot read, then "finishes" with no results.
    (re.compile(r"Card image cannot be interpreted:?\s*\n.*?Card image:\s*\n\s*(\S[^\n]*)", re.I | re.S),
     "DECK_CARD_REJECTED"),
]
_IGNORED_CARD = re.compile(r"Card image cannot be interpreted", re.I)


@dataclass
class SolveOutcome:
    ok: bool
    seconds: float
    returncode: Optional[int]
    diagnostics: list[Diagnostic]
    cancelled: bool = False
    peak_rss_gb: float = 0.0


def estimate_memory_gb(dof: int) -> float:
    return dof * defaults.BYTES_PER_DOF / 1e9


def memory_check(dof: int) -> list[Diagnostic]:
    ram = psutil.virtual_memory().total / 1e9
    need = estimate_memory_gb(dof)
    if need > defaults.MEMORY_BLOCK_FRACTION * ram:
        # would swap the computer to a standstill (found live): stop before starting the solver
        return [make("SOLVE_MEMORY_EXCEEDS_RAM", need_gb=need, ram_gb=ram, dof=dof,
                     evidence={"dof": dof, "bytes_per_dof": defaults.BYTES_PER_DOF})]
    if need > defaults.MEMORY_WARN_FRACTION * ram:
        return [make("MEMORY_ESTIMATE_HIGH", need_gb=need, frac=defaults.MEMORY_WARN_FRACTION, ram_gb=ram,
                     evidence={"dof": dof, "bytes_per_dof": defaults.BYTES_PER_DOF})]
    return []


def tail(path: Path, n: int = 8) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4000))
            lines = f.read().decode(errors="replace").splitlines()
        return [l for l in lines if l.strip()][-n:]
    except OSError:
        return []


def _kill_tree(proc: psutil.Process) -> None:
    for p in proc.children(recursive=True) + [proc]:
        try:
            p.kill()
        except psutil.Error:
            pass


def run_ccx(cfg: Config, workdir: Path, job: str, dof: int,
            on_tick: Callable[[dict], bool]) -> SolveOutcome:
    """Run ccx -i job in workdir. on_tick(status) is called about once a second;
    it returns True to cancel. status holds elapsed, cpu, rss_gb, log_tail."""
    if not cfg.ccx or not Path(cfg.ccx).is_file():
        return SolveOutcome(False, 0.0, None, [make("SOLVER_NOT_FOUND", path=cfg.ccx or "(not configured)")])
    n = str(cfg.threads)
    env = dict(os.environ, OMP_NUM_THREADS=n, MKL_NUM_THREADS=n, CCX_NPROC_EQUATION_SOLVER=n,
               CCX_NPROC_STIFFNESS=n, CCX_NPROC_RESULTS=n)
    log_path = workdir / "ccx.log"
    t0 = time.perf_counter()
    peak = 0.0
    with open(log_path, "w") as log:
        popen = subprocess.Popen([cfg.ccx, "-i", job], cwd=workdir, stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=env,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        proc = psutil.Process(popen.pid)
        proc.cpu_percent(None)
        while popen.poll() is None:
            time.sleep(defaults.HEARTBEAT_S)
            try:
                cpu = proc.cpu_percent(None) / 100.0
                rss = proc.memory_info().rss / 1e9
            except psutil.Error:
                cpu, rss = 0.0, 0.0
            peak = max(peak, rss)
            status = {"elapsed": time.perf_counter() - t0, "cpu_cores_busy": cpu, "rss_gb": rss,
                      "log_tail": tail(log_path, 5), "solver_pid": popen.pid}
            if on_tick(status):
                _kill_tree(proc)
                popen.wait()
                return SolveOutcome(False, time.perf_counter() - t0, popen.returncode, [], cancelled=True,
                                    peak_rss_gb=peak)
    secs = time.perf_counter() - t0
    text = log_path.read_text(errors="replace")
    frd = workdir / f"{job}.frd"
    # Success needs: no *ERROR, no ignored card, and a results file with real content
    # (an ignored-card run leaves a 6-byte .frd and exit code 0).
    failed = ("*ERROR" in text or _IGNORED_CARD.search(text) is not None
              or not frd.exists() or frd.stat().st_size < 200)
    if not failed:
        return SolveOutcome(True, secs, popen.returncode, [], peak_rss_gb=peak)
    excerpt = "\n".join(tail(log_path, 25))
    return SolveOutcome(False, secs, popen.returncode, classify_log(text, popen.returncode, dof, excerpt),
                        peak_rss_gb=peak)


def classify_log(text: str, returncode: Optional[int], dof: int, excerpt: str) -> list[Diagnostic]:
    """Map a failed CalculiX log to a diagnostic; unknown failures say so plainly."""
    for rx, code in FAILURES:
        m = rx.search(text)
        if m:
            if code in ("SOLVE_SINGULAR", "CONTACT_NOT_CONVERGED"):
                return [make(code, detail="", raw=excerpt, evidence={"matched": m.group(0)})]
            if code == "DECK_CARD_REJECTED":
                return [make(code, card=m.group(1).strip(), raw=excerpt)]
            return [make(code, dof=dof, raw=excerpt, evidence={"matched": m.group(0)})]
    return [make("SOLVE_FAILED_UNKNOWN", exit_code=returncode, raw=excerpt)]


def run_hybrid(workdir: Path, job: str, on_tick: Optional[Callable[[dict], bool]] = None) -> SolveOutcome:
    """Solve ``workdir``/``job``.inp with the hybrid solver (asmfea.hybrid, D49) instead of CalculiX: each part
    condensed onto its joints, for a model CalculiX cannot hold in memory. Writes the same .frd/.dat.

    It runs on a worker thread while this one reports progress every second through ``on_tick`` (the job's
    heartbeat; a heavy part can take a minute) and passes on a cancel. Raises hybrid.Unsupported, before any
    solving, for a deck it cannot solve (buckling, contact, bolts, equations), so the caller can refuse as before."""
    import threading
    from ..hybrid import Settings, run
    from ..hybrid.deckread import Unsupported
    from ..hybrid.solve import Singular

    class _Cancel(Exception):
        pass

    state = {"msg": "starting", "done": False, "error": None, "cancel": False}

    def log(s: str) -> None:
        state["msg"] = s
        if state["cancel"]:
            raise _Cancel()

    def work() -> None:
        try:
            state["timings"] = run(workdir, job, settings=Settings(degree=defaults.HYBRID_DEGREE, exact_max_nodes=0),
                                   log=log)
        except BaseException as e:            # noqa: BLE001 - handed to the caller's thread below
            state["error"] = e
        finally:
            state["done"] = True

    t0 = time.perf_counter()
    proc = psutil.Process()
    peak = 0.0
    proc.cpu_percent(None)
    th = threading.Thread(target=work, name="asmfea-hybrid", daemon=True)
    th.start()
    while not state["done"]:
        th.join(1.0)
        rss = proc.memory_info().rss / 1e9
        peak = max(peak, rss)
        if on_tick is not None and on_tick({"elapsed": time.perf_counter() - t0, "rss_gb": rss,
                                            "cpu_cores_busy": proc.cpu_percent(None) / 100.0,
                                            "log_tail": [state["msg"]], "solver": "hybrid solver"}):
            state["cancel"] = True
    seconds = time.perf_counter() - t0
    err = state["error"]
    if isinstance(err, _Cancel):
        return SolveOutcome(False, seconds, None, [], cancelled=True, peak_rss_gb=peak)
    if isinstance(err, Unsupported):
        raise err
    if isinstance(err, Singular):
        return SolveOutcome(False, seconds, None, [make("SOLVE_SINGULAR", detail=f" ({err})", raw=str(err))],
                            peak_rss_gb=peak)
    if err is not None:
        raise err
    return SolveOutcome(True, seconds, 0, [], peak_rss_gb=peak)
