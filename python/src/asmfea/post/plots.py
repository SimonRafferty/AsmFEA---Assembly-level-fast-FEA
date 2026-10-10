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


def dashed(starts, ends, dash: float):
    """Lines from each start to its end as alternating black and white dashes about ``dash`` long, ending in a whole
    dash: a pv.PolyData whose cells carry RGB colours ("colours", direct). On any background one of the two shows
    (a renderer cannot XOR colours reliably)."""
    pts, cells, rgb = [], [], []
    for a, b in zip(np.atleast_2d(np.asarray(starts, float)), np.atleast_2d(np.asarray(ends, float))):
        n = max(2, int(round(float(np.linalg.norm(b - a)) / max(dash, 1e-12))))
        k0 = len(pts)
        pts += list(a + np.outer(np.linspace(0.0, 1.0, n + 1), b - a))
        for i in range(n):
            cells += [2, k0 + i, k0 + i + 1]
            rgb.append((0, 0, 0) if i % 2 == 0 else (255, 255, 255))
    poly = pv.PolyData(np.array(pts) if pts else np.zeros((0, 3)), lines=np.array(cells, dtype=np.int64) if cells else None)
    if rgb:
        poly.cell_data["colours"] = np.array(rgb, dtype=np.uint8)
        poly.set_active_scalars("colours", preference="cell")
    return poly


def add_markers(pl: pv.Plotter, markers, centre, size) -> list:
    """Labelled min / max markers: each label set off the model, with a leader line to its point. The line is
    alternating black and white dashes and the point black with a white centre, so they show against any background
    and model (a renderer cannot XOR colours reliably), on an overlay layer sharing the camera, so the model never
    hides them. Call after the camera is set: labels move off across the view."""
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
    lines = dashed(P, labels, defaults.PLOT_LEADER_DASH * size)
    dots = pv.PolyData(P)
    for poly, width, colour, as_points in ((lines, 4, None, False),
                                          (dots, 16, (0, 0, 0), True), (dots, 8, (1, 1, 1), True)):
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(poly)
        if colour is None:                              # the dashes' own colours
            mapper.SetScalarModeToUseCellData()
            mapper.SetColorModeToDirectScalars()
            mapper.ScalarVisibilityOn()
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        if colour is not None:
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
          markers=(), beyond: str | None = None):
    """One image. markers: [(point, label)] drawn as labelled points (the field's min and max). A fresh plotter
    each time: reusing one (cleared, light kit restored, overlay layer removed) saved half the plotting time but
    lost the markers' leader lines after the first image (2026-10-09)."""
    pl = pv.Plotter(off_screen=True, window_size=defaults.PLOT_SIZE)
    pl.set_background("white")
    if clim is None and np.isnan(scalars).any() and np.isfinite(scalars).any():
        clim = (float(np.nanmin(scalars)), float(np.nanmax(scalars)))
    # beyond: "above" or "below" the key: the local peaks past the assessed stress, in their own colour
    extra = {f"{beyond}_color": PEAK_COLOUR} if beyond else {}
    pl.add_mesh(mesh, scalars=scalars, cmap=cmap, clim=clim, show_scalar_bar=True, nan_color=PLAIN_GREY, **extra,
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
PLAIN_GREY = (0.75, 0.75, 0.77)    # joint edges (no assessed stress): about SolidWorks' default part colour
PEAK_COLOUR = (0.85, 0.30, 0.85)   # local peaks beyond the assessed stress: magenta, in no part of the jet map


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
            # the joint edges: nodes whose stress is not assessed (pipeline.joint_zones), shown grey
            edges = info.get("joint_edge_nodes", info.get("tie_zone_nodes", {}))
            zone = np.concatenate([np.asarray(v, np.int64) for v in edges.values()] or [np.empty(0, np.int64)])
            frd_cache[d] = (read_frd(d / "study.frd"),
                            {s["case"]: s["step"] for s in info["steps"] if s["kind"] == "static"}, zone)
        return frd_cache[d]
    yields = np.array([model.material(owner[int(g)].material).yield_ for g in ids])
    ultimates = np.array([model.material(owner[int(g)].material).ultimate or np.nan for g in ids])
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
        frd, step_of, zone = frd_for(case)
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
        # stress and factor of safety are not shown on the joint edges (the elements on a joint's edge line): their
        # values there are artefacts of how a joint is modelled (a hinge's rigid ring, a bond's sharp edge) and grow
        # without limit with the mesh (5 900 MPa, FoS 0.04, on a folding step assessed at 125 MPa, FoS 1.66). They show
        # grey, and the min / max markers and the report's extremes are taken outside them (the user's choice,
        # 2026-10-09: artefacts are not shown anywhere). Only the edge: the stress round a joint is shown and assessed
        # (the user's request, 2026-10-10)
        joint = np.isin(ids, zone)
        vm[joint] = np.nan
        parts_res = results["load_cases"][case].get("parts", {})
        # parts not assessed at all (wholly in joint bands: bushes, a bearing check by hand) carry no stress
        unassessed = np.array([bool(parts_res.get(p, {}).get("joint_only")) for p in part_ids], dtype=bool)
        vm[unassessed[point_part]] = np.nan
        fos_raw = yields / np.maximum(vm, 1e-12)            # the assessed field: extremes and markers come from it
        # the joint edges are drawn with the stress just beside them, not grey (the user's request, 2026-10-10: the
        # grey strips along ~25 joints on a 6 mm plate hid much of it); only unassessed parts stay grey
        vm_plot, filled = fill_joint_edges(vm, joint & ~unassessed[point_part], X, point_part)
        fos = np.clip(yields / np.maximum(vm_plot, 1e-12), 0, defaults.PLOT_FOS_CAP)
        # the keys run to the assessed stress (membrane plus bending, as the checks), so local peaks past it (notches,
        # the stress next to a joint's edge) don't squeeze everything else into the bottom of the key: they show
        # magenta (the user's point, 2026-10-10: the highest peak takes the eye from the real problem areas)
        top = assessed_top(parts_res)
        vm_clim = ((float(np.nanmin(vm_plot)), top) if top and np.isfinite(vm_plot).any() and np.nanmax(vm_plot) > top
                   and np.nanmin(vm_plot) < top else None)
        fos_floor = min((p["min_fos"] for p in parts_res.values() if p.get("min_fos")), default=None)
        fos_clim = ((fos_floor, defaults.PLOT_FOS_CAP) if fos_floor and fos_floor < defaults.PLOT_FOS_CAP
                    and np.nanmin(fos) < fos_floor else (0, defaults.PLOT_FOS_CAP))
        grey = ("  joint edges: the stress beside them" if filled.any() else "") + \
            ("  grey: parts not assessed (bearing check by hand)" if np.isnan(vm_plot).any() else "")
        peak_note = "  magenta: local peaks beyond the assessed (membrane + bending) stress"
        safe = case.replace(" ", "_")
        # where each field is smallest and largest (surface nodes), for the plots and the report
        (e_u, r_u), (e_vm, r_vm), (e_f, r_f) = (_extremes(umag, X, ids, owner, "mm"),
                                                _extremes(vm, X, ids, owner, "MPa"),
                                                _extremes(fos_raw, X, ids, owner, ""))
        e_f, r_f = _governing_fos(parts_res, e_f, r_f, X, ids)
        e_vm, r_vm = _governing_stress(parts_res, e_vm, r_vm, X, ids)
        results["load_cases"][case]["extremes"] = {"displacement": e_u, "von_mises": e_vm, "fos": e_f}
        results["load_cases"][case]["joint_zone_points"] = int(joint.sum())     # joint-edge surface points
        results["load_cases"][case]["unassessed_points"] = int(np.isnan(vm_plot).sum())   # shown grey
        mk_u = _marks(e_u, r_u, X + scale * U, "{:.3g} mm")
        mk_vm = _marks(e_vm, r_vm, X, "{:.3g} MPa")
        mk_fos = _marks(e_f, r_f, X, "FoS {:.3g}", cap=defaults.PLOT_FOS_CAP)
        # the plotted surface and fields, for the interactive 3-D viewer (post.viewer)
        from .viewer import save_fields
        save_fields(study, case, X, cells, U, umag, vm_plot, fos, scale,
                    {"displacement": mk_u, "vonmises": mk_vm, "fos": mk_fos}, defaults.PLOT_FOS_CAP, out=out,
                    parts=(part_ids, point_part), yields=yields, ultimates=ultimates, run_mode=mode,
                    proportional=_proportional(case_dirs[case]), joint_edge=filled)
        for view, cam in views.items():
            p = out / f"{safe}_displacement_{view}.png"
            jobs.append(dict(mesh=deformed, scalars=umag, title="Displacement mm", clim=None, cmap="jet", path=p,
                             centre=centre, size=size, view=cam, note=f"(deformed shape x{scale:.3g})", markers=mk_u))
            images.append(p.name)
            p = out / f"{safe}_vonmises_{view}.png"
            jobs.append(dict(mesh=undeformed, scalars=vm_plot, title="von Mises MPa", clim=vm_clim, cmap="jet", path=p,
                             centre=centre, size=size, view=cam,
                             note=(grey + (peak_note if vm_clim else "")).strip(), markers=mk_vm,
                             beyond="above" if vm_clim else None))
            images.append(p.name)
            p = out / f"{safe}_fos_{view}.png"
            low = fos_clim[0] > 0
            jobs.append(dict(mesh=undeformed, scalars=fos, title="FoS on yield", clim=fos_clim,
                             cmap="jet_r", path=p, centre=centre, size=size, view=cam,
                             note=f"(colour scale capped at {defaults.PLOT_FOS_CAP:g}){grey}"
                                  + ("  magenta: local peaks, FoS below the assessed (membrane + bending) minimum"
                                     if low else ""), markers=mk_fos,
                             beyond="below" if low else None))
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


def fill_joint_edges(vm: np.ndarray, edge: np.ndarray, X: np.ndarray, point_part: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(values, filled): the joint-edge points (``edge``) given the stress of their part's assessed points beside them,
    inverse-distance weighted over the nearest JOINT_EDGE_FILL_NEAREST, in place of their own (an artefact of the
    sharp modelled edge that grows with the mesh). A part with no assessed point keeps NaN (grey)."""
    from scipy.spatial import cKDTree
    out = np.array(vm, dtype=float, copy=True)
    filled = np.zeros(len(vm), dtype=bool)
    for k in np.unique(point_part[edge]):
        mine = point_part == k
        known = mine & ~edge & np.isfinite(vm)
        holes = mine & edge
        if not known.any() or not holes.any():
            continue
        n = min(defaults.JOINT_EDGE_FILL_NEAREST, int(known.sum()))
        d, j = cKDTree(X[known]).query(X[holes], k=n)
        d, j = d.reshape(len(d), -1), j.reshape(len(j), -1)
        w = 1.0 / np.maximum(d, 1e-9) ** 2
        out[holes] = (w * vm[known][j]).sum(axis=1) / w.sum(axis=1)
        filled[holes] = True
    return out, filled


def _extremes(values: np.ndarray, X: np.ndarray, ids: np.ndarray, owner: dict, unit: str) -> tuple[dict, dict]:
    """({"min": {value, at, part, node, unit}, "max": {...}}, {"min": row, "max": row}) of a nodal
    field over the surface nodes; rows index X, for the plot markers. NaN values (joint zones) are left out."""
    out, rows = {}, {}
    if not np.isfinite(values).any():         # nothing assessed (every point in a joint zone)
        return {k: {"value": None, "at": None, "part": None, "node": None, "unit": unit} for k in ("min", "max")}, {}
    for key, i in (("min", int(np.nanargmin(values))), ("max", int(np.nanargmax(values)))):
        v = float(values[i])
        out[key] = {"value": v if np.isfinite(v) else None, "at": [round(float(c), 3) for c in X[i]],
                    "part": owner[int(ids[i])].part_id, "node": int(ids[i]), "unit": unit}
        rows[key] = i
    return out, rows


def _governing_fos(parts: dict, ext: dict, rows: dict, X: np.ndarray, ids: np.ndarray) -> tuple[dict, dict]:
    """The FoS minimum as the checks have it: the lowest part min_fos (membrane plus bending where classified, so a
    notch peak doesn't count), at that part's assessed peak, in place of the plotted field's own minimum (yield /
    nodal von Mises: 1.655 against the check's 1.659 on the folding step). The plot, the report and the check table
    then give one figure (the user's request, 2026-10-09)."""
    best = min(((p["min_fos"], name, _where(p)) for name, p in parts.items()
                if p.get("min_fos") is not None and _where(p) is not None), default=None)
    if best is None or not rows:
        return ext, rows
    value, name, at = best
    i = int(np.argmin(np.linalg.norm(X - np.asarray(at, float), axis=1)))     # the surface point there
    ext = dict(ext, min={"value": float(value), "at": [round(float(c), 3) for c in X[i]], "part": name,
                         "node": int(ids[i]), "unit": ext["min"]["unit"]})
    return ext, dict(rows, min=i)


def _where(p: dict):
    """Where a part's assessed stress is: its membrane-plus-bending section, else its peak."""
    return p.get("membrane_bending_at") or p.get("peak_vm_excl_ties_at")


def assessed_top(parts: dict) -> float | None:
    """The highest assessed stress of a load case (post.assessed_stress: membrane plus bending), the stress keys' top."""
    from . import assessed_stress
    vals = [v for p in parts.values() if (v := assessed_stress(p)) is not None]
    return max(vals) if vals else None


def _governing_stress(parts: dict, ext: dict, rows: dict, X: np.ndarray, ids: np.ndarray) -> tuple[dict, dict]:
    """The stress maximum as the checks have it: the highest assessed stress (membrane plus bending), at its section,
    in place of the field's own maximum (a local peak beside a joint's edge, which the plot shows magenta), so the
    plot, the report and the checks give one figure, as for the FoS (_governing_fos)."""
    from . import assessed_stress
    best = max(((v, name, _where(p)) for name, p in parts.items()
                if (v := assessed_stress(p)) is not None and _where(p) is not None), default=None)
    if best is None or not rows:
        return ext, rows
    value, name, at = best
    i = int(np.argmin(np.linalg.norm(X - np.asarray(at, float), axis=1)))     # the surface point there
    ext = dict(ext, max={"value": float(value), "at": [round(float(c), 3) for c in X[i]], "part": name,
                         "node": int(ids[i]), "unit": ext["max"]["unit"]})
    return ext, dict(rows, max=i)


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
