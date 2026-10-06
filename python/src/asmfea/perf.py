"""Opt-in timing of pipeline steps (INVESTIGATION-001). Off unless ASMFEA_PERF names a file.

    with perf.span("resolver", pairs=n) as info:
        ...
        info["interfaces"] = len(conns)

Each span appends one JSON line to that file when it ends: its name, wall-clock seconds, the peak
memory of this process and its children (mesh workers, Gmsh, CalculiX) and the average number of
busy cores over the span, plus whatever the caller put in ``info``. Spans may nest. With the
variable unset a span costs a dictionary and nothing is sampled or written.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time

SAMPLE_S = 0.2


class _Sampler(threading.Thread):
    """Peak resident memory and CPU seconds of this process tree, sampled every SAMPLE_S. CPU time of
    a child that exits between two samples is counted up to its last sample."""

    def __init__(self):
        super().__init__(daemon=True)
        import psutil
        self._psutil = psutil
        self._me = psutil.Process()
        self._stop = threading.Event()
        self._cpu: dict[int, float] = {}
        self._cpu0 = {pid: cpu for pid, cpu, _ in self._read()}
        self.peak_rss = 0.0

    def _read(self) -> list[tuple[int, float, int]]:
        """(pid, CPU seconds, resident bytes) of every live process in the tree."""
        out = []
        for p in [self._me] + self._me.children(recursive=True):
            try:
                t = p.cpu_times()
                out.append((p.pid, t.user + t.system, p.memory_info().rss))
            except self._psutil.Error:
                pass
        return out

    def sample(self):
        rss = 0
        for pid, cpu, r in self._read():
            self._cpu[pid] = cpu
            rss += r
        self.peak_rss = max(self.peak_rss, rss)

    def run(self):
        while not self._stop.wait(SAMPLE_S):
            self.sample()

    def stop(self) -> float:
        """CPU seconds used by the tree during the span."""
        self._stop.set()
        self.join()
        self.sample()
        return sum(cpu - self._cpu0.get(pid, 0.0) for pid, cpu in self._cpu.items())


@contextlib.contextmanager
def span(name: str, **info):
    path = os.environ.get("ASMFEA_PERF")
    if not path:
        yield info
        return
    sampler = _Sampler()
    sampler.start()
    t0 = time.perf_counter()
    try:
        yield info
    finally:
        wall = time.perf_counter() - t0
        cpu = sampler.stop()
        rec = {"span": name, "seconds": round(wall, 3), "peak_rss_gb": round(sampler.peak_rss / 1e9, 3),
               "busy_cores": round(cpu / wall, 2) if wall > 0 else None, "at": time.time(), **info}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
