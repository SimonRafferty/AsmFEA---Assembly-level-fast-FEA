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


MODEL_KEY_VERSION = "2:"


def _without(d, key: str):
    """``d`` with ``key`` left out of every object, however deep."""
    if isinstance(d, dict):
        return {k: _without(v, key) for k, v in d.items() if k != key}
    if isinstance(d, list):
        return [_without(v, key) for v in d]
    return d


def _model_dump(model, exclude_defaults: bool) -> dict:
    d = model.model_dump(by_alias=True, mode="json", exclude_defaults=exclude_defaults)
    d["source"] = {k: v for k, v in (d.get("source") or {}).items() if k not in ("timestamp", "view")}
    if d.get("study"):
        d["study"] = {k: v for k, v in d["study"].items() if k != "fatigue"}     # not a static input (D105)
    return d


def model_key(model) -> str:
    """What a full run's results depend on in the model (geometry by the add-in's hashes, materials,
    loads, fixtures, connections overrides, study settings), for the report's "is this full run still
    current?" check. Left out (D115): the export's time and view; the faces' SolidWorks persistent
    references, which change when the assembly is only saved (faces are matched by fingerprint, the
    references only find them again); and fields at their defaults, so a field added to the model
    in a later version does not make every earlier result look out of date."""
    from .stagecache import digest
    return MODEL_KEY_VERSION + digest(_without(_model_dump(model, True), "persistent_ref"))


def model_key_matches(stored: str, model) -> bool:
    """True when a full run's stored model key is the model's now (a key from before D115 by the old formula)."""
    from .stagecache import digest
    if stored.startswith(MODEL_KEY_VERSION):
        return stored == model_key(model)
    return stored == digest(_model_dump(model, False))
