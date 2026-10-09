"""Off-screen plot images (PyVista/VTK, MIT/BSD; optional extra asmfea[report]).

For every load case: displacement magnitude (on the deformed shape), von Mises stress
and factor of safety on yield, each in four views, plus one set-up view showing fixtures
and loads. For a model exported from SolidWorks the main view ("iso") is the user's view at
export, and front, top and right are SolidWorks' standard views (Y up); for a hand-written
model they are fixed views with Z up. Successive runs of the same model are comparable.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")

import numpy as np  # noqa: E402
import pyvista as pv  # noqa: E402

from .. import defaults  # noqa: E402
from ..jobs import read_json  # noqa: E402
from ..mesh.tagging import boundary_facets  # noqa: E402
from ..model import Model  # noqa: E402
from .frd import read_frd, read_frd_modes, von_mises  # noqa: E402

pv.OFF_SCREEN = True

VIEW_FILE = "view.json"   # a main view chosen after the solve (api.replot); removed by the next full run

VIEWS = {  # name -> (direction from the target to the camera, view-up); hand-written models, Z up
    "iso": ((1.0, -1.0, 0.8), (0, 0, 1)),
    "front": ((0.0, -1.0, 0.0), (0, 0, 1)),
    "top": ((0.0, 0.0, 1.0), (0, 1, 0)),
    "right": ((1.0, 0.0, 0.0), (0, 0, 1)),
}
SW_VIEWS = {  # SolidWorks' standard views, Y up
    "iso": ((1.0, 1.0, 1.0), (0, 1, 0)),
    "front": ((0.0, 0.0, 1.0), (0, 1, 0)),
    "top": ((0.0, 1.0, 0.0), (0, 0, -1)),
    "right": ((1.0, 0.0, 0.0), (0, 1, 0)),
}


def views_for(model: Model, study: Optional[Path] = None) -> dict:
    """The plot views: SolidWorks' views with the user's view as "iso" when the model came
    from SolidWorks with one recorded, else the fixed Z-up set. A view set after the solve
    (``view.json`` in the study, from api.replot) takes the place of the export's view."""
    v = getattr(model.source, "view", None)
    later = study / VIEW_FILE if study is not None else None
    if later is not None and later.exists():
        d = read_json(later)
        return dict(SW_VIEWS) | {"iso": (tuple(d["toward_viewer"]), tuple(d["up"]))}
    if v is None:
        return dict(VIEWS)
    out = dict(SW_VIEWS)
    out["iso"] = (tuple(v.toward_viewer), tuple(v.up))
    return out


def _surface(instances):
    """Boundary surface of the assembly: (points, faces, global node ids, part per point, facet lookup)."""
    pts_ids, faces_raw, owner = [], [], {}
    facet_index = {}
    for inst in instances:
        for row, fnum, nodes in boundary_facets(inst.mesh.blocks):
            nc = 3 if len(nodes) == 6 else 4
            gids = inst.node0 + nodes[:nc]
            faces_raw.append(gids)
            facet_index[(inst.part_id, row, fnum)] = len(faces_raw) - 1
            for g in gids:
                owner[int(g)] = inst
    ids = np.array(sorted(owner), dtype=np.int64)
    pos = np.full(int(ids.max()) + 1, -1, dtype=np.int64)
    pos[ids] = np.arange(len(ids))
    X = np.empty((len(ids), 3))
    for g in ids:
        inst = owner[int(g)]
        X[pos[g]] = inst.X[int(g) - inst.node0 - 1]
    cells = []
    for f in faces_raw:
        cells += [len(f), *pos[f].tolist()]
    return X, np.asarray(cells), ids, pos, owner, facet_index


def _proportional(case_dir: Path) -> bool:
    """Whether a load case's results are proportional to its loads (the viewer's load animation scales them):
    not with separating contact or preloaded bolts in its deck."""
    try:
        info = read_json(case_dir / "deck_info.json")
    except (OSError, ValueError):
        return False
    return not info.get("contacts") and not info.get("bolts")


def _camera(pl: pv.Plotter, centre, size, view):
    d, up = view
    d = np.asarray(d, float) / np.linalg.norm(d)
    pl.camera_position = [tuple(centre + d * size * 2.2), tuple(centre), up]
    pl.reset_camera()


def add_markers(pl: pv.Plotter, markers, centre, size) -> list:
    """Labelled min / max markers: each label set off the model, with a leader line to its point. The line
    and the point are drawn twice, black and a thinner white on top, so one of them always contrasts with
    the background and the model (a renderer cannot XOR colours reliably), on an overlay layer sharing the
    camera, so the model never hides them. Call after the camera is set: labels move off across the view."""
    from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper, vtkRenderer
    if not markers:
        return []
    cam = pl.camera
    view = np.asarray(cam.focal_point, float) - np.asarray(cam.position, float)
    view /= max(np.linalg.norm(view), 1e-30)
    up = np.asarray(cam.up, float)
    P = np.array([np.asarray(m[0], float) for m in markers])
    labels = []
    for i, p in enumerate(P):
        d = p - np.asarray(centre, float)
        d -= (d @ view) * view                      # outwards from the model's centre, across the view
        if np.linalg.norm(d) < 1e-6 * size:
            d = up * (1 if i % 2 == 0 else -1)
        labels.append(p + d / np.linalg.norm(d) * defaults.PLOT_LABEL_OFFSET * size)
    labels = np.array(labels)
    label_actor = pl.add_point_labels(labels, [m[1] for m in markers], show_points=False, font_size=16, text_color="black",
                        shape_color="white", shape_opacity=0.85, always_visible=True)
    over = vtkRenderer()
    over.SetLayer(1)
    over.SetActiveCamera(pl.renderer.GetActiveCamera())
    over.InteractiveOff()
    pl.ren_win.SetNumberOfLayers(2)
    pl.ren_win.AddRenderer(over)
    pts = np.vstack([P, labels])
    n = len(P)
    lines = pv.PolyData(pts, lines=np.hstack([[2, i, n + i] for i in range(n)]))
    dots = pv.PolyData(P)
    for poly, width, colour, as_points in ((lines, 6, (0, 0, 0), False), (lines, 2, (1, 1, 1), False),
                                          (dots, 16, (0, 0, 0), True), (dots, 8, (1, 1, 1), True)):
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(poly)
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*colour)
        prop.LightingOff()
        if as_points:
            prop.SetPointSize(width)
            prop.SetRenderPointsAsSpheres(True)
        else:
            prop.SetLineWidth(width)
        over.AddActor(actor)
    # the labels on the overlay too, drawn after the lines: on the model's layer the leader line ran across the
    # first letters of its own label
    pl.remove_actor(label_actor, render=False)
    over.AddViewProp(label_actor)
    return [label_actor, over]          # the labels and the overlay layer (the viewer hides them mid-animation)


def _shot(mesh: pv.PolyData, scalars, title: str, clim, cmap, path: Path, centre, size, view, note: str = "",
          markers=()):
    """One image. markers: [(point, label)] drawn as labelled points (the field's min and max). A fresh plotter
    each time: reusing one (cleared, light kit restored, overlay layer removed) saved half the plotting time but
    lost the markers' leader lines after the first image (2026-10-09)."""
    pl = pv.Plotter(off_screen=True, window_size=defaults.PLOT_SIZE)
    pl.set_background("white")
    pl.add_mesh(mesh, scalars=scalars, cmap=cmap, clim=clim, show_scalar_bar=True,
                # an explicit key: with pyvista's defaults the title sat on top of the highest value
                scalar_bar_args={"title": title + "\n", "color": "black", "fmt": "%.3g", "vertical": True,
                                 "position_x": 0.86, "position_y": 0.10, "width": 0.09, "height": 0.66,
                                 "title_font_size": 14, "label_font_size": 12})
    pl.add_text(f"{path.stem.replace('_', ' ')}{'  ' + note if note else ''}", font_size=9, color="black")
    if _WATERMARK:
        # a fast run's plots carry their status on the image, so a screenshot cannot pass for a full result
        pl.add_text(_WATERMARK, position="upper_right", font_size=12, color=(0.85, 0.35, 0.0))
    _camera(pl, centre, size, view)
    add_markers(pl, markers, centre, size)
    pl.screenshot(str(path))
    pl.close()


_WATERMARK = ""


def _shot_job(job: dict) -> str:
    """One deferred image: the mesh travels as plain arrays (points, cells)."""
    global _WATERMARK
    _WATERMARK = job.pop("watermark", "")
    points, cells = job.pop("mesh")
    _shot(pv.PolyData(points, cells), **job)
    return job["path"].name


def _render_jobs(jobs: list[dict], also=None) -> None:
    """The images, one after another in this process (``also`` first: the setup view). Drawing them in worker
    processes was tried and gained nothing: starting VTK in each, and sending the meshes to them, cost what the
    parallel drawing saved (Robo-mower 6.5 s -> 6.0 s, a 4.5 M-DOF frame 10.3 s -> 10.9 s, 2026-10-09)."""
    if also:
        also()
    for j in jobs:
        j["watermark"] = _WATERMARK
        _shot_job(dict(j))


def render_all(study: Path, model: Model, case_dirs: dict, results: dict, level: str = "refined",
               on_progress: Callable[[str], None] = lambda s: None, mode: str = "full") -> list[str]:
    global _WATERMARK
    from ..pipeline import build_instances
    from ..runmode import FAST, results_dir
    out = results_dir(study, mode)
    out.mkdir(parents=True, exist_ok=True)
    _WATERMARK = "FAST RUN - APPROXIMATE" if mode == FAST else ""
    instances = build_instances(study, model, level, mode)
    X, cells, ids, pos, owner, facet_index = _surface(instances)
    lo, hi = X.min(axis=0), X.max(axis=0)
    centre, size = (lo + hi) / 2, float(np.linalg.norm(hi - lo))
    frd_cache: dict = {}

    def frd_for(case):
        d = case_dirs[case]
        if d not in frd_cache:
            info = read_json(d / "deck_info.json")
            frd_cache[d] = (read_frd(d / "study.frd"),
                            {s["case"]: s["step"] for s in info["steps"] if s["kind"] == "static"})
        return frd_cache[d]
    yields = np.array([model.material(owner[int(g)].material).yield_ for g in ids])
    # each surface point's part, for the 3-D viewer's point probe
    part_ids = sorted({i.part_id for i in instances})
    part_no = {p: k for k, p in enumerate(part_ids)}
    point_part = np.array([part_no[owner[int(g)].part_id] for g in ids], dtype=np.int32)
    images = []
    views = views_for(model, study)
    if mode == FAST:
        views = {"iso": views["iso"]}        # a fast run draws the main view only (the panes follow SolidWorks' view)

    # remote loads: their faces' centroid and point, as the deck worked them out
    remote = {}
    for d in set(case_dirs.values()):
        if (d / "deck_info.json").exists():
            remote.update(read_json(d / "deck_info.json").get("remote_loads", {}))
    jobs: list[dict] = []                     # the images, drawn together at the end (_render_jobs)
    setup = lambda: images.insert(0, _setup_view(study, model, instances, X, cells, pos, facet_index,  # noqa: E731
                                                 centre, size, out, views["iso"], remote))
    for case in results["load_cases"]:
        frd, step_of = frd_for(case)
        k = step_of[case]
        on_progress(f"rendering load case {case}")
        un, uv = frd[k]["DISP"]
        sn, sv = frd[k]["STRESS"]
        U = np.zeros((len(ids), 3))
        keep = un < len(pos)
        sel = pos[un[keep]] >= 0
        U[pos[un[keep]][sel]] = uv[keep][sel, :3]
        vm = np.zeros(len(ids))
        keep = sn < len(pos)
        sel = pos[sn[keep]] >= 0
        vm[pos[sn[keep]][sel]] = von_mises(sv[keep][sel, :6])
        umag = np.linalg.norm(U, axis=1)
        scale = defaults.PLOT_DEFORM_FRACTION * size / umag.max() if umag.max() > 0 else 0.0
        deformed = (X + scale * U, cells)
        undeformed = (X, cells)
        fos_raw = yields / np.maximum(vm, 1e-12)
        fos = np.clip(fos_raw, 0, defaults.PLOT_FOS_CAP)
        safe = case.replace(" ", "_")
        # where each field is smallest and largest (surface nodes), for the plots and the report
        (e_u, r_u), (e_vm, r_vm), (e_f, r_f) = (_extremes(umag, X, ids, owner, "mm"),
                                                _extremes(vm, X, ids, owner, "MPa"),
                                                _extremes(fos_raw, X, ids, owner, ""))
        results["load_cases"][case]["extremes"] = {"displacement": e_u, "von_mises": e_vm, "fos": e_f}
        mk_u = _marks(e_u, r_u, X + scale * U, "{:.3g} mm")
        mk_vm = _marks(e_vm, r_vm, X, "{:.3g} MPa")
        mk_fos = _marks(e_f, r_f, X, "FoS {:.3g}", cap=defaults.PLOT_FOS_CAP)
        # the plotted surface and fields, for the interactive 3-D viewer (post.viewer)
        from .viewer import save_fields
        save_fields(study, case, X, cells, U, umag, vm, fos, scale,
                    {"displacement": mk_u, "vonmises": mk_vm, "fos": mk_fos}, defaults.PLOT_FOS_CAP, out=out,
                    parts=(part_ids, point_part), yields=yields, run_mode=mode,
                    proportional=_proportional(case_dirs[case]))
        for view, cam in views.items():
            p = out / f"{safe}_displacement_{view}.png"
            jobs.append(dict(mesh=deformed, scalars=umag, title="Displacement mm", clim=None, cmap="jet", path=p,
                             centre=centre, size=size, view=cam, note=f"(deformed shape x{scale:.3g})", markers=mk_u))
            images.append(p.name)
            p = out / f"{safe}_vonmises_{view}.png"
            jobs.append(dict(mesh=undeformed, scalars=vm, title="von Mises MPa", clim=None, cmap="jet", path=p,
                             centre=centre, size=size, view=cam, markers=mk_vm))
            images.append(p.name)
            p = out / f"{safe}_fos_{view}.png"
            jobs.append(dict(mesh=undeformed, scalars=fos, title="FoS on yield", clim=(0, defaults.PLOT_FOS_CAP),
                             cmap="jet_r", path=p, centre=centre, size=size, view=cam,
                             note=f"(colour scale capped at {defaults.PLOT_FOS_CAP:g})", markers=mk_fos))
            images.append(p.name)
    for case, bk in results.get("buckling", {}).items():
        on_progress(f"rendering buckling modes of {case}")
        modes = read_frd_modes(case_dirs[f"buckling:{case}"] / "study.frd", bk["step"])
        for i, (un, uv) in enumerate(modes[:defaults.PLOT_BUCKLING_MODES], start=1):
            U = np.zeros((len(ids), 3))
            keep = un < len(pos)
            sel = pos[un[keep]] >= 0
            U[pos[un[keep]][sel]] = uv[keep][sel, :3]
            umag = np.linalg.norm(U, axis=1)
            scale = defaults.PLOT_MODE_FRACTION * size / umag.max() if umag.max() > 0 else 0.0
            factor = bk["factors"][i - 1] if i - 1 < len(bk["factors"]) else float("nan")
            for view in [v for v in ("iso", "front") if v in views]:
                p = out / f"{case.replace(' ', '_')}_buckling_mode{i}_{view}.png"
                jobs.append(dict(mesh=(X + scale * U, cells), scalars=umag / max(umag.max(), 1e-30),
                                 title="relative displacement", clim=(0, 1), cmap="jet", path=p, centre=centre,
                                 size=size, view=views[view], note=f"(load factor {factor:.4g})"))
                images.append(p.name)
    on_progress(f"drawing {len(jobs) + 1} plot images")
    _render_jobs(jobs, also=setup)
    return images


def _extremes(values: np.ndarray, X: np.ndarray, ids: np.ndarray, owner: dict, unit: str) -> tuple[dict, dict]:
    """({"min": {value, at, part, node, unit}, "max": {...}}, {"min": row, "max": row}) of a nodal
    field over the surface nodes; rows index X, for the plot markers."""
    out, rows = {}, {}
    for key, i in (("min", int(np.argmin(values))), ("max", int(np.argmax(values)))):
        v = float(values[i])
        out[key] = {"value": v if np.isfinite(v) else None, "at": [round(float(c), 3) for c in X[i]],
                    "part": owner[int(ids[i])].part_id, "node": int(ids[i]), "unit": unit}
        rows[key] = i
    return out, rows


def _marks(ext: dict, rows: dict, P: np.ndarray, fmt: str, cap: float | None = None) -> list:
    """Labelled points for a field's min and max; P: the plotted node positions (rows of X)."""
    out = []
    for key in ("min", "max"):
        v = ext[key]["value"]
        if v is None:
            continue
        text = f"max > {cap:g}" if cap is not None and v > cap else f"{key} " + fmt.format(v)
        out.append((P[rows[key]], text))
    return out


def _setup_view(study, model, instances, X, cells, pos, facet_index, centre, size, out: Path, cam,
                remote: dict | None = None) -> str:
    """Grey model, fixtures in blue, loaded faces in red, arrows for load directions."""
    geo = read_json(study / "geometry.json")
    face_map = {k: tuple(v) for k, v in geo["face_map"].items()}
    by = {i.part_id: i for i in instances}
    pl = pv.Plotter(off_screen=True, window_size=defaults.PLOT_SIZE)
    pl.set_background("white")
    pl.add_mesh(pv.PolyData(X, cells), color="lightgrey", opacity=0.6)

    def face_mesh(fid):
        part, idx = face_map[fid]
        inst = by.get(part)
        if inst is None:
            return None
        sub = []
        for _, fnum, etype, conn in inst.face_elements(idx):
            from ..mesh.elements import FACES
            corner = conn[FACES[etype][fnum - 1]] + inst.node0
            sub += [len(corner), *pos[corner].tolist()]
        return pv.PolyData(X, np.asarray(sub)) if sub else None

    for fx in model.fixtures:
        for fid in fx.faces:
            fm = face_mesh(fid) if fid in face_map else None
            if fm is not None:
                pl.add_mesh(fm, color="royalblue")
    for ld in model.loads:
        if ld.type == "gravity":
            d = np.asarray(ld.direction, float)
            pl.add_arrows(np.array([centre + [0, 0, 0.45 * size]]), np.array([d / np.linalg.norm(d)]),
                          mag=0.15 * size, color="darkgreen")
            continue
        if ld.type == "remote_force" and ld.id in (remote or {}):
            # the faces, a line from their centroid to the remote point, and the force arrow ending there
            for fid in ld.faces:
                fm = face_mesh(fid) if fid in face_map else None
                if fm is not None:
                    pl.add_mesh(fm, color="red")
            c, p = (np.asarray(remote[ld.id][k], float) for k in ("centroid", "point"))
            if np.linalg.norm(p - c) > 1e-9:
                pl.add_mesh(pv.Line(c, p), color="red", line_width=3)
                pl.add_mesh(pv.Sphere(radius=0.008 * size, center=p), color="red")
            d = np.asarray(ld.direction, float)
            d = d / np.linalg.norm(d)
            pl.add_arrows(np.array([p - d * 0.12 * size]), np.array([d]), mag=0.12 * size, color="red")
            continue
        for fid in ld.faces:
            fm = face_mesh(fid) if fid in face_map else None
            if fm is None:
                continue
            pl.add_mesh(fm, color="red")
            if ld.direction is not None:
                d = np.asarray(ld.direction, float)
                c = np.asarray(fm.center)
                pl.add_arrows(np.array([c - d / np.linalg.norm(d) * 0.12 * size]),
                              np.array([d / np.linalg.norm(d)]), mag=0.12 * size, color="red")
    pl.add_text("set-up: fixtures blue, loaded faces red", font_size=9, color="black")
    _camera(pl, centre, size, cam)
    path = out / "setup_iso.png"
    pl.screenshot(str(path))
    pl.close()
    return path.name
