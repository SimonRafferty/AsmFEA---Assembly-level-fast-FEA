"""Fast run and full run (ADDENDUM-FAST-RUN-MODE.md, D40).

A fast run is the same pipeline stopped at a cheaper point: the same model, connections, loads and
fixtures, meshed coarser (defaults.FAST_*), one mesh level, no report. Its results go to results_fast/,
so a full run's results/ (the only ones a report is written from) are never overwritten by an
approximate run. The panes show whichever results are newer and say which kind they are.
"""
from __future__ import annotations

from pathlib import Path

FULL, FAST = "full", "fast"


def results_dir(study: Path, mode: str = FULL) -> Path:
    return Path(study) / ("results_fast" if mode == FAST else "results")


def latest_results(study: Path) -> tuple[Path, str]:
    """(results folder, mode) of the newer of the full and the fast results; the full folder when neither."""
    best = None
    for mode in (FULL, FAST):
        f = results_dir(study, mode) / "results.json"
        if f.exists() and (best is None or f.stat().st_mtime > best[0]):
            best = (f.stat().st_mtime, mode)
    mode = best[1] if best else FULL
    return results_dir(study, mode), mode


def mode_of(js) -> str:
    """The run mode a job was started with (job.json "mode"); full when absent."""
    try:
        from .jobs import read_json
        return FAST if read_json(js.path).get("mode") == FAST else FULL
    except (OSError, ValueError, AttributeError):
        return FULL


def latest_solve_file(study: Path, mode: str = FULL) -> Path:
    return Path(study) / "run" / ("latest_solve_fast.txt" if mode == FAST else "latest_solve.txt")


def model_key(model) -> str:
    """What a full run's results depend on in the model (geometry by the add-in's hashes, materials,
    loads, fixtures, connections overrides, study settings), for the report's "is this full run still
    current?" check. The export's time and view are left out."""
    from .stagecache import digest
    d = model.model_dump(by_alias=True, mode="json")
    d["source"] = {k: v for k, v in (d.get("source") or {}).items() if k not in ("timestamp", "view")}
    return digest(d)
