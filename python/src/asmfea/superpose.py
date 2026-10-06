"""Load superposition (INVESTIGATION-001, D39): solve each load on its own once; every load case is then
the weighted sum of its loads' results, so a change to the size of loads needs no new solve.

Exact for a linear static model (displacement, stress tensor, reactions and nodal forces are linear in
the loads; von Mises is computed by post from the summed tensor, never summed itself): verified to the
6 printed figures of CalculiX's results against a fresh solve (docs/perf/REPORT-002.md). Not used with
buckling, separating contact or preloaded bolts, whose results are not linear in the loads.

Layout of a superposed level (run/<job>/<level>/):
  loads/   the CalculiX deck with one static step per load (deck_info["superposition"] maps each load to
           its step and the magnitude it was solved at), with its .frd and .dat
  cases/   what post reads, rebuilt from the current loads before every post: deck_info.json (each case's
           step, applied load and load into supports), and study.frd / study.dat STUBS naming the source
           and each case's terms (step, weight). The results readers (post/frd.py) recognise a stub and
           return the weighted sums. A stub's content changes with the weights, so everything keyed on
           file content (post's key, the binary results copy, the stored stress classification) follows.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

STUB = "ASMFEA-SUPERPOSED"
LINEAR_BLOCKS = ("DISP", "STRESS", "FORC")    # .frd blocks linear in the loads (ERROR, an estimate, is not)


def pseudo_case(load_id: str) -> str:
    return f"LOAD:{load_id}"


def not_usable(model, conns: list[dict], bolts: list[dict]) -> str | None:
    """Why superposition cannot be used for this model, or None when it can."""
    if "buckling" in model.study.analyses:
        return "the study includes buckling, whose load factor is not linear in the loads"
    if any(c["type"] == "contact" for c in conns):
        return "the model has separating contact, which is not linear"
    if bolts:
        return "the model has preloaded bolts, which are not linear"
    return None


def per_load_model(model):
    """A copy of the model with each load in a load case of its own (at its own size; a load of size zero
    is solved at size one so it can be scaled later), buckling off."""
    m = model.model_copy(deep=True)
    for ld in m.loads:
        ld.load_case = pseudo_case(ld.id)
        if not ld.magnitude:
            ld.magnitude = 1.0
    m.study.load_cases = [pseudo_case(ld.id) for ld in m.loads]
    m.study.buckling_cases = []
    m.study.analyses = ["static"]
    return m


def solved_map(info: dict, model_solved) -> dict:
    """deck_info["superposition"]: {load id: {"step": k, "magnitude": solved size}}."""
    step_of = {s["case"]: s["step"] for s in info["steps"] if s["kind"] == "static"}
    return {ld.id: {"step": step_of[pseudo_case(ld.id)], "magnitude": float(ld.magnitude)} for ld in model_solved.loads}


def prepare_cases(level_dir: Path, model) -> None:
    """(Re)write level_dir/cases/ for the model's current loads: the combined deck_info and the stubs."""
    src = level_dir / "loads"
    info = json.loads((src / "deck_info.json").read_text())
    solved = info["superposition"]
    out = level_dir / "cases"
    out.mkdir(exist_ok=True)
    steps, terms, cases_info, remote = [], {}, {}, {}
    for k, case in enumerate(model.study.load_cases, start=1):
        t = []
        applied, into = np.zeros(3), np.zeros(3)
        for ld in (ld for ld in model.loads if ld.load_case == case):
            s = solved[ld.id]
            w = float(ld.magnitude) / s["magnitude"]
            t.append([s["step"], w])
            li = info["load_cases"][pseudo_case(ld.id)]
            applied += w * np.asarray(li["applied"])
            into += w * np.asarray(li["into_supports"])
            if ld.id in info.get("remote_loads", {}):
                r = dict(info["remote_loads"][ld.id])
                r["moment"] = (w * np.asarray(r["moment"])).tolist()
                remote[ld.id] = r
        steps.append({"kind": "static", "case": case, "step": k})
        terms[str(k)] = t
        cases_info[case] = {"applied": applied.tolist(), "into_supports": into.tolist(),
                            "loads": [ld.id for ld in model.loads if ld.load_case == case]}
    combined = dict(info, steps=steps, load_cases=cases_info, remote_loads=remote, kind="linear (superposed loads)")
    combined.pop("superposition", None)
    _write_if_changed(out / "deck_info.json", json.dumps(combined))
    from .stagecache import file_digest
    for name in ("study.frd", "study.dat"):
        spec = {"source": f"../loads/{name}", "source_sha": file_digest(src / name) if (src / name).exists() else None,
                "steps": terms}
        _write_if_changed(out / name, STUB + "\n" + json.dumps(spec, sort_keys=True) + "\n")


def _write_if_changed(path: Path, text: str) -> None:
    """Rewrite only when the content differs (keeps the binary results copy and its key valid)."""
    try:
        if path.read_text() == text:
            return
    except OSError:
        pass
    path.write_text(text)


def read_stub(path: Path) -> dict | None:
    """The spec of a stub results file, or None when path is a real CalculiX file."""
    try:
        with open(path, "rb") as f:
            if f.read(len(STUB)) != STUB.encode():
                return None
        spec = json.loads(Path(path).read_text().split("\n", 1)[1])
    except (OSError, ValueError, IndexError):
        return None
    spec["source_path"] = (Path(path).parent / spec["source"]).resolve()
    return spec


def combine_frd(spec: dict, source: dict) -> dict:
    """The weighted sums of the source .frd's per-load steps, one step per case; node coordinates kept."""
    out = {0: source[0]} if 0 in source else {}
    for k, terms in spec["steps"].items():
        blocks = {}
        for name in LINEAR_BLOCKS:
            parts = [(source[s][name], w) for s, w in terms if name in source.get(s, {})]
            if not parts or len(parts) != len(terms):
                continue
            ids = parts[0][0][0]
            acc = np.zeros_like(parts[0][0][1], dtype=float)
            for (ids_i, vals), w in parts:
                if len(ids_i) != len(ids) or not np.array_equal(ids_i, ids):
                    raise ValueError(f"superposition: block {name} has different nodes in different load steps")
                acc += w * vals
            blocks[name] = (ids, acc)
        out[int(k)] = blocks
    return out


def combine_dat(spec: dict, source: dict) -> dict:
    """read_dat_steps' {"totals": {step: {set: vec}}, "nodal": {step: {key: {node: vec}}}}, combined."""
    totals, nodal = {}, {}
    for k, terms in spec["steps"].items():
        k = int(k)
        tot: dict = {}
        nod: dict = {}
        for s, w in terms:
            for name, vec in source["totals"].get(s, {}).items():
                tot[name] = tot.get(name, 0.0) + w * np.asarray(vec, dtype=float)
            for key, per_node in source["nodal"].get(s, {}).items():
                d = nod.setdefault(key, {})
                for n, vec in per_node.items():
                    d[n] = d.get(n, 0.0) + w * np.asarray(vec, dtype=float)
        totals[k], nodal[k] = tot, nod
    return {"totals": totals, "nodal": nodal}


def combine_totals(spec: dict, source: list) -> list:
    """read_dat_totals' list (one array per step, in step order), combined."""
    out = []
    for k in sorted(spec["steps"], key=int):
        acc = None
        for s, w in spec["steps"][k]:
            v = w * np.asarray(source[s - 1], dtype=float)
            acc = v if acc is None else acc + v
        out.append(acc if acc is not None else np.zeros(3))
    return out
