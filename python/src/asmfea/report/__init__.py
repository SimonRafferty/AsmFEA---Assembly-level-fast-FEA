"""Word report from results.json (python-docx, MIT; optional extra asmfea[report]).

Laid out after the reports engineers know from SolidWorks Simulation (not a copy): a title
page, a contents list, then Description, Summary, Assumptions, Model information, Study
properties, Units, Material properties, Loads and fixtures, Connections, Mesh
information, Resultant forces, Study results (a Name / Type / Min / Max table with the
plots beneath), Conclusion and an appendix. Arial throughout, shaded table headers, a
page header and footer.

Styling comes from a template when the user supplies one (the first of
<study>/report_template.docx or [report] template in asmfea.toml); otherwise the built-in
style below. Assumption wording lives in report/assumptions.py. Optional PDF conversion
uses Microsoft Word through COM when installed, else LibreOffice headless.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .. import defaults
from ..config import load_config
from ..jobs import read_json
from ..model import load_model
from ..post import assessed_stress
from . import assumptions as A

FONT = "Arial"
BLUE = RGBColor(0x1F, 0x4E, 0x79)
GREY = RGBColor(0x59, 0x59, 0x59)
HEADER_FILL = "D9E2F3"        # table header shading
BORDER = "A6A6A6"
STATUS_COLOUR = {"PASS": RGBColor(0x1B, 0x7F, 0x2A), "WARN": RGBColor(0xB2, 0x6B, 0x00),
                 "FAIL": RGBColor(0xB0, 0x1C, 0x1C), "INFO": RGBColor(0x55, 0x5B, 0x66)}
KIND_TEXT = {"solid": "Solid body", "sheet": "Sheet", "member": "Structural member", "fastener": "Fastener",
             "weld_bead": "Weld bead"}
CONN_TEXT = {"tie": "Bonded", "contact": "Contact", "bolted": "Bolt", "revolute": "Pin (revolute)",
             "hinge": "Hinge", "slide": "Sliding, no separation", "screw": "Screw",
             "gear": "Gear coupling", "rack_pinion": "Rack and pinion coupling",
             "sliding": "Sliding", "none": "No connection"}


# --------------------------------------------------------------------------- formatting

def _fmt(v, nd=4) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if 1e4 <= abs(v) < 1e12:
            return f"{v:,.0f}"                 # 2,000,000 rather than 2e+06
        return f"{v:.{nd}g}"
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) >= 1e4:
        return f"{v:,}"
    return str(v)


def _font(run, size: Optional[float] = None, bold: Optional[bool] = None, colour: Optional[RGBColor] = None):
    run.font.name = FONT
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    for k in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(k), FONT)
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if colour is not None:
        run.font.color.rgb = colour
    return run


def _style_document(doc) -> None:
    """Arial everywhere, blue headings, A4 with 2 cm margins (the built-in style)."""
    sizes = {"Normal": 10, "Title": 26, "Heading 1": 15, "Heading 2": 12.5, "Heading 3": 11, "List Bullet": 10,
             "Caption": 9}
    for name, size in sizes.items():
        try:
            st = doc.styles[name]
        except KeyError:
            continue
        st.font.name = FONT
        st.font.size = Pt(size)
        rpr = st.element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.append(fonts)
        for k in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            fonts.set(qn(k), FONT)
        for t in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
            if fonts.get(qn(t)) is not None:
                del fonts.attrib[qn(t)]
        if name.startswith("Heading") or name == "Title":
            st.font.color.rgb = BLUE
            st.font.bold = True
    for s in doc.sections:
        s.page_height, s.page_width = Cm(29.7), Cm(21.0)
        s.left_margin = s.right_margin = Cm(2.0)
        s.top_margin, s.bottom_margin = Cm(2.2), Cm(2.0)


def _shade(cell, fill: str) -> None:
    tcpr = cell._element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcpr.append(shd)


def _borders(table, colour: str = BORDER) -> None:
    tblpr = table._element.tblPr
    b = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), "4")
        e.set(qn("w:color"), colour)
        b.append(e)
    tblpr.append(b)


def _table(doc, header: list[str], rows: list[list], widths: Optional[list[float]] = None, size: float = 8.5):
    """A report table: shaded bold header row, light borders, Arial. Cell text may hold line
    breaks (shown as separate lines)."""
    t = doc.add_table(rows=1, cols=len(header))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    _borders(t)
    for c, h in zip(t.rows[0].cells, header):
        c.text = ""
        _font(c.paragraphs[0].add_run(h), size, True)
        _shade(c, HEADER_FILL)
    for row in rows:
        cells = t.add_row().cells
        for c, v in zip(cells, row):
            c.text = ""
            lines = _fmt(v).split("\n")
            p = c.paragraphs[0]
            for i, line in enumerate(lines):
                r = _font(p.add_run(line), size)
                if i < len(lines) - 1:
                    r.add_break()
    if widths:
        _widths(t, widths)
    return t


def _widths(t, widths: list) -> None:
    """Fixed column widths (cell widths alone are ignored by some readers)."""
    t.autofit = False
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    t._element.tblPr.append(layout)
    for col, w in zip(t.columns, widths):
        col.width = Cm(w)
    for r in t.rows:
        for c, w in zip(r.cells, widths):
            c.width = Cm(w)


def _kv_table(doc, rows: list, widths=(5.0, 12.0), size: float = 9.5):
    """Two columns, no header row: the label column shaded."""
    t = doc.add_table(rows=0, cols=2)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    _borders(t)
    for k, v in rows:
        a, b = t.add_row().cells
        a.text = b.text = ""
        _font(a.paragraphs[0].add_run(str(k)), size, True)
        _shade(a, HEADER_FILL)
        _font(b.paragraphs[0].add_run(_fmt(v)), size)
    _widths(t, list(widths))
    return t


def _para(doc, text: str = "", size: Optional[float] = None, bold: bool = False, colour=None, style=None,
          align=None):
    p = doc.add_paragraph(style=style)
    if text:
        _font(p.add_run(text), size, bold, colour)
    if align is not None:
        p.alignment = align
    return p


def _trimmed(path: Path):
    """The image with its white margins cut away (a small border kept), as a PNG stream."""
    import io
    from PIL import Image, ImageChops
    im = Image.open(path).convert("RGB")
    # the plot's own title (top-left, for the image on its own) is left out: the report captions it
    im = im.crop((0, int(0.04 * im.height), im.width, im.height))
    box = ImageChops.difference(im, Image.new("RGB", im.size, (255, 255, 255))).getbbox()
    if box:
        pad = 12
        im = im.crop((max(box[0] - pad, 0), max(box[1] - pad, 0), min(box[2] + pad, im.width),
                      min(box[3] + pad, im.height)))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    return buf


def _picture(doc, path: Path, caption: str, width_cm: float = 15.0) -> None:
    if not path.exists():
        return
    doc.add_picture(_trimmed(path), width=Cm(width_cm))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    _para(doc, caption, 8.5, colour=GREY, align=WD_ALIGN_PARAGRAPH.CENTER)


def _field(paragraph, instruction: str) -> None:
    """A Word field (PAGE, NUMPAGES) in a paragraph."""
    run = _font(paragraph.add_run(), 8)
    for kind, text in (("begin", None), (None, instruction), ("separate", None), (None, "1"), ("end", None)):
        if kind:
            e = OxmlElement("w:fldChar")
            e.set(qn("w:fldCharType"), kind)
        elif text == instruction:
            e = OxmlElement("w:instrText")
            e.set(qn("xml:space"), "preserve")
            e.text = f" {instruction} "
        else:
            e = OxmlElement("w:t")
            e.text = text
        run._element.append(e)


def _band(part, left: str, right: str, page_number: bool = False) -> None:
    """A header or footer line: text on the left, text (and the page number) on the right,
    as a borderless two-cell table so it lines up whatever the template's tab stops."""
    part.paragraphs[0].text = ""
    t = part.add_table(rows=1, cols=2, width=Cm(17.0))
    a, b = t.rows[0].cells
    _font(a.paragraphs[0].add_run(left), 8, colour=GREY)
    pb = b.paragraphs[0]
    pb.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _font(pb.add_run(right), 8, colour=GREY)
    if page_number:
        _font(pb.add_run("Page "), 8, colour=GREY)
        _field(pb, "PAGE")
        _font(pb.add_run(" of "), 8, colour=GREY)
        _field(pb, "NUMPAGES")
    _widths(t, [11.0, 6.0])


def _header_footer(doc, left: str, right: str, footer: str) -> None:
    """Every page but the title page: model and study on the left, the date on the right;
    the producing tools and the page number in the footer."""
    for s in doc.sections:
        s.different_first_page_header_footer = True
        _band(s.header, left, right)
        _band(s.footer, footer, "", page_number=True)


def _template(study: Path) -> Optional[Path]:
    local = study / "report_template.docx"
    if local.exists():
        return local
    t = load_config().raw.get("report", {}).get("template")
    return Path(t) if t and Path(t).exists() else None


def _geo_volume(study: Path, part: str) -> Optional[float]:
    """A part's volume from the resolved geometry, for a part the model gives no body description (D53)."""
    try:
        return read_json(study / "geometry.json")["parts"][part]["volume"]
    except (OSError, KeyError, ValueError):
        return None


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12] if path.exists() else "-"


def _vec(v) -> str:
    return "(" + ", ".join(f"{x:.3g}" for x in v) + ")" if v else "-"


# --------------------------------------------------------------------------- the report

SECTIONS = ["Description", "Summary", "Results", "Conclusion", "Assumptions"]
APPENDICES = ["Model information", "Study properties", "Units", "Material properties", "Loads and fixtures",
              "Connections", "Mesh information", "Resultant forces", "Detailed results", "Input files and log"]


def _plan_text(plan: Optional[dict]) -> str:
    """How the run planner (D60) set this run up, for the study properties table."""
    if not plan:
        return "-"
    m = plan.get("machine") or {}
    txt = (f"Optimised for {str(plan.get('favour', 'balanced')).capitalize()}, on a computer with {m.get('cores', '?')} cores and "
           f"{m.get('ram_available_gb', '?')} GB free of {m.get('ram_total_gb', '?')} GB")
    if plan.get("overrides"):
        txt += "; set by hand: " + ", ".join(plan["overrides"])
    return txt


def _reuse_text(study: Path) -> str:
    """What the job that produced this report reused from earlier runs (stage caching)."""
    try:
        doc = json.loads((study / "results" / "reuse.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "-"
    reuse = doc.get("reuse") or {}
    if not reuse:
        return "Nothing: every stage ran"
    return f"Job {doc.get('job', '?')}: " + "; ".join(f"{stage} {text}" for stage, text in reuse.items())


def build_report(study: Path, pdf: bool = False) -> Path:
    study = Path(study)
    res = read_json(study / "results" / "results.json")
    model = load_model(study)
    conns_doc = read_json(study / "connections.json")
    conns = conns_doc["connections"]
    removed = conns_doc.get("removed_parts", [])
    tpl = _template(study)
    doc = Document(str(tpl)) if tpl else Document()
    if not tpl:
        _style_document(doc)
    asm_name = Path(model.source.assembly).stem
    study_name = model.study.name
    when = datetime.now()
    analyses = ["Static"] + (["Linear buckling"] if "buckling" in (model.study.analyses or []) and res.get("buckling") else [])
    versions = res["versions"]
    statuses = [c["status"] for c in res["checks"]]
    overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
    # the main body is numbered; the data sections, read on demand, are lettered and go in a data report of their own
    # (the user's request, 2026-10-17: the report was longer than anyone reads)
    num = {name: f"{i + 1}." for i, name in enumerate(SECTIONS)}
    num.update({name: f"{chr(65 + i)}." for i, name in enumerate(APPENDICES)})

    def heading(name: str):
        doc.add_heading(f"{num[name]} {name}", 1)

    # ---- title page
    # ---- title and contents on one page
    _para(doc)
    _para(doc, f"Simulation of {asm_name}", 26, True, BLUE)
    _para(doc, f"Study: {study_name}", 14, colour=GREY)
    _kv_table(doc, [["Date", f"{when:%d %B %Y}"], ["Analysis type", " and ".join(analyses)],
                    ["Load cases", ", ".join(res["load_cases"])],
                    ["Part" if model.source.assembly.lower().endswith(".sldprt") else "Assembly", model.source.assembly],
                    ["Configuration", model.source.configuration or "-"],
                    ["Overall result", overall]]
                   # fatigue is reported on its own (D107, D125): nothing of it here
                   + ([["Accuracy", res["accuracy"]["grade"]]] if res.get("accuracy") else []), (4.5, 12.5), size=10)
    _para(doc, "Prepared with AsmFEA, which builds the finite element model from the SolidWorks assembly, and "
               "CalculiX, which solves it.", 9, colour=GREY)
    _para(doc)
    _para(doc, "Contents", 15, True, BLUE)
    for name in SECTIONS:
        _para(doc, f"{num[name]}  {name}", 10.5).paragraph_format.space_after = Pt(2)
    _para(doc, f"The data behind the results is in {study_name}_data.docx.", 9.5, colour=GREY)
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    conv = res.get("convergence") or {}
    # ---- 1. Description
    heading("Description")
    _para(doc, f"Linear {' and '.join(a.lower() for a in analyses)} analysis of the assembly {asm_name} "
               f"(study {study_name}), with {len(res["load_cases"])} load case(s).")
    _para(doc, "AsmFEA does not calculate stress itself. It reads the geometry, materials, connections, loads and "
               "fixtures from SolidWorks, builds the finite element model, and passes it to CalculiX, which solves it. "
               "AsmFEA then reads the results back and reports them.")

    # ---- 2. Summary
    heading("Summary")
    p = _para(doc, "Overall result: ")
    _font(p.add_run(overall), None, True, STATUS_COLOUR[overall])
    acc = res.get("accuracy")
    if acc:
        # how far to trust these numbers, from how the model was solved (D60; wording in asmfea.accuracy)
        _para(doc, acc["summary"], bold=True)
        for r in acc["reasons"]:
            _para(doc, r, style="List Bullet")
    t = _table(doc, ["Check", "Value", "Limit", "Status"],
               [[c["check"], c["value"], c["limit"], c["status"]] for c in res["checks"]], [9, 3, 3, 2])
    for row, c in zip(t.rows[1:], res["checks"]):
        for run in row.cells[3].paragraphs[0].runs:
            run.font.color.rgb = STATUS_COLOUR[c["status"]]
            run.bold = True
    _para(doc)
    rows = []
    for case, lc in res["load_cases"].items():
        fos = [(p_["min_fos"], name) for name, p_ in lc["parts"].items() if p_["min_fos"]]
        fmin = min(fos) if fos else (None, "-")
        fult = min((p_["min_fos_ultimate"] for p_ in lc["parts"].values() if p_.get("min_fos_ultimate")), default=None)
        vmax = max(((v, name) for name, p_ in lc["parts"].items() if (v := assessed_stress(p_)) is not None),
                   default=(None, "-"))
        rows.append([case, lc["max_disp"], vmax[0], vmax[1], fmin[0], fult, fmin[1]])
    _table(doc, ["Load case", "Max displacement (mm)", "Max von Mises, membrane + bending (MPa)", "In part",
                 "Min factor of safety (yield)", "Min factor of safety (ultimate)", "In part"], rows)
    recs = A.weld_checks(res["load_cases"])
    if recs:
        _para(doc, "Recommended checks", 11, True, BLUE)
        for line in recs:
            _para(doc, line, style="List Bullet")

    # ---- main body: what matters first
    # ---- 3. Results
    heading("Results")
    for case, lc in res["load_cases"].items():
        doc.add_heading(f"Load case {case}", 2)
        ext = lc.get("extremes")
        safe = case.replace(" ", "_")
        if ext:
            def cell(e, unit, cap=None):
                v = e["value"]
                if v is None or e.get("at") is None:
                    return "not assessed (joint edges only)"
                val = f"above {cap:g}" if cap is not None and v > cap else _fmt(v)
                at = ", ".join(f"{c:.1f}" for c in e["at"])
                return f"{val} {unit}\nNode {e['node']} ({e['part']})\nat ({at})"
            _table(doc, ["Name", "Type", "Min", "Max"], [
                ["Stress", "von Mises stress", cell(ext["von_mises"]["min"], "MPa"), cell(ext["von_mises"]["max"], "MPa")],
                ["Displacement", "Resultant displacement", cell(ext["displacement"]["min"], "mm"),
                 cell(ext["displacement"]["max"], "mm")],
                ["Factor of safety", "Yield / von Mises (per part's material)", cell(ext["fos"]["min"], ""),
                 cell(ext["fos"]["max"], "", defaults.PLOT_FOS_CAP)]], [3, 4, 5, 5])
            _para(doc, "The stress maximum and factor of safety minimum are the membrane plus bending values the "
                       "checks use. On the plots, local peaks beyond them (notches, the stress next to a joint's edge) "
                       "are magenta"
                       + (". Along the joint edges themselves, where the computed values are artefacts of how the "
                          "joints are modelled, the plots show the stress just beside them." if lc.get("joint_zone_points")
                          else ".")
                       + (" Parts not assessed (wholly within joints, such as bushes: a bearing check by hand) are grey."
                          if lc.get("unassessed_points") else ""), 9)
        for field, label in (("vonmises", "Stress: von Mises"), ("displacement", "Displacement: resultant (deformed shape)"),
                             ("fos", "Factor of safety on yield")):
            _picture(doc, study / "results" / f"{safe}_{field}_iso.png", f"{asm_name} - {study_name} - {case} - {label}")
    if res.get("buckling"):
        doc.add_heading("Linear buckling", 2)
        _para(doc, "The buckling factor multiplies the loads of the buckling case; loads marked as preload (self-weight "
                   "by default) are applied but not scaled. Factors are upper bounds.")
        _table(doc, ["Buckling case", "Load factors (lowest first)", "Scaled loads", "Preloads"],
               [[case, ", ".join(f"{f:.4g}" for f in bk["factors"]), ", ".join(bk["scaled_loads"]),
                 ", ".join(bk["preloads"]) or "-"] for case, bk in res["buckling"].items()])
        for case, bk in res["buckling"].items():
            for i in range(1, defaults.PLOT_BUCKLING_MODES + 1):
                _picture(doc, study / "results" / f"{case.replace(' ', '_')}_buckling_mode{i}_iso.png",
                         f"{asm_name} - {study_name} - {case} - buckling mode {i}", 12)
    _para(doc, "The data behind these results (model, materials, loads, connections, mesh, reactions and the "
               f"results part by part) is in {study_name}_data.docx.", 9, colour=GREY)
    # ---- 4. Conclusion
    heading("Conclusion")
    p = _para(doc, "Overall result: ")
    _font(p.add_run(overall), None, True, STATUS_COLOUR[overall])
    failed = [c for c in res["checks"] if c["status"] not in ("PASS", "INFO")]
    if failed:
        for c in failed:
            _para(doc, f"{c['status']}: {c['check']} = {_fmt(c['value'])} (limit {_fmt(c['limit'])})", style="List Bullet")
    else:
        _para(doc, "Every check passed against the study's limits.")
    # warnings only: notes (severity info) record what AsmFEA handled itself and ask nothing of the reader (D132);
    # they stay in results.json and the run log
    warnings = [w for w in res.get("warnings", []) if w.get("severity", "warning") != "info"]
    if warnings:
        _para(doc, "Warnings present when this report was produced:", bold=True)
        for w in warnings:
            _para(doc, f"{w['code']}: {w['what']}", style="List Bullet")

    # ---- 5. Assumptions
    heading("Assumptions")
    conv = res.get("convergence") or {}
    joints = [c for c in conns if c["type"] == "bolted"]
    exc_path = study / "exceptions.json"
    stabilised = sorted({p_ for e in (read_json(exc_path)["exceptions"] if exc_path.exists() else [])
                         if e["code"] == "CONTACT_ONLY_SUPPORT" for p_ in e["where"]["parts"]})
    exc_codes = read_json(exc_path)["exceptions"] if exc_path.exists() else []
    disconnected = {p_ for e in exc_codes if e["code"] == "PART_EXCLUDED_DISCONNECTED" for p_ in e["where"]["parts"]}
    small = conns_doc.get("small_parts", [])
    small_ids = {s["part"] for s in small}
    user_out = set(model.study.excluded_parts or [])
    reasons = {p_: "disconnected" if p_ in disconnected else "user" if p_ in user_out else "small" if p_ in small_ids
               else "weld bead" if model.part(p_) and model.part(p_).kind == "weld_bead" else "fastener" for p_ in removed}
    man = next((p_ for p_ in (study / "mesh" / "manifest.json", study / "mesh" / "manifest_fast.json") if p_.exists()), None)
    n_holes = sum((m.get("base") or {}).get("holes", 0) for m in read_json(man)["parts"].values()) if man else 0
    sentences = (A.ALWAYS + A.mesh_levels(bool(conv)) + A.holes(n_holes) + A.ties(sum(c["type"] == "tie" for c in conns))
                 + A.removed(reasons) + A.rigid(conns_doc.get("rigid_groups", [])) + A.bolted(joints)
                 + A.bond_first(conns_doc.get("method", "classic"), conns_doc.get("method_why"),
                                len(conns_doc.get("bonded_redundant", [])) + len(conns_doc.get("bonded_on_pins", [])),
                                small, defaults.SMALL_PART_VOLUME_FRACTION)
                 + A.contact([c["id"] for c in conns if c["type"] == "contact"], stabilised,
                             bool(res.get("buckling")) and any(c["type"] == "contact" for c in conns))
                 + A.joints(sum(c["type"] in ("revolute", "hinge") for c in conns), sum(c["type"] == "sliding" for c in conns))
                 + A.slides(sum(c["type"] == "slide" for c in conns))
                 + A.couplings(sum(c["type"] in ("gear", "rack_pinion") for c in conns),
                               sum(c["type"] == "screw" for c in conns))
                 + A.buckling("buckling" in (model.study.analyses or []))
                 + A.welds(sorted({c["evidence"]["weld"] for c in conns if c.get("source") == "weld" and c.get("region")}),
                           sorted({w for c in conns if c.get("source") == "weld" and not c.get("region")
                                   for w in c.get("evidence", {}).get("welds", [])}))
                 + A.subassemblies(sorted({p.subassembly for p in model.parts if p.subassembly}))
                 + A.remote([ld.id for ld in model.loads if ld.type == "remote_force"])
                 + A.hydrostatic([ld.id for ld in model.loads if ld.type == "hydrostatic"])
                 + A.joint_band(any(c["type"] == "tie" for c in conns))
                 + A.weldment(sum(1 for c in conns if c.get("source") == "weldment" and c.get("butt")),
                              sum(1 for c in conns if c.get("source") == "weldment" and c.get("geometry") == "line"),
                              model.study.bond_tolerance)
                 + A.overrides(sum(c.get("source") == "override" for c in conns))
                 + A.superposition(bool(res.get("superposed")))
                 + A.hybrid(bool(res.get("hybrid"))))
    for s in sentences:
        _para(doc, s, style="List Bullet")

    # the main report ends here
    if not tpl:
        _header_footer(doc, f"Simulation of {asm_name}  |  {study_name}", f"{when:%d %B %Y}",
                       f"AsmFEA {versions['asmfea']}  |  {versions.get('solver') or 'CalculiX'}")
    out = study / "report" / f"{study_name}_report.docx"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    if pdf:
        to_pdf(out)

    # ---- the data report: the data behind it, for the technical file (sections A-J)
    doc = Document(str(tpl)) if tpl else Document()
    if not tpl:
        _style_document(doc)
    _para(doc, f"Simulation data: {asm_name}", 20, True, BLUE)
    _para(doc, f"Study: {study_name}  |  {when:%d %B %Y}  |  the data behind {study_name}_report.docx", 10, colour=GREY)
    for name in APPENDICES:
        _para(doc, f"{num[name]}  {name}", 10).paragraph_format.space_after = Pt(1)
    # ---- A. Model information
    heading("Model information")
    _picture(doc, study / "results" / "setup_iso.png", f"{asm_name} - {study_name}: model, fixtures and loads")
    inst = {i["part"]: i for i in res["mesh"]["instances"]}
    rows = []
    for p_ in model.parts:
        mat = model.material(p_.material)
        treat = KIND_TEXT.get(p_.kind, p_.kind)
        if p_.kind == "sheet" and p_.sheet:
            treat += f", {p_.sheet.thickness:.3g} mm" + (" flat" if p_.sheet.flat else " bent")
        if p_.kind == "member" and p_.member:
            prof = p_.member.profile
            treat += f", {prof.get('type', '')} " + " x ".join(_fmt(prof[k]) for k in ("b", "h", "d", "t") if k in prof)
        if p_.id in {x["part"] for x in conns_doc.get("small_parts", [])}:
            treat += " (left out: small part, see Assumptions)"
        elif p_.id in (model.study.excluded_parts or []):
            treat += " (left out by the user)"
        elif p_.id in removed:
            treat += " (removed: see Connections)"
        elif p_.id in set(conns_doc.get("rigid_parts", [])):
            treat = "Rigid (not meshed)"
        vol = p_.body.volume if p_.body is not None else _geo_volume(study, p_.id)
        rows.append([p_.id, treat, p_.material or "-", vol, vol * mat.density * 1000 if mat and vol else None,
                     inst.get(p_.id, {}).get("n_elements", "-")])
    _table(doc, ["Part", "Treated as", "Material", "Volume (mm³)", "Mass (kg)", "Elements"], rows,
           [4.5, 4.2, 2.8, 2.1, 1.6, 1.8])

    # ---- B. Study properties
    heading("Study properties")
    contacts = [c for c in conns if c["type"] == "contact"]
    _kv_table(doc, [
        ["Study name", study_name], ["Analysis type", " and ".join(analyses)],
        ["Load cases", ", ".join(res["load_cases"])],
        ["Mesh type", "Solid mesh: quadratic wedges (swept thin walls and member spans) and quadratic tetrahedra"],
        ["Run planned", _plan_text(res.get("plan"))],
        ["Mesh level", (res.get("plan") or {}).get("fidelity") or model.study.mesh.fidelity or "standard"],
        ["Convergence check", ("On: base and refined meshes solved, then the parts that had not converged solved "
                               "again on a finer mesh" if res.get("local_pass") else
                               "On: base and refined meshes solved") if conv else "Off: one mesh"],
        ["Solver", "AsmFEA hybrid solver (parts condensed onto their joints; the model was too large for CalculiX "
                   "on this computer)" if res.get("hybrid") else versions.get("solver") or "CalculiX"],
        ["Large displacement", "Off (linear geometry)"],
        ["Contact", f"{len(contacts)} separating contact pair(s)" if contacts else "None: touching faces bonded"],
        ["Results folder", str(study)],
        ["Model", f"{model.source.assembly}" + (f", configuration {model.source.configuration}" if model.source.configuration else "")],
        ["SolidWorks", model.source.sw_version or "-"],
        ["AsmFEA", f"{versions['asmfea']} (add-in {model.source.addin_version or '-'})"],
        ["Meshers", ", ".join(versions.get("meshers", [])) or "-"],
        ["Job", res["job"]], ["Reused from earlier runs", _reuse_text(study)]])

    # ---- C. Units
    heading("Units")
    _kv_table(doc, [["Length, displacement", "mm"], ["Force", "N"], ["Stress, pressure, elastic modulus", "MPa (N/mm²)"],
                    ["Mass", "kg (t in the result files)"], ["Density", "t/mm³"], ["Acceleration", "mm/s²"]])

    # ---- D. Material properties
    heading("Material properties")
    rows = []
    for m in model.materials:
        users = [p_.id for p_ in model.parts if p_.material == m.name]
        props = (f"Model type: linear elastic isotropic\nFailure criterion: von Mises stress against yield\n"
                 f"Yield strength: {_fmt(m.yield_)} MPa\n"
                 + (f"Tensile strength: {_fmt(m.ultimate)} MPa\n" if m.ultimate else "")
                 + f"Elastic modulus: {_fmt(m.E)} MPa\nPoisson's ratio: {_fmt(m.nu)}\n"
                 f"Mass density: {m.density * 1e12:.4g} kg/m³")
        rows.append([m.name, props, "\n".join(users) or "-"])
    _table(doc, ["Material", "Properties", "Components"], rows, [3.5, 8, 5.5])

    # ---- E. Loads and fixtures
    heading("Loads and fixtures")
    face_part = {f.id: f.part for f in model.face_refs}

    def entities(faces):
        parts = sorted({face_part.get(f, "?") for f in faces})
        return f"{len(faces)} face(s) on {', '.join(parts)}"
    fx_type = {"fixed": "Fixed geometry", "pinned": "Pinned (rigid face, rotation free)", "displacement": "Prescribed displacement"}
    _table(doc, ["Fixture", "Details"],
           [[fx.id, f"Type: {fx_type.get(fx.type, fx.type)}\nEntities: {entities(fx.faces)}"
             + (f"\nDirections held: {fx.dofs}" if fx.type != "fixed" and fx.dofs else "")
             + (f"\nRotations held about: {fx.hold_rotations}" if fx.hold_rotations else "")] for fx in model.fixtures],
           [4, 13])
    _para(doc)
    ld_type = {"force": "Force", "pressure": "Pressure", "gravity": "Gravity", "remote_force": "Remote force",
               "hydrostatic": "Hydrostatic pressure"}
    units = {"force": "N (total)", "pressure": "MPa", "gravity": "mm/s²", "remote_force": "N"}
    def load_value(ld) -> str:
        """Components and resultant of a vector load; for a remote force also where it acts and its moment."""
        if ld.type == "hydrostatic":
            down = ld.direction if ld.direction is not None else list(defaults.HYDRO_DOWN)
            top = (f"{_vec(ld.point)} mm" if ld.point is not None else
                   f"{_vec(list(ld.offset or [0, 0, 0]))} mm from the centroid of its faces")
            return (f"Fluid density: {_fmt(ld.density if ld.density is not None else defaults.WATER_DENSITY)} kg/m³"
                    f"\nPressure on top of the fluid (absolute): "
                    f"{_fmt(ld.surface_pressure if ld.surface_pressure is not None else defaults.ATMOSPHERE_MPA)} MPa"
                    f"\nTop of the fluid: {top}\nDown: {_vec(list(down))}"
                    + ("" if ld.magnitude in (None, 1, 1.0) else f"\nLoad factor: {_fmt(ld.magnitude)}"))
        if ld.direction is None:
            return f"Value: {_fmt(ld.magnitude)} {units.get(ld.type, '')}"
        d = np.asarray(ld.direction, float)
        v = ld.magnitude * d / np.linalg.norm(d)
        unit = units.get(ld.type, "")
        text = f"X, Y, Z: {_vec(v.round(9).tolist())} {unit.split(' ')[0]}\nResultant: {_fmt(ld.magnitude)} {unit}"
        if ld.type == "remote_force":
            if ld.point is not None:
                text += f"\nActs at: {_vec(ld.point)} mm"
            else:
                off = np.asarray(ld.offset or [0, 0, 0], float)
                text += (f"\nActs at: {_vec(off.tolist())} mm from the centroid of its faces"
                         f"\nMoment about that centroid: {_vec(np.cross(off, v).round(6).tolist())} N·mm")
        return text

    _table(doc, ["Load", "Details"],
           [[ld.id, f"Type: {ld_type.get(ld.type, ld.type)}\nLoad case: {ld.load_case}\n" + load_value(ld)
             + (f"\nEntities: {entities(ld.faces)}" if ld.faces else "\nEntities: whole model")] for ld in model.loads],
           [4, 13])

    # ---- F. Connections
    heading("Connections")
    _para(doc, "Connections are found from touching faces and classified from mates and part kinds. Rows marked * "
               "were decided by the user.")
    rows = []
    for c in conns:
        det = []
        if c.get("area") is not None:
            det.append(f"Area {_fmt(c['area'])} mm²")
        if c.get("gap") is not None:
            det.append(f"gap {_fmt(c['gap'])} mm")
        if c.get("friction"):
            det.append(f"friction {_fmt(c['friction'])}")
        if c.get("preload"):
            det.append(f"preload {_fmt(c['preload'])} N")
        rule = c.get("evidence", {}).get("rule", c.get("mode", ""))
        if rule:
            det.append(str(rule))
        kind = CONN_TEXT.get(c["type"], c["type"]) + (", rotation locked" if c.get("locked") else "")
        rows.append([("* " if c.get("source") == "override" else "") + c["id"], kind,
                     "\n".join(c["parts"][:2]), ", ".join(det)])
    _table(doc, ["Connection", "Type", "Components", "Details"], rows, [3.2, 2.6, 4.5, 6.7])

    # ---- G. Mesh information
    heading("Mesh information")
    m = res["mesh"]
    _table(doc, ["Mesh", "Total nodes", "Degrees of freedom", "Total elements", "Mass (kg)"],
           [[lv, m[lv]["nodes"], m[lv]["dof"], m[lv].get("elements"), m[lv]["total_mass_t"] * 1000]
            for lv in ("base", "refined", "local") if lv in m])
    if m.get("fallback_parts"):
        _para(doc, "Parts meshed by a fallback mesher: " + ", ".join(m["fallback_parts"]))
    sel = res.get("refine_selection")
    if conv and sel:
        # a big model refined only where most stressed (D73)
        _para(doc, f"The model is large, so the refined solve meshed finer only the {len(sel['parts'])} of {sel['of']} "
                   f"parts whose assessed stress was at least {sel['fraction']:.0%} of the highest "
                   f"({_fmt(sel['highest_MPa'])} MPa) in the base solve; the others kept their base mesh, and the "
                   "convergence check measures the refined parts and the deflection.", 9)
    lp = res.get("local_pass")
    if conv and lp:
        # the local pass (D65): what was refined, the changes, and the extrapolated stress where the three agree
        short = lambda pid: pid.split("/body:")[-1].split("/")[-1]  # noqa: E731
        _para(doc, f"Largest change between the base and refined meshes: {lp['first_worst_change']:.1%}. The parts "
                   f"that changed by more than {defaults.CONVERGENCE_TOL:.0%} ({', '.join(short(p) for p in lp['parts'])}) "
                   f"were meshed {1 / defaults.LOCAL_REFINE_RATIO:.3g} times finer again and the model solved a third "
                   f"time (the \"local\" mesh, reported). Largest change between the refined and local meshes: "
                   f"{conv.get('worst_change', 0):.1%} (assessed stress and maximum displacement, per part).")
        rows = []
        for case, parts in (lp.get("extrapolated") or {}).items():
            for part, e in parts.items():
                rows.append([case, short(part)] + [e["stress"][k] for k in range(3)]
                            + ([e["extrapolated"], f"±{e['gci']:.1%}"] if "extrapolated" in e else ["-", "not steady"]))
        if rows:
            _table(doc, ["Load case", "Part", "Base (MPa)", "Refined (MPa)", "Local (MPa)", "Extrapolated (MPa)",
                         "Band"], rows, size=9)
            _para(doc, "Extrapolated: the assessed stress the mesh is converging to (Richardson extrapolation over the "
                       "three meshes), with the band it is expected within (grid convergence index, safety factor "
                       "1.25). \"Not steady\": the three values do not close in steadily, so no extrapolation is "
                       "given; judge by the local mesh's value and the change shown.", 9)
    elif conv:
        _para(doc, f"Largest change between the base and refined meshes: {conv.get('worst_change', 0):.1%} (assessed "
                   "stress, membrane plus bending, and maximum displacement, per part).")
    else:
        _para(doc, "The convergence re-solve was switched off for this study; results come from a single mesh and their "
                   "mesh sensitivity is not measured.")

    # ---- H. Resultant forces
    heading("Resultant forces")
    _para(doc, "Reaction forces at all fixtures, per load case (N).")
    rows = []
    for case, lc in res["load_cases"].items():
        r = lc.get("support_reaction") or lc.get("reaction")     # older results have only the raw RF
        if r:
            total = sum(x * x for x in r) ** 0.5
            clean = [0.0 if abs(x) < 1e-9 * max(total, 1e-30) else x for x in r]      # round-off shown as 0
            rows.append([case, "All fixtures", clean[0], clean[1], clean[2], total])
    _table(doc, ["Load case", "Selection set", "Sum X", "Sum Y", "Sum Z", "Resultant"], rows)
    _para(doc, "Reactions balance the applied load to within: "
               + "; ".join(f"{case} {_fmt(lc['reaction_balance_err'])}" for case, lc in res["load_cases"].items()) + ".")

    # ---- I. Detailed results
    heading("Detailed results")
    for case, lc in res["load_cases"].items():
        doc.add_heading(f"Load case {case}", 2)
        conv_case = conv.get("by_case", {}).get(case, {}).get("parts", {})
        _para(doc, "Results per part. The factor of safety uses the membrane plus bending stress (ASME VIII Div. 2 "
                   "Part 5 classification: the stress linearised along a line through the material at the most stressed "
                   "points, round the joints as well as away from them), which leaves local peaks to fatigue "
                   "assessment. Peaks are shown for reference, leaving out the joint edges, where they are artefacts of "
                   "how the joints are modelled; bonded joints are also assessed below, and a part lying wholly in "
                   "joint bands (a bush) is marked \"at joints only\".", 9)
        _table(doc, ["Part", "Membrane + bending (MPa)", "Peak excl. joint edges (MPa)", "Min FoS yield",
                     "Min FoS ultimate", "Max disp. (mm)", "Mesh change"],
               [[name, (f"{p_['membrane_bending']:.1f} (rose with the mesh: not used)" if p_.get("singular_fallback")
                        else p_.get("membrane_bending")),
                 p_["peak_vm_excl_ties"] if p_["peak_vm_excl_ties"] is not None else "at joints only",
                 p_["min_fos"], p_.get("min_fos_ultimate"), p_["max_disp"],
                 f"{max(conv_case.get(name, {}).get('max_disp', 0), conv_case.get(name, {}).get('stress', conv_case.get(name, {}).get('peak_vm_excl_ties', 0))):.1%}"
                 if conv_case else "-"] for name, p_ in lc["parts"].items()])
        if lc.get("rigid_parts"):
            _para(doc, "Rigid parts (not meshed, no stress: D96). Largest displacement of each, from its rigid motion.", 9)
            _table(doc, ["Rigid part", "Max disp. (mm)", "At (x, y, z)"],
                   [[name, r["max_disp"], ", ".join(f"{c:.1f}" for c in r["max_disp_at"])]
                    for name, r in lc["rigid_parts"].items()])
        joints = sorted(lc.get("joints", []), key=lambda j: -j["structural_stress"])
        if joints:
            _para(doc, "Bonded joints: structural stress, from the forces each joint carries (the mesh-insensitive "
                       "method of Dong, Battelle). The peak stress right at a bonded edge is singular and not used; this "
                       "membrane plus bending stress of the member's section at the joint is. Highest first.", 9)
            short = lambda pid: pid.split("/body:")[-1].split("/")[-1]  # noqa: E731
            _table(doc, ["Joint (member -> part it meets)", "Structural stress (MPa)", "Membrane", "Bending", "Shear",
                         "Wall (mm)", "FoS"],
                   [[(f"{short(j['part'])} wall under {short(j['other'])}" if j.get("side") == "receiving"
                      else f"{short(j['part'])} -> {short(j['other'])}"), j["structural_stress"], j["membrane"], j["bending"],
                     j["shear"], j["thickness"], j.get("fos")] for j in joints[:defaults.REPORT_JOINT_ROWS]])
        if lc.get("contact"):
            ct = lc["contact"]
            _para(doc, f"Contact pairs {', '.join(ct['pairs'])}: largest opening {_fmt(ct.get('max_opening'))} mm, "
                       f"largest penetration {_fmt(ct.get('max_penetration'))} mm, open fraction {_fmt(ct.get('open_fraction'))}.")
        if lc.get("bolts"):
            _table(doc, ["Bolt", "Preload (N)", "Bolt force (N)", "Elongation (mm)"],
                   [[k, v["preload"], v.get("force"), v.get("elongation")] for k, v in lc["bolts"].items()])
        if lc.get("stabilising_spring_share") is not None:
            _para(doc, f"Stabilising springs carry {lc['stabilising_spring_share']:.2%} of the applied load.")

    # ---- J. Input files and log
    heading("Input files and log")
    rows = [["model.json", _sha1(study / "model.json")], ["study.json", _sha1(study / "study.json")]]
    rows += [[f"{p_.id} geometry", p_.geometry_hash[:12]] for p_ in model.parts]
    _table(doc, ["Input", "Hash"], rows)
    log = study / "run" / res["job"] / "log.txt"
    if log.exists():
        _para(doc, "Log excerpt:")
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        para = doc.add_paragraph()
        for i, line in enumerate(tail):
            r = _font(para.add_run(line), 7.5)
            if i < len(tail) - 1:
                r.add_break()

    if not tpl:
        _header_footer(doc, f"Simulation data of {asm_name}  |  {study_name}", f"{when:%d %B %Y}",
                       f"AsmFEA {versions['asmfea']}  |  {versions.get('solver') or 'CalculiX'}")
    data = study / "report" / f"{study_name}_data.docx"
    doc.save(str(data))
    if pdf:
        to_pdf(data)
    return out


def data_report_path(study: Path) -> Path:
    """The data report written beside the main report by build_report."""
    model = load_model(Path(study))
    return Path(study) / "report" / f"{model.study.name}_data.docx"


def build_summary(study: Path, pdf: bool = False) -> Path:
    """A half-page summary for risk assessments: project details, one von Mises plot (the
    most stressed load case) and a table of max stress, max displacement and min factor of
    safety per load case. The full report is the technical file's document."""
    study = Path(study)
    res = read_json(study / "results" / "results.json")
    model = load_model(study)
    tpl = _template(study)
    doc = Document(str(tpl)) if tpl else Document()
    if not tpl:
        _style_document(doc)
    asm_name = Path(model.source.assembly).stem
    study_name = model.study.name
    when = datetime.now()
    statuses = [c["status"] for c in res["checks"]]
    overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"

    # per load case: the figures the factor of safety is based on (membrane plus bending)
    rows, worst_case, worst_vm = [], None, -1.0
    for case, lc in res["load_cases"].items():
        parts = lc["parts"]
        vm_part = max(parts, key=lambda n: assessed_stress(parts[n]) or 0) if parts else None
        fos = [(p_["min_fos"], n) for n, p_ in parts.items() if p_["min_fos"]]
        fmin = min(fos) if fos else (None, "-")
        fult = min((p_["min_fos_ultimate"] for p_ in parts.values() if p_.get("min_fos_ultimate")), default=None)
        vm = assessed_stress(parts[vm_part]) if vm_part else None
        if vm is not None and vm > worst_vm:
            worst_vm, worst_case = vm, case
        # the values only: the summary's readers do not know the parts by name (the user's request, 2026-10-11)
        rows.append([case, _fmt(vm), _fmt(lc["max_disp"]), _fmt(fmin[0]), _fmt(fult) if fult else "-"])

    _para(doc, f"Structural analysis summary: {asm_name}", 15, True, BLUE)
    _para(doc, f"Study {study_name}  |  {when:%d %B %Y}  |  AsmFEA {res['versions']['asmfea']} with CalculiX", 8.5,
          colour=GREY)

    # details on the left, the stress plot on the right
    layout = doc.add_table(rows=1, cols=2)
    _widths(layout, [8.0, 9.0])
    left, right = layout.rows[0].cells
    left.text = ""
    mats = sorted({p_.material for p_ in model.parts if p_.material})
    details = [["Assembly", asm_name], ["Configuration", model.source.configuration or "-"], ["Study", study_name],
               ["Analysis", "Linear static" + (" and buckling" if res.get("buckling") else "")],
               ["Load cases", ", ".join(res["load_cases"])], ["Materials", ", ".join(mats) or "-"],
               ["Date", f"{when:%d %B %Y}"], ["Result", overall]]
    t = left.add_table(rows=0, cols=2)
    _borders(t)
    for k, v in details:
        a, b = t.add_row().cells
        a.text = b.text = ""
        _font(a.paragraphs[0].add_run(k), 8.5, True)
        _shade(a, HEADER_FILL)
        _font(b.paragraphs[0].add_run(str(v)), 8.5, k == "Result", STATUS_COLOUR.get(v) if k == "Result" else None)
    _widths(t, [2.6, 5.0])
    left.paragraphs[0].paragraph_format.space_after = Pt(0)
    img = study / "results" / f"{(worst_case or '').replace(' ', '_')}_vonmises_iso.png"
    pr = right.paragraphs[0]
    pr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if img.exists():
        pr.add_run().add_picture(_trimmed(img), width=Cm(8.6))
        cap = right.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _font(cap.add_run(f"von Mises stress, load case {worst_case} (MPa)"), 8, colour=GREY)

    _para(doc)
    _table(doc, ["Load case", "Max von Mises stress (MPa)", "Max displacement (mm)", "Min factor of safety (yield)",
                 "Min factor of safety (ultimate)"], rows, [3.0, 4.0, 3.5, 3.8, 3.2], size=9)
    if res.get("accuracy"):                             # one line: how far to trust the table (D60)
        _para(doc, res["accuracy"]["summary"], 8.5, bold=True)
    recs = A.weld_checks(res["load_cases"], names=False)
    if recs:                                            # the recommended joint checks, without part names
        _para(doc, "Recommended: " + " ".join(recs) + f" Where: see {study_name}_report.docx.", 8.5)
    lim = model.study.limits
    limits = []
    if lim.min_fos:
        limits.append(f"minimum factor of safety {lim.min_fos:g}")
    if lim.max_deflection:
        limits.append(f"maximum deflection {lim.max_deflection:g} mm")
    if lim.max_stress:
        limits.append(f"maximum stress {lim.max_stress:g} MPa")
    note = ("Linear elastic analysis; factor of safety = yield strength / von Mises stress, excluding the stress at "
            "tied part interfaces, where it is singular.")
    if limits:
        note += " Limits: " + ", ".join(limits) + "."
    note += f" Full details: {study_name}_report.docx (technical file)."
    _para(doc, note, 7.5, colour=GREY)

    out = study / "report" / f"{study_name}_summary.docx"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    if pdf:
        to_pdf(out)
    return out


LIBREOFFICE_DEFAULT = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console window pops up over the desktop


def to_pdf(docx: Path) -> Optional[Path]:
    """Convert on this machine: Microsoft Word through COM if it is installed, otherwise
    LibreOffice headless (separate executable, MPL-2.0; [report] libreoffice in asmfea.toml
    or the standard install path). Returns None when neither is available."""
    pdf = docx.with_suffix(".pdf")
    word = subprocess.run(["powershell", "-NoProfile", "-Command",
                           "Test-Path 'Registry::HKEY_CLASSES_ROOT\\Word.Application'"],
                          capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW).stdout.strip() == "True"
    if word:
        ps = (f"$w = New-Object -ComObject Word.Application; $w.Visible = $false; "
              f"$d = $w.Documents.Open('{docx}'); $d.SaveAs([ref] '{pdf}', [ref] 17); $d.Close(); $w.Quit()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
    else:
        lo = load_config().raw.get("report", {}).get("libreoffice")
        exe = Path(lo) if lo else LIBREOFFICE_DEFAULT
        if not exe.is_file():
            return None
        subprocess.run([str(exe), "--headless", "--convert-to", "pdf", "--outdir", str(docx.parent), str(docx)],
                       capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    return pdf if pdf.exists() else None
