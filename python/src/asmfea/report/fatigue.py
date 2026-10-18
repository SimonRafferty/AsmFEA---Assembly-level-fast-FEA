"""The fatigue documents (release 2 F4, D107; D114): a half-page summary, a short report on the minimum, and a data
report with every item, separate from the static documents.

Written by the fatigue stage from results/fatigue.json. They name the static results they were made from (the job
and a hash of its results.json), so a static report and a fatigue report can never be read against each other's
results unnoticed. The summary and the report give the governing (lowest-factor) item only, since a list of every
item gets out of hand on a rig of hundreds of parts (the user's request, 2026-10-17); every item is in the data report.
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

from ..jobs import read_json
from ..model import load_model
from ..stagecache import file_digest
from . import (BLUE, GREY, HEADER_FILL, STATUS_COLOUR, _borders, _font, _header_footer, _kv_table, _para, _picture,
               _shade, _style_document, _table, _template, _trimmed, _widths, to_pdf)
from . import assumptions as A

# the kinds of item, by the prefix of their name in _items, and how the report names the lowest of each
KINDS = (("Part", "Parent material"), ("Weld", "Bonded joint (weld, member side)"),
         ("Metal", "Wall under a bonded joint"), ("Bolt", "Preloaded bolt"))

# warnings the report counts in one line each, instead of listing every one (they are all in the data report)
NOTE_WORDS = {
    "FATIGUE_RECEIVING_SIDE_INDICATIVE": "joint wall(s) read from the bonded model as an indication only",
    "FATIGUE_RECEIVING_SIDE_NOT_ASSESSED": "joint wall(s) not assessed: check by hand",
    "FATIGUE_HOTSPOT_UNCONVERGED": "item(s) whose stress range changed by more than the tolerance between the last "
                                   "two meshes",
    "FATIGUE_SHARP_NOTCH": "part(s) whose highest damage was at a sharp inside corner (no finite stress; the next "
                           "point is reported)",
    "FATIGUE_BOLT_NOT_ASSESSED": "bolted joint group(s) not in the preloaded mode: bolts not assessed",
}


def _f(v, nd=3) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and math.isinf(v):
        return "no damage"
    return f"{v:.{nd}g}"


def _items(doc: dict, ids: bool = False) -> list[tuple[str, dict]]:
    """Every assessed item named by its parts (the user, 2026-10-18, D134: no joint ids in the reports), ranked by
    factor. ids: the joint's connection id after the name, for the data report, where several welds can join the
    same two parts."""
    tag = (lambda c: f" [{c}]") if ids else (lambda c: "")
    out = [(f"Part {p}", v) for p, v in doc.get("parts", {}).items()]
    out += [(f"Weld joining {v['part']} and {v['other']}{tag(c)}", v) for c, v in doc.get("joints", {}).items()]
    out += [(f"Metal of {v['part']} beside its weld to {v['other']}"
             f"{' (indicative)' if 'indicative' in v['method'] else ''}{tag(c)}", v)
            for c, v in doc.get("walls", {}).items()]
    out += [(f"Bolt {b}{tag(v['connection'])}", v) for b, v in doc.get("bolts", {}).items()]
    # "no damage" is an infinite factor, written to JSON as null
    return sorted(out, key=lambda kv: math.inf if kv[1].get("factor_k") is None else kv[1]["factor_k"])


def _where(g: dict) -> str:
    return "(" + ", ".join(f"{c:.0f}" for c in g["at"]) + ")" if g.get("at") else "-"


def _basis(study: Path, doc: dict) -> tuple[str, str]:
    """(overall status, how current the fatigue results are against the static ones)."""
    items = _items(doc)
    overall = "PASS" if items and all(v["pass"] for _, v in items) else "FAIL" if items else "INFO"
    current = (study / "results" / "results.json").exists() and \
        file_digest(study / "results" / "results.json") == doc.get("static_results")
    return overall, "current" if current else "OUT OF DATE: the static results have changed since; run fatigue again"


def _open(study: Path):
    """(study, fatigue.json, model, a new document, assembly name, study name, now)."""
    study = Path(study)
    doc = read_json(study / "results" / "fatigue.json")
    model = load_model(study)
    tpl = _template(study)
    d = Document(str(tpl)) if tpl else Document()
    if not tpl:
        _style_document(d)
    return study, doc, model, d, Path(model.source.assembly).stem, model.study.name, datetime.now()


def _save(d, path: Path, pdf: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))
    if pdf:
        to_pdf(path)
    return path


def _iso(study: Path):
    imgs = sorted((study / "results").glob("fatigue_damage_*.png"))
    return next((p for p in imgs if p.stem.endswith("_iso")), imgs[0] if imgs else None)


def _governing_rows(items) -> list[list[str]]:
    """The governing (lowest-factor) item in a few rows: what, the factor, damage and life, where, the curve."""
    if not items:
        return [["Lowest fatigue factor", "nothing was assessed"]]
    name, v = items[0]
    g = v["governing"]
    rng = g.get("range", g.get("stress_range"))
    rows = [["Lowest fatigue factor", f"{_f(v['factor_k'])} on stress"],
            ["At", name],
            ["Damage over the design life", f"{_f(v['damage'])} (fails at 1)"],
            ["Life", f"{_f(v['life_blocks'])} times the design spectrum"],
            ["Governing", f"spectrum row {g['row']}, stress range {_f(rng, 4)} MPa"
                          + (f" at {_where(g)} mm" if g.get("at") else "")],
            ["S-N curve", v["curve"] + (" (ESTIMATED from the tensile strength)" if v.get("curve_estimated") else "")]]
    if "indicative" in v.get("method", ""):
        rows.append(["Note", "indicative only: read from the bonded model, not from a hand formula"])
    return rows


def _plain_rows(doc: dict, items, overall: str, current: str) -> list[list[str]]:
    """The summary's figures in plain words, for a safety officer (the user, 2026-10-18, D134): the result and what it
    means, the loading, the margin, the weakest place and the life used. The engineer's terms (factor on stress,
    damage, spectrum rows, S-N curve) stay in the report and the data report."""
    blocks = float(doc.get("design_blocks") or 1)
    loading = "; ".join(f"{r['cycles'] * blocks:,.0f} cycles of {r['case']} ({r['min_pct']:g} % to "
                        f"{r['max_pct']:g} % of that load"
                        + (f", against {r['min_case']}" if r.get("min_case") and r["min_case"] != r["case"] else "")
                        + ")" for r in doc["spectrum"])
    rows = [["Result", {"PASS": "Pass: expected to last its design life under the repeated loading given",
                        "FAIL": "Fail: fatigue cracks are expected within the design life"}.get(overall, overall)],
            ["Repeated loading", loading]]
    if not items:
        return rows + [["Checked", "nothing could be checked"]]
    name, v = items[0]
    k = v.get("factor_k")
    if k is None or k == float("inf"):
        margin = "no fatigue damage at all at these loads"
    elif k >= 1:
        margin = f"{_f(k)}: the stresses could be {_f(k)} times higher before fatigue cracks are expected"
    else:
        margin = (f"{_f(k)}: below 1, so cracks are expected; the stresses would have to fall to {k:.0%} of "
                  "these to last the design life")
    dmg = float(v.get("damage") or 0.0)
    used = ("practically none (less than 0.01 %)" if dmg < 1e-4 else f"{dmg:.1%}" if dmg < 10
            else f"{dmg:.0f} times its fatigue life")
    where = name.split(" ", 1)[-1]
    kind = name.split(" ", 1)[0].lower()               # part, weld, metal or bolt (_items)
    rows += [["Safety margin", margin],
             ["Weakest place", {"part": f"{where} (marked on the picture)",
                                "weld": f"the weld joining {v.get('part')} and {v.get('other')}",
                                "metal": f"the metal of {v.get('part')} beside its weld to {v.get('other')}",
                                "bolt": f"bolt {where}"}.get(kind, name)],
             ["Fatigue life used", used]]
    if v.get("curve_estimated"):
        rows.append(["Fatigue data", f"estimated from the strength of {v.get('material') or 'the material'} (no "
                                     "fatigue test data): an engineer may want test data if the margin is small"])
    rows.append(["Stresses used", "up to date" if current == "current"
                 else "OUT OF DATE: the design or loads changed since; click Assess fatigue again"])
    return rows


def build_fatigue_summary(study: Path, pdf: bool = False) -> Path:
    """Half a page: the governing item's figures beside the damage plot."""
    study, doc, model, d, asm_name, study_name, when = _open(study)
    overall, current = _basis(study, doc)
    items = _items(doc)
    _para(d, f"Fatigue assessment summary: {asm_name}", 15, True, BLUE)
    _para(d, f"Study {study_name}  |  {when:%d %B %Y}  |  {doc['standard_name']}", 8.5, colour=GREY)

    # the figures on the left, the damage plot on the right (as the static summary)
    layout = d.add_table(rows=1, cols=2)
    _widths(layout, [8.0, 9.0])
    left, right = layout.rows[0].cells
    left.text = ""
    rows = _plain_rows(doc, items, overall, current)
    t = left.add_table(rows=0, cols=2)
    _borders(t)
    for k, v in rows:
        a, b = t.add_row().cells
        a.text = b.text = ""
        _font(a.paragraphs[0].add_run(k), 8.5, True)
        _shade(a, HEADER_FILL)
        _font(b.paragraphs[0].add_run(str(v)), 8.5, k == "Result", STATUS_COLOUR.get(overall) if k == "Result" else None)
    _widths(t, [2.6, 5.0])
    left.paragraphs[0].paragraph_format.space_after = Pt(0)
    iso = _iso(study)
    pr = right.paragraphs[0]
    pr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if iso is not None:
        pr.add_run().add_picture(_trimmed(iso), width=Cm(8.6))
        cap = right.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _font(cap.add_run("How much of its fatigue life each area uses over the design life: dark blue very little, "
                          "red cracking expected; grey not checked"), 8, colour=GREY)

    # plain words for a safety officer (the user, 2026-10-18, D134): what was checked and who has to act
    codes = [w["code"] for w in doc.get("warnings", [])]
    walls, bolts = codes.count("FATIGUE_RECEIVING_SIDE_NOT_ASSESSED"), codes.count("FATIGUE_BOLT_NOT_ASSESSED")
    note = (f"{len(items)} places were checked: the metal of each part, each weld, and the metal beside each weld, "
            f"to {doc['standard_name']}.")
    unchecked = [t for t in (f"the metal beside {walls} weld(s)" if walls else "",
                             f"{bolts} group(s) of bolts" if bolts else "") if t]
    if unchecked:
        note += (f" Not checked by this method: {' and '.join(unchecked)}; an engineer should check them by hand.")
    note += f" For the engineer: {study_name}_fatigue_report.docx and {study_name}_fatigue_data.docx."
    _para(d, note, 7.5, colour=GREY)
    return _save(d, study / "report" / f"{study_name}_fatigue_summary.docx", pdf)


def build_fatigue_report(study: Path, pdf: bool = False) -> Path:
    """The fatigue report: the result and the plot first, then the lowest item of each kind, the spectrum, what to
    note and the assumptions."""
    study, doc, model, d, asm_name, study_name, when = _open(study)
    overall, current = _basis(study, doc)
    items = _items(doc)
    _header_footer(d, f"Fatigue assessment of {asm_name}  |  {study_name}", f"{when:%d %B %Y}",
                   f"{study_name}_fatigue_report.docx")
    _para(d, f"Fatigue assessment: {asm_name}", 16, True, BLUE)
    _para(d, f"Study {study_name}  |  {when:%d %B %Y}  |  {doc['standard_name']}", 9, colour=GREY)

    d.add_heading("1. Result", 1)
    p = _para(d, "Overall result: ", 10)
    _font(p.add_run(overall), 10, True, STATUS_COLOUR.get(overall))
    _kv_table(d, _governing_rows(items) + [
        ["Design life", f"the spectrum ({len(doc['spectrum'])} row(s)) repeated {doc['design_blocks']:g} time(s)"],
        ["Static results", f"job {doc['static_job']}, {doc['level']} mesh: {current}"]], size=9)
    iso = _iso(study)
    if iso is not None:
        _picture(d, iso, "Fatigue damage over the design life, log10 (0 = failure); at the joints, the weld's or the wall's beside it; "
                         "grey: not assessed (next to supports and load edges); the lowest "
                         "fatigue factors marked", 13.0)

    d.add_heading("2. Lowest of each kind", 1)
    rows = []
    for prefix, label in KINDS:
        of_kind = [(n, v) for n, v in items if n.startswith(prefix)]
        if of_kind:
            n, v = of_kind[0]
            note = ("indicative" if "indicative" in v.get("method", "") else
                    "S-N estimated" if v.get("curve_estimated") else "")
            rows.append([f"{label} ({len(of_kind)})", n, _f(v["factor_k"]), _f(v["damage"]),
                         "PASS" if v["pass"] else "FAIL", note])
    if rows:
        _table(d, ["Kind (number assessed)", "Lowest", "Factor", "Damage", "Result", "Note"], rows,
               [3.6, 6.4, 1.5, 1.6, 1.3, 2.4], size=8)
    else:
        _para(d, "Nothing was assessed.", 9)

    d.add_heading("3. Loading spectrum", 1)
    _table(d, ["Row", "Load case at max", "Max %", "Load case at min", "Min %", "Cycles per block"],
           [[str(i), r["case"], f"{r['max_pct']:g}", r.get("min_case") or r["case"], f"{r['min_pct']:g}",
             f"{r['cycles']:.4g}"] for i, r in enumerate(doc["spectrum"], start=1)], [1.2, 3.5, 1.6, 3.5, 1.6, 3.0])

    d.add_heading("4. To note", 1)
    counts: dict[str, int] = {}
    for w in doc.get("warnings", []):
        # warnings, and the walls read as an indication (they qualify results shown here); other notes ask nothing
        # of the reader (D132)
        if w.get("severity", "warning") != "info" or w["code"] == "FATIGUE_RECEIVING_SIDE_INDICATIVE":
            counts[w["code"]] = counts.get(w["code"], 0) + 1
    lines = [f"{n} {NOTE_WORDS[c]}." for c, n in counts.items() if c in NOTE_WORDS]
    est = sorted({v["material"] for _, v in items if v.get("curve_estimated") and v.get("material")})
    if est:
        lines.append(f"S-N data estimated from the tensile strength (no measured data) for {', '.join(est)}.")
    if doc.get("parts_not_assessed"):
        lines.append(f"{len(doc['parts_not_assessed'])} part(s) had nothing to assess (every point next to a joint, "
                     "support or load edge).")
    others = sum(n for c, n in counts.items() if c not in NOTE_WORDS)
    if others:
        lines.append(f"{others} other warning(s).")
    for line in lines or ["Nothing."]:
        _para(d, "- " + line, 9)
    _para(d, f"Every item, the S-N data and their sources, and every warning in full: {study_name}_fatigue_data.docx.",
          9, colour=GREY)

    d.add_heading("5. Assumptions", 1)
    for line in A.fatigue(doc):
        _para(d, "- " + line, 9)
    return _save(d, study / "report" / f"{study_name}_fatigue_report.docx", pdf)


def build_fatigue_data(study: Path, pdf: bool = False) -> Path:
    """The fatigue data report: the basis, every item ranked, the S-N data with their sources, what was not assessed
    and every warning."""
    study, doc, model, d, asm_name, study_name, when = _open(study)
    overall, current = _basis(study, doc)
    items = _items(doc, ids=True)
    _header_footer(d, f"Fatigue data of {asm_name}  |  {study_name}", f"{when:%d %B %Y}",
                   f"{study_name}_fatigue_data.docx")
    _para(d, f"Fatigue data: {asm_name}", 16, True, BLUE)
    _para(d, f"Study {study_name}  |  {when:%d %B %Y}  |  the data behind {study_name}_fatigue_report.docx", 9,
          colour=GREY)

    d.add_heading("A. Basis", 1)
    _kv_table(d, [
        ["Assembly", asm_name], ["Study", study_name], ["Standard", doc["standard_name"]],
        ["Static results", f"job {doc['static_job']}, {doc['level']} mesh (results {doc['static_results'][:12]}): "
                           f"{current}"],
        ["Spectrum", f"{len(doc['spectrum'])} row(s), repeated {doc['design_blocks']:g} time(s) in the design life"],
        ["Mean stress (user curves)", doc["mean_stress"]],
        ["Partial factors", f"gamma_Ff {doc['gamma_Ff']:g}, gamma_Mf {doc['gamma_Mf']:g}"],
        ["Result", overall]])

    d.add_heading("B. Every item", 1)
    _para(d, "Ranked by the fatigue factor (the factor on every stress at which the damage over the design life "
             "reaches 1). Damage 1 or more fails.", 9)
    rows = []
    for name, v in items:
        g = v["governing"]
        conv = (v.get("convergence") or {}).get("change")
        rng = g.get("range", g.get("stress_range"))
        rows.append([name, v["curve"], _f(v["damage"]), _f(v["life_blocks"]), _f(v["factor_k"]), str(g["row"]),
                     _f(rng, 4), _where(g), f"{100 * conv:+.1f} %" if conv is not None else "-",
                     "PASS" if v["pass"] else "FAIL"])
    if rows:
        _table(d, ["Item", "S-N curve", "Damage", "Life (blocks)", "Factor", "Row", "Range (MPa)", "At (mm)",
                   "Mesh change", "Result"], rows, [3.0, 3.4, 1.4, 1.5, 1.2, 0.8, 1.4, 2.2, 1.4, 1.2], size=7.5)
    else:
        _para(d, "Nothing was assessed.", 9)

    d.add_heading("C. S-N data", 1)
    curves = {}
    for _, v in items:
        curves.setdefault(v["curve"], (v.get("curve_source", "-"), v.get("curve_checked", True), v.get("curve_estimated")))
    status = lambda ok, est: ("ESTIMATED from the tensile strength (not measured)" if est else  # noqa: E731
                              "two sources agree" if ok else "one source only")
    _table(d, ["Curve", "Source", "Status"],
           [[c, s, status(ok, est)] for c, (s, ok, est) in curves.items()], [5.0, 9.0, 3.0], size=8)

    d.add_heading("D. Not assessed", 1)
    lines = [f"Part {p}: {why}." for p, why in doc.get("parts_not_assessed", {}).items()]
    lines += [f"Part {p}: the highest damage was at a sharp inside corner at {_where(v['sharp_notch_refused'])}, "
              "which has no finite stress; the next point is reported." for p, v in doc.get("parts", {}).items()
              if v.get("sharp_notch_refused")]
    for line in lines or ["Nothing left out."]:
        _para(d, "- " + line, 9)

    d.add_heading("E. Warnings", 1)
    for w in doc.get("warnings", []) or [{"what": "None.", "actions": []}]:
        _para(d, "- " + w["what"] + (" " + " ".join(w["actions"]) if w.get("actions") else ""), 9)
    return _save(d, study / "report" / f"{study_name}_fatigue_data.docx", pdf)


def fatigue_data_path(study: Path) -> Path:
    return Path(study) / "report" / f"{load_model(Path(study)).study.name}_fatigue_data.docx"
