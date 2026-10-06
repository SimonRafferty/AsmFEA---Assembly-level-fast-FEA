"""The verification report as one self-contained web page (images embedded), styled like the getting-started
guide, and its PDF copy printed by Microsoft Edge (headless). The Word version is report.py; the wording is
shared (text.py)."""
from __future__ import annotations

import base64
import datetime as dt
import html as H
import io
import json
import platform
import subprocess
from pathlib import Path
from typing import Optional

from . import text as T
from .report import NO_SWS_BUCKLING, SOURCES, _mesh_plot, _num, _pct, _sha256, _theory_text

EDGE = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]

CSS = """
:root { --ink:#1c2430; --muted:#5d6878; --line:#e2e7ee; --bg:#f5f7fa; --card:#fff; --blue:#1f6fd1;
        --green:#1b7f2a; --red:#c0392b; --shadow:0 1px 2px rgba(16,24,40,.06),0 6px 20px rgba(16,24,40,.07); }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.6 "Segoe UI",Arial,sans-serif;
       -webkit-print-color-adjust:exact; print-color-adjust:exact; }
a { color:var(--blue); word-break:break-all; }
header { background:linear-gradient(120deg,#13233d 0%,#1b3a66 55%,#1f6fd1 100%); color:#fff; padding:34px 0 38px; }
.wrap { max-width:1060px; margin:0 auto; padding:0 18px; }
.hero { display:flex; align-items:center; gap:24px; }
.hero img { width:84px; height:84px; flex:none; filter:drop-shadow(0 4px 10px rgba(0,0,0,.35)); }
.hero h1 { margin:0; font-size:31px; }
.hero p { margin:4px 0 0; color:#d6e3f7; font-size:16.5px; }
.chips { display:flex; flex-wrap:wrap; gap:8px; margin-top:18px; }
.chips span { background:rgba(255,255,255,.12); border:1px solid rgba(255,255,255,.25); border-radius:999px;
              padding:3px 13px; font-size:13px; }
.rainbow { height:6px; background:linear-gradient(90deg,#2b3fd8,#1f8fe0,#17b8c8,#3aa856,#b5d62e,#f2c418,#f28a1a,#d9412b); }
main { padding:22px 0 50px; }
.card { background:var(--card); border-radius:14px; box-shadow:var(--shadow); padding:24px 26px; margin:20px 0; }
.card.keep, figure, table.kv, .stats, .note, tr { break-inside:avoid; }
h2, h3 { break-after:avoid; }
h2 { margin:0 0 12px; font-size:22px; display:flex; align-items:center; gap:10px; }
h3 { margin:18px 0 8px; font-size:16px; }
.num { display:inline-flex; align-items:center; justify-content:center; min-width:34px; height:34px; padding:0 8px;
       border-radius:17px; color:#fff; font-weight:700; font-size:15px; background:var(--blue); }
.note { background:#eef5ff; border-left:4px solid var(--blue); border-radius:6px; padding:11px 15px; margin:14px 0; }
.note.strong { font-weight:600; }
.stats { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin:6px 0 18px; }
.stat { background:#f3f6fa; border-radius:12px; padding:14px 16px; }
.stat b { display:block; font-size:28px; color:#13233d; line-height:1.15; }
.stat span { color:var(--muted); font-size:13.5px; }
table { width:100%; border-collapse:collapse; font-size:13.5px; margin:8px 0; }
th { background:#dfe8f5; text-align:left; font-weight:600; padding:7px 9px; }
td { border-bottom:1px solid var(--line); padding:6px 9px; vertical-align:top; }
td.n { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
.badge { display:inline-block; border-radius:999px; padding:1px 10px; font-size:12px; font-weight:700; color:#fff; }
.pass { background:var(--green); } .fail { background:var(--red); }
.bar { position:relative; height:8px; background:#edf1f6; border-radius:4px; min-width:70px; margin-top:6px; }
.bar i { position:absolute; left:0; top:0; bottom:0; border-radius:4px; background:#3aa856; }
.bar.over i { background:var(--red); }
.kv td:first-child { width:30%; color:var(--muted); font-weight:600; }
.figs { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin:12px 0; }
figure { margin:0; }
figure img { width:100%; height:auto; display:block; border:1px solid var(--line); border-radius:8px; background:#fff; }
figcaption { color:var(--muted); font-size:12.5px; text-align:center; margin-top:5px; }
pre { background:#f3f6fa; border-radius:8px; padding:12px 14px; font:11.5px/1.45 Consolas,monospace; overflow-x:auto;
      white-space:pre; }
details summary { cursor:pointer; color:var(--blue); font-weight:600; margin:8px 0; }
ul { padding-left:20px; } li { margin:5px 0; }
.muted { color:var(--muted); font-size:13px; }
footer { color:var(--muted); font-size:12.5px; text-align:center; padding:6px 18px 36px; }
@media screen and (max-width:820px) { .stats, .figs { grid-template-columns:1fr; } .hero img { width:60px; height:60px; } }
@media print { body { background:#fff; font-size:12.5px; } .card { box-shadow:none; border:1px solid var(--line); }
               header { padding:22px 0 24px; } details > summary { color:var(--ink); } }
"""


def _img(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _png(path: Path, max_w: int = 1300, trim: bool = True) -> Optional[str]:
    """A picture as a data URI: white margins trimmed (ignoring a plot's title strip at the top left), scaled down
    to max_w, transparency kept."""
    if not path or not Path(path).exists():
        return None
    from PIL import Image, ImageChops
    im = Image.open(path).convert("RGBA")
    if trim:
        rgb = Image.new("RGB", im.size, (255, 255, 255))
        rgb.paste(im, mask=im.split()[3])
        probe = rgb.copy()
        probe.paste((255, 255, 255), (0, 0, int(im.width * 0.6), int(im.height * 0.05)))   # the title text
        box = ImageChops.difference(probe, Image.new("RGB", im.size, (255, 255, 255))).getbbox()
        if box:
            im = rgb.crop((max(box[0] - 14, 0), max(box[1] - 14, 0), min(box[2] + 14, im.width),
                           min(box[3] + 14, im.height)))
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return _img(buf.getvalue())


def _diagram_svg() -> str:
    boxes = [("SolidWorks", "assembly", False), ("AsmFEA add-in", "geometry, materials,|connections, loads", False),
             ("AsmFEA pipeline", "mesh + CalculiX|input file", False), ("CalculiX", "solves the|mechanics", True),
             ("AsmFEA", "reads results back:|checks, plots, report", False)]
    w, h, gap, x0 = 170, 96, 34, 6
    parts = [f'<svg viewBox="0 0 {x0 * 2 + 5 * w + 4 * gap} 128" width="100%" role="img" '
             'aria-label="SolidWorks to AsmFEA to CalculiX to results" font-family="Segoe UI, Arial">',
             '<defs><marker id="ar" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto">'
             '<path d="M0,0 L10,5 L0,10 z" fill="#1f4e79"/></marker></defs>']
    for i, (title, sub, hot) in enumerate(boxes):
        x = x0 + i * (w + gap)
        parts.append(f'<rect x="{x}" y="8" width="{w}" height="{h}" rx="14" fill="{"#ffe2b3" if hot else "#e8eef7"}" '
                     f'stroke="#1f4e79" stroke-width="1.6"/>')
        parts.append(f'<text x="{x + w / 2}" y="40" text-anchor="middle" font-size="15" font-weight="700" fill="#13233d">'
                     f'{H.escape(title)}</text>')
        for k, line in enumerate(sub.split("|")):
            parts.append(f'<text x="{x + w / 2}" y="{62 + k * 17}" text-anchor="middle" font-size="12.5" '
                         f'fill="#3b4656">{H.escape(line)}</text>')
        if i < len(boxes) - 1:
            parts.append(f'<line x1="{x + w + 3}" y1="{8 + h / 2}" x2="{x + w + gap - 5}" y2="{8 + h / 2}" '
                         'stroke="#1f4e79" stroke-width="2" marker-end="url(#ar)"/>')
    parts.append(f'<text x="{(x0 * 2 + 5 * w + 4 * gap) / 2}" y="124" text-anchor="middle" font-size="12.5" '
                 'font-style="italic" fill="#5d6878">The add-in prepares the model and reports the results; the stresses '
                 'are calculated by CalculiX.</text></svg>')
    return "".join(parts)


def _e(t) -> str:
    return H.escape(str(t))


def _bar(diff: float, tol: float) -> str:
    frac = min(abs(diff) / tol, 1.0) if tol else 0
    return f'<div class="bar{" over" if abs(diff) > tol else ""}" title="share of the {tol * 100:g} % criterion used">' \
           f'<i style="width:{max(frac * 100, 1.5):.1f}%"></i></div>'


def _rows_table(rows: list[dict], with_case: bool, with_where: bool = False) -> str:
    head = (["Case"] if with_case else []) + ["Quantity", "Reference", "AsmFEA", "Difference", "Result"] + \
        (["Read-out"] if with_where else [])
    out = ["<table><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr>"]
    for r in rows:
        cells = ([f"<td><b>{_e(r['case'])}</b></td>"] if with_case else [])
        cells += [f"<td>{_e(r['quantity'])} <span class='muted'>({_e(r['unit'])})</span></td>",
                  f"<td class='n'>{_num(r['reference'])}</td>", f"<td class='n'>{_num(r['asmfea'])}</td>",
                  f"<td class='n'>{_pct(r['difference'])}{_bar(r['difference'], r['tolerance'])}</td>",
                  f"<td><span class='badge {'pass' if r['pass'] else 'fail'}'>{'PASS' if r['pass'] else 'FAIL'}</span></td>"]
        if with_where:
            nodes = f" ({r['nodes']} nodes)" if r["nodes"] else ""
            cells.append(f"<td class='muted'>{_e(r['where'])}{nodes}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "".join(out) + "</table>"


def build(result: dict, root: Path, out: Path, versions: dict, icon: Optional[Path] = None) -> Path:
    root, out = Path(root), Path(out)
    work = out.parent / (out.stem + "_files")
    work.mkdir(parents=True, exist_ok=True)
    rows = result["rows"]
    when = dt.date.today()
    npass = sum(r["pass"] for r in rows)
    worst = max(rows, key=lambda r: abs(r["difference"])) if rows else None
    sv = result.get("solver") or {}
    ex = sv.get("examples", {})
    b = []
    a = b.append
    icon_uri = _png(icon, 200, trim=False) if icon else None
    a(f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, "
      f"initial-scale=1'><title>AsmFEA verification report</title><style>{CSS}</style></head><body>")
    a("<header><div class='wrap'><div class='hero'>" + (f"<img src='{icon_uri}' alt=''>" if icon_uri else "") +
      "<div><h1>AsmFEA verification report</h1><p>Classical-mechanics checks of the full chain, SolidWorks to report, "
      "and of the CalculiX solver</p></div></div><div class='chips'>"
      f"<span>{when:%d %B %Y}</span><span>AsmFEA {_e(versions['asmfea'])}</span>"
      f"<span>CalculiX {_e(versions['calculix'])}</span>"
      f"<span>{'Full-chain runs from SolidWorks' if result['source'] == 'sw' else 'Regression runs (no SolidWorks)'}</span>"
      "</div></div></header><div class='rainbow'></div><main class='wrap'>")

    # summary
    a("<section class='card'><h2><span class='num'>1</span>Summary</h2>")
    a(f"<div class='note strong'>{_e(T.USER_NOTE)}</div>")
    a("<div class='stats'>"
      f"<div class='stat'><b>{npass} / {len(rows)}</b><span>comparisons within their acceptance criteria</span></div>"
      f"<div class='stat'><b>{_pct(worst['difference']) if worst else '-'}</b><span>largest difference from the "
      f"reference ({_e(worst['case'] + ', ' + worst['quantity'].lower()) if worst else ''})</span></div>"
      f"<div class='stat'><b>{ex.get('matched', '-')} / {ex.get('run', '-')}</b><span>CalculiX test examples matching "
      "their reference results</span></div></div>")
    a(_rows_table(rows, True))
    mrows = result.get("method_rows") or []
    if mrows:
        a(f"<p><b>{sum(r['pass'] for r in mrows)} of {len(mrows)}</b> method checks for joints and notches (M1-M4, on "
          "hand-written models) pass; see section M.</p>")
    a("<p class='muted'>Difference = (AsmFEA − reference) / reference, sign kept; the bar shows how much of the "
      "acceptance criterion it uses. The reference is the classical solution, except the mass rows marked SolidWorks. "
      f"Criteria: deflection, stress and buckling load 5 %, reactions 1 %, mass 0.5 %.</p>")
    a(f"<p>{_e(T.PURPOSE)}</p><p class='muted'>{_e(T.NO_SWS)}</p></section>")

    # how it works
    a("<section class='card keep'><h2><span class='num'>2</span>How the analysis works</h2>"
      f"<div class='note strong'>{_e(T.NOT_STRESS)}</div>{_diagram_svg()}<p>{_e(T.TWO_LAYERS)}</p></section>")

    # solver
    a("<section class='card flow'><h2><span class='num'>3</span>The solver</h2>" f"<p>{_e(T.SOLVER_INTRO)}</p>")
    if sv:
        pa = ex.get("pardiso_pass", {})
        a("<table class='kv'>"
          f"<tr><td>Binary</td><td>{_e(sv.get('binary', '-'))} ({_e(sv.get('ccx_version', 'CalculiX 2.22'))})</td></tr>"
          f"<tr><td>MD5</td><td>{_e(sv.get('md5', '-'))}: the same file as in the author's calculix_2.22_4win.zip, which the "
          "AsmFEA installer downloads</td></tr>"
          f"<tr><td>Official test examples</td><td><b>{ex.get('matched')} of {ex.get('run')}</b> decks match their reference "
          "results (official compare scripts; a value fails beyond 0.1 % of the largest value in its output block)</td></tr>"
          f"<tr><td>With PARDISO, AsmFEA's solver</td><td><b>{pa.get('matched', '-')} of {pa.get('run', '-')}</b>; the "
          "exception tests the iterative Cholesky solver, whose reference values carry that solver's convergence error</td>"
          "</tr></table>")
        for key, name, cols in (("le1", "NAFEMS LE1, elliptic membrane", "own_decks_cps8"),
                                ("le10", "NAFEMS LE10, thick plate", "own_decks_c3d10")):
            le = sv.get(key, {})
            decks = le.get(cols) or []
            if not decks:
                continue
            t = le["target_MPa"]
            a(f"<h3>{name}: σyy at point D, target {t:g} MPa</h3><table><tr><th>Element</th><th>Mesh</th>"
              "<th>σyy at D (MPa)</th><th>vs target</th></tr>")
            for d in decks:
                mesh = f"{d['elements']:,} elements" + (f", {d['equations']:,} equations" if d.get("equations") else "")
                diff = (d["syy_D_MPa"] - t) / t
                a(f"<tr><td>{_e(d['element'].split()[0])}</td><td>{mesh}</td><td class='n'>{d['syy_D_MPa']:.4g}</td>"
                  f"<td class='n'>{_pct(diff)}</td></tr>")
            a("</table>")
        a("<p class='muted'>Targets: NAFEMS TNSB Rev. 3 (1990), quoted via bConverged (the NAFEMS document itself is not "
          "freely available). The official test set is thin on quadratic tetrahedra, wedges, gravity and buckling; the "
          "LE10 runs and the full-chain cases cover those.</p>")
    a("<h3>Sources</h3><table><tr><th>Source</th><th>What it shows</th><th>Link</th></tr>")
    for name, what, link in SOURCES:
        cell = f"<a href='{_e(link)}'>{_e(link)}</a>" if link else "-"
        a(f"<tr><td>{_e(name)}</td><td>{_e(what)}</td><td>{cell}</td></tr>")
    a("</table><p class='muted'>All links were checked on the date of this report.</p></section>")

    # method
    a("<section class='card flow'><h2><span class='num'>3b</span>" + _e(T.HYBRID_TITLE) + "</h2>"
      f"<p>{_e(T.HYBRID_INTRO)}</p><ul>" + "".join(f"<li>{_e(x)}</li>" for x in T.HYBRID_HOW) + "</ul>"
      "<table><tr><th>Model</th><th>Result</th></tr>" +
      "".join(f"<tr><td>{_e(m)}</td><td>{_e(r)}</td></tr>" for m, r in T.HYBRID_EVIDENCE) +
      f"</table><p class='muted'>{_e(T.HYBRID_NOTE)}</p></section>")
    a("<section class='card keep'><h2><span class='num'>4</span>Method</h2><ul>" +
      "".join(f"<li>{_e(t)}</li>" for t in T.METHOD) + "</ul><table><tr><th>Quantity</th><th>Criterion</th></tr>" +
      "".join(f"<tr><td>{_e(q)}</td><td>{_e(c)}</td></tr>" for q, c in T.CRITERIA) + "</table></section>")

    # cases
    for cr in result["cases"]:
        c, m = cr["case"], cr["measured"]
        res = Path(m["study"]) / "results"
        setup = _png(res / "setup_iso.png")
        plot = next((res / i for i in m["images"] if "buckling_mode1_iso" in i), None) or \
            next((res / i for i in m["images"] if i.endswith("_vonmises_iso.png")), None)
        plot_uri = _png(plot) if plot else None
        conv = f"largest change between base and refined meshes {_pct(m['convergence_worst'])}" + \
            (" (flagged: see the comment)" if "CONVERGENCE_POOR" in m["warnings"] else "")
        a(f"<section class='card flow'><h2><span class='num'>{_e(c['id'])}</span>{_e(c['title'])}</h2>"
          "<table class='kv'>"
          f"<tr><td>Exercises</td><td>{_e(c['exercises'])}</td></tr><tr><td>Model</td><td>{_e(c['description'])}</td></tr>"
          f"<tr><td>Restraints and loads</td><td>{_e(c['restraints'])}</td></tr>"
          f"<tr><td>Mesh</td><td>default fidelity (standard); reported mesh {m['level']}, {m['dof']:,} degrees of freedom</td></tr>"
          f"<tr><td>Convergence check</td><td>{conv}</td></tr></table>")
        figs = []
        if setup:
            figs.append(f"<figure><img src='{setup}' alt='model'><figcaption>Model as analysed: fixtures blue, loaded "
                        "faces red</figcaption></figure>")
        if plot_uri:
            figs.append(f"<figure><img src='{plot_uri}' alt='result'><figcaption>Result: "
                        f"{'buckling mode 1' if 'buckling' in plot.name else 'von Mises stress'}</figcaption></figure>")
        if figs:
            a("<div class='figs'>" + "".join(figs) + "</div>")
        a("<h3>Comparison</h3>" + _rows_table(cr["rows"], False, True))
        a(f"<div class='note'>{_e(T.CASE_NOTES.get(c['id'], ''))}</div>")
        a(f"<details open><summary>Classical calculation (written before the run)</summary><pre>"
          f"{_e(_theory_text(Path(c['dir'])))}</pre></details></section>")

    # methods for joints and notches, and their checks on hand-written models
    a("<section class='card flow'><h2><span class='num'>M</span>Methods for joints and notches</h2>"
      f"<p>{_e(T.METHODS_INTRO)}</p><table><tr><th>Method</th><th>What it does</th><th>Situations it is there for</th>"
      "<th>Evidence</th></tr>" +
      "".join(f"<tr><td><b>{_e(n)}</b></td><td>{_e(w)}</td><td>{_e(e)}</td><td>{_e(v)}</td></tr>"
              for n, w, e, v in T.JOINT_METHODS) + "</table>")
    if result.get("methods"):
        a("<h3>Method checks</h3><p>Hand-written models run through the pipeline (no SolidWorks), each against a hand "
          "answer. M4 checks the receiving-wall method, which is not used in normal runs (see Limitations).</p>")
        for mc in result["methods"]:
            a(f"<h3>{_e(mc['id'])}  {_e(mc['title'])}</h3>" + _rows_table(mc["rows"], False, True))
    a("</section>")

    # mesh sensitivity
    ms = sorted([x for x in result["mesh_sensitivity"] if x["case"] == "C1"],
                key=lambda x: {"coarse": 0, "standard": 1, "fine": 2}.get(x["fidelity"], 9))
    a("<section class='card'><h2><span class='num'>6</span>Mesh sensitivity (C1)</h2>")
    if ms:
        a("<table><tr><th>Fidelity</th><th>Degrees of freedom</th><th>Tip deflection (mm)</th><th>vs theory</th>"
          "<th>σ mid-length (MPa)</th><th>vs theory</th><th>Check: largest change</th><th>Flagged</th></tr>")
        for x in ms:
            r1 = next(r for r in x["rows"] if r["quantity"] == "Tip deflection")
            r2 = next(r for r in x["rows"] if r["quantity"].startswith("Bending stress"))
            a(f"<tr><td>{x['fidelity']}</td><td class='n'>{x['measured']['dof']:,}</td><td class='n'>{_num(r1['asmfea'])}</td>"
              f"<td class='n'>{_pct(r1['difference'])}</td><td class='n'>{_num(r2['asmfea'])}</td>"
              f"<td class='n'>{_pct(r2['difference'])}</td><td class='n'>{_pct(x['measured']['convergence_worst'])}</td>"
              f"<td>{'yes' if 'CONVERGENCE_POOR' in x['measured']['warnings'] else 'no'}</td></tr>")
        a("</table>")
        if len(ms) >= 2:
            a(f"<figure><img src='{_png(_mesh_plot(ms, work / 'mesh_c1.png'), 1300, trim=False)}' alt='mesh sensitivity'>"
              "<figcaption>Difference from theory against degrees of freedom; green band: the 5 % criterion</figcaption></figure>")
        a(f"<p>{_e(T.MESH_NOTE)}</p>")
    else:
        a("<p>Not run.</p>")
    a("</section>")

    # limitations, findings, appendix
    a("<section class='card keep'><h2><span class='num'>7</span>Limitations</h2><ul>" +
      "".join(f"<li>{_e(t)}</li>" for t in T.LIMITATIONS) + "</ul></section>")
    a("<section class='card keep'><h2><span class='num'>8</span>Findings during verification</h2><ul>" +
      "".join(f"<li>{_e(t)}</li>" for t in T.FINDINGS) + "</ul></section>")
    sw_version = "-"
    try:
        first = result["cases"][0]["measured"]
        v = json.loads((Path(first["study"]) / "model.json").read_text(encoding="utf-8-sig"))["source"].get("sw_version", "-")
        sw_version = f"SOLIDWORKS {int(str(v).split('.')[0]) + 1992} ({v})"
    except Exception:
        pass
    a("<section class='card flow'><h2><span class='num'>9</span>Appendix</h2><h3>Software versions</h3><table class='kv'>"
      f"<tr><td>AsmFEA</td><td>{_e(versions['asmfea'])}</td></tr>"
      f"<tr><td>CalculiX</td><td>{_e(versions['calculix'])} (ccx_dynamic.exe, PARDISO via Intel MKL 2022.2.1), MD5 "
      f"{_e(versions.get('calculix_md5', '-'))}</td></tr><tr><td>Gmsh</td><td>{_e(versions.get('gmsh', '-'))}</td></tr>"
      f"<tr><td>SolidWorks</td><td>{_e(sw_version)}</td></tr><tr><td>Python</td><td>{platform.python_version()}</td></tr>"
      "</table><details open><summary>Inputs and generated CalculiX files (SHA-256)</summary><table><tr><th>Case</th>"
      "<th>File</th><th>Bytes</th><th>SHA-256 (first 16)</th></tr>")
    for cr in result["cases"]:
        st = Path(cr["measured"]["study"])
        for f in [st / "model.json", st / "study.json", *sorted((st / "parts").glob("*.step")), Path(cr["measured"]["inp"])]:
            if f.exists():
                rel = f.relative_to(root) if root in f.parents else f
                a(f"<tr><td>{_e(cr['case']['id'])}</td><td class='muted'>{_e(rel)}</td><td class='n'>{f.stat().st_size:,}</td>"
                  f"<td><code>{_sha256(f)[:16]}</code></td></tr>")
    a("</table></details></section>")
    a(f"</main><footer>AsmFEA {_e(versions['asmfea'])} · CalculiX {_e(versions['calculix'])} · {when:%d %B %Y}. "
      f"{_e(T.USER_NOTE)}</footer></body></html>")
    out.write_text("".join(b), encoding="utf-8")
    return out


def print_pdf(html_file: Path, pdf: Path) -> Optional[Path]:
    """The page printed to PDF by Microsoft Edge (headless); None when Edge is not installed."""
    edge = next((e for e in EDGE if e.exists()), None)
    if edge is None:
        return None
    pdf.unlink(missing_ok=True)
    subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", Path(html_file).resolve().as_uri()],
                   capture_output=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return pdf if pdf.exists() else None
