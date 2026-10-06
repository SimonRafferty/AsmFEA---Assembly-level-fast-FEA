"""Method checks (DECISIONS D29, D33, D34): the methods for joints and notches, each on a hand-written
neutral model (cases/m11..m14) run through the pipeline and compared with a hand answer. They test
how results are classified, not the SolidWorks extraction (the full-chain cases C1-C4 do that)."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import numpy as np

from .. import defaults


class _Job:
    """The minimum of a job the pipeline stages call, run in this process."""

    def __init__(self, folder: Path):
        self.dir = folder
        self.path = folder / "job.json"
        folder.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"timings": {}}))
        self.diags = []

    def activity(self, *a, **k):
        pass

    def result(self, line):
        pass

    def verify(self, line):
        pass

    def add_diags(self, diags):
        self.diags += diags

    def log(self, line):
        pass

    def solver(self, *a, **k):
        pass

    def cancel_requested(self):
        return False

    def timing(self, *a, **k):
        pass

    def reuse(self, stage, text):
        doc = json.loads(self.path.read_text())
        doc.setdefault("reuse", {})[stage] = text
        self.path.write_text(json.dumps(doc))


def _run(cases: Path, work: Path, name: str, as_name: str, study_edit=None, model_edit=None) -> tuple[Path, _Job]:
    from ..pipeline import STAGES
    dst = work / as_name
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    for item in ("model.json", "study.json", "parts"):
        p = cases / name / item
        (shutil.copytree if p.is_dir() else shutil.copy2)(p, dst / item)
    for fname, edit in (("study.json", study_edit), ("model.json", model_edit)):
        if edit:
            d = json.loads((dst / fname).read_text(encoding="utf-8-sig"))
            edit(d, dst)
            (dst / fname).write_text(json.dumps(d))
    job = _Job(dst / "run" / "check")
    for s in ("resolve", "mesh", "solve", "post"):
        STAGES[s](dst, job)
    return dst, job


def _results(study: Path, case: str) -> dict:
    return json.loads((study / "results" / "results.json").read_text())["load_cases"][case]


def _row(check: str, quantity: str, unit: str, reference: float, got: float, tol: float, where: str) -> dict:
    diff = (got - reference) / reference
    return {"case": check, "quantity": quantity, "unit": unit, "reference": reference, "reference_label": "hand",
            "asmfea": got, "difference": diff, "criterion": "method", "tolerance": tol, "pass": abs(diff) <= tol,
            "solidworks_simulation": None, "sws_difference": None, "where": where, "nodes": None}


def _fidelity(level):
    def edit(d, _):
        d["study"]["mesh"] = {"fidelity": level, "convergence_check": False}
    return edit


def m1_joint_structural_stress(cases: Path, work: Path) -> list[dict]:
    """A plate edge-bonded to a block (m12): the joint's structural stress from the forces it carries."""
    from ..post.frd import read_dat_steps, read_frd
    from ..post.joints import _traction_points
    s, job = _run(cases, work, "m12_plate_edge", "M1_standard", _fidelity("standard"))
    info = json.loads((job.dir / "base" / "deck_info.json").read_text())
    dat = read_dat_steps(job.dir / "base" / "study.dat")
    ids, xyz = read_frd(job.dir / "base" / "study.frd")[0]["COORDS"]
    tab = np.full((ids.max() + 1, 3), np.nan)
    tab[ids] = xyz[:, :3]
    tie = [t for t in info["ties"] if t.get("section")][0]
    P, Tr, W = _traction_points(tie["facets"], dat["nodal"][1][("forces", tie["name"].upper() + "S")], tab)
    F = Tr * W[:, None]
    avg = 6 * abs(float((-P[:, 2] * F[:, 1]).sum())) / (100.0 * 25.0)
    std = _results(s, "Bend")["joints"][0]["structural_stress"]
    sc, _ = _run(cases, work, "m12_plate_edge", "M1_coarse", _fidelity("coarse"))
    coarse = _results(sc, "Bend")["joints"][0]["structural_stress"]
    return [_row("M1", "Joint bending stress averaged over the joint (statics: 6 P L / b t^2)", "MPa", 180.0, avg, 0.01,
                 "the recovered traction on the plate's bonded edge"),
            _row("M1", "Worst joint structural stress, coarse mesh against standard", "MPa", std, coarse, 0.05,
                 "the plate's bonded edge; peak of the moving window")]


def m2_membrane_bending(cases: Path, work: Path) -> list[dict]:
    """A sheet with a hole (m13): membrane plus bending across the ligament from the notch."""
    s, _ = _run(cases, work, "m13_plate_hole", "M2")
    p = _results(s, "Pull")["parts"]["Plate-1"]
    rows = [_row("M2", "Ligament membrane stress (statics: N / 2 / ligament area)", "MPa", 25.0, p["membrane"], 0.10,
                 "stress classification line from the hole's edge across the ligament")]
    r = _row("M2", "Membrane + bending, below the notch peak (reference: the peak)", "MPa", p["peak_vm"], p["membrane_bending"],
             1.0, "the same line")
    r["pass"] = p["membrane"] < p["membrane_bending"] < p["peak_vm"]
    return rows + [r]


def m3_weldment_butt(cases: Path, work: Path) -> list[dict]:
    """A weldment post butted on a chord's side over its corner radii (m11): bonded across the gap."""
    sb, _ = _run(cases, work, "m11_weldment_tee", "M3_bonded")
    bonded = _results(sb, "Side")["max_disp"]

    def one_solid(d, dst):
        import hashlib
        from ..geometry import bbox, read_step, volume_props
        step = dst / "parts" / "Welded.step"
        sol = read_step(step)[0]
        vol, cen = volume_props(sol)
        bb = bbox(sol)
        p = d["parts"][0]
        p.update(id="Frame-1", step="parts/Welded.step", geometry_hash=hashlib.sha1(step.read_bytes()).hexdigest(),
                 body={"volume": vol, "centroid": cen, "bbox_min": bb[:3], "bbox_max": bb[3:]})
        d["parts"] = [p]

    def refs(d, _):
        for r in d["face_refs"]:
            r["part"] = "Frame-1"
    sw, _ = _run(cases, work, "m11_weldment_tee", "M3_welded", study_edit=refs, model_edit=one_solid)
    welded = _results(sw, "Side")["max_disp"]
    return [_row("M3", "Post-top deflection: weldment bond against the frame fused with its V gaps weld-filled", "mm",
                 welded, bonded, 0.15, "largest displacement (the post's top)")]


def m4_receiving_wall(cases: Path, work: Path) -> list[dict]:
    """A plate edge-bonded across a wall clamped at both ends (m14): the wall's structural stress at the toe.
    Research: the method is off in normal runs (D34)."""
    old = defaults.RECEIVING_WALLS
    defaults.RECEIVING_WALLS = True
    try:
        s, _ = _run(cases, work, "m14_wall_tee", "M4")
    finally:
        defaults.RECEIVING_WALLS = old
    walls = [j for j in _results(s, "Pull")["joints"] if j.get("side") == "receiving"]
    got = max(j["structural_stress"] for j in walls) if walls else float("nan")
    return [_row("M4", "Receiving-wall structural stress at the toe (clamped beam: 6 M / b t^2, M = N L / 8 - N t / 4)",
                 "MPa", 57.0, got, 0.15, "the wall under the plate's bonded edge (research: off in normal runs)")]


CHECKS = [
    ("M1", "Structural stress at a bonded joint (Dong / Battelle)", m1_joint_structural_stress),
    ("M2", "Membrane plus bending at a notch (ASME VIII-2 Part 5)", m2_membrane_bending),
    ("M3", "Weldment butt joint bonded across corner radii", m3_weldment_butt),
    ("M4", "Receiving wall of a bonded joint (research, off in normal runs)", m4_receiving_wall),
]


def run(cases: Path, work: Path) -> list[dict]:
    out = []
    for cid, title, fn in CHECKS:
        t0 = time.time()
        rows = fn(Path(cases), Path(work) / "methods")
        out.append({"id": cid, "title": title, "rows": rows, "seconds": time.time() - t0})
    return out
