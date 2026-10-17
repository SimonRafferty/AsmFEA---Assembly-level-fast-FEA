"""Model-level checks that need no geometry: references, materials, load cases."""
from __future__ import annotations

from pathlib import Path

from .diagnostics import Diagnostic, make
from .model import Model, SchemaError, load_model


def load_checked(study: Path) -> tuple[Model | None, list[Diagnostic]]:
    if not (study / "model.json").is_file():
        return None, [make("MODEL_FILE_MISSING", file="model.json")]
    try:
        model = load_model(study)
    except SchemaError as e:
        return None, [make("MODEL_SCHEMA_INVALID", file="model.json/study.json", detail=str(e))]
    return model, check_model(model)


def make_issue(code: str, where: dict, params: dict) -> Diagnostic:
    """A diagnostic from an add-in issue. The add-in's params fill the catalogue wording (a
    missing one shows as '?' rather than failing the load) and are kept as evidence."""
    import string
    from .diagnostics import catalogue
    names = {f for _, f, _, _ in string.Formatter().parse(catalogue()[code]["what"]) if f}
    fill = {n: params.get(n, "?") for n in names}
    return make(code, where=where, evidence=dict(params), **fill)


def check_model(model: Model) -> list[Diagnostic]:
    d: list[Diagnostic] = []
    for iss in model.issues:                  # found by the add-in during extraction
        d.append(make_issue(iss.code, iss.where.model_dump(), iss.params))
    parts = {p.id for p in model.parts}
    faces = {f.id for f in model.face_refs}
    for p in model.parts:
        if model.material(p.material) is None:
            d.append(make("MATERIAL_MISSING", where={"parts": [p.id]}, part=p.id, material=p.material))
    for f in model.face_refs:
        if f.part not in parts:
            d.append(make("MODEL_REFERENCE_UNKNOWN", where={"faces": [f.id]}, kind="Part", ref=f.part,
                          owner=f"face {f.id}"))
    for ld in model.loads:
        for fid in ld.faces:
            if fid not in faces:
                d.append(make("MODEL_REFERENCE_UNKNOWN", where={"loads": [ld.id]}, kind="Face", ref=fid,
                              owner=f"load {ld.id}"))
    for fx in model.fixtures:
        for fid in fx.faces:
            if fid not in faces:
                d.append(make("MODEL_REFERENCE_UNKNOWN", where={"fixtures": [fx.id]}, kind="Face", ref=fid,
                              owner=f"fixture {fx.id}"))
    for h in model.hinges:
        for fid in h.faces:
            if fid not in faces:
                d.append(make("MODEL_REFERENCE_UNKNOWN", where={"faces": [fid]}, kind="Face", ref=fid,
                              owner=f"hinge {h.id}"))
    for j in model.joins:
        for fid in j.faces:
            if fid not in faces:
                d.append(make("MODEL_REFERENCE_UNKNOWN", where={"faces": [fid]}, kind="Face", ref=fid,
                              owner=f"join {j.id}"))
    if model.study is not None:
        for pid in model.study.rigid_parts:
            if pid not in parts:
                d.append(make("RIGID_PART_UNKNOWN", where={"parts": [pid]}, part=pid))
    # A connection decision about a part that is not exported (suppressed in this configuration, or
    # deleted) is kept but silently unused: it applies again if the part comes back. The user sees the
    # suppression in the feature tree (found live: a suppressed part blocked the check; the user asked
    # for no message about it).
    for w in model.welds:
        for pid in w.parts:
            if pid not in parts:
                d.append(make("MODEL_REFERENCE_UNKNOWN", where={"parts": [pid]}, kind="Part", ref=pid,
                              owner=f"weld bead {w.id}"))
    if model.study is None:
        d.append(make("MODEL_SCHEMA_INVALID", file="study.json", detail="no 'study' section"))
    else:
        cases = set(model.study.load_cases)
        for c in sorted(cases):
            if not any(ld.load_case == c for ld in model.loads):
                d.append(make("LOAD_CASE_UNDEFINED", case=c, problem="has no loads"))
        for ld in model.loads:
            if ld.load_case not in cases:
                d.append(make("LOAD_CASE_UNDEFINED", where={"loads": [ld.id]}, case=ld.load_case,
                              problem=f"is used by load {ld.id} but is not in the study's load cases"))
    if not model.fixtures:
        d.append(make("NO_FIXTURES"))
    return d
