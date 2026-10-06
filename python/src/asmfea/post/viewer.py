"""Interactive 3-D view of one result field (task pane: Stress / Displacement / FoS).

The post stage saves each load case's plotted surface as ``results/<case>_fields.vtp`` (point
arrays for every field, the displacement vector) with ``<case>_fields.json`` beside it (the
deformation scale and the min/max markers), so the window opens without reading the results
again. It shows the same colours, scale bar and markers as the report's plots, starts on the
main view (the SolidWorks view, as for the plots) and rotates, pans and zooms with the mouse;
the Reset view button or the R key returns to the main view.

Run as ``python -m asmfea.post.viewer <study> <case> <field>`` (api.view starts it detached).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

FIELDS = {  # field -> (point array, scale bar title, colour map, window title)
    "vonmises": ("von Mises MPa", "von Mises MPa", "jet", "von Mises stress"),
    "displacement": ("|U| mm", "Displacement mm", "jet", "displacement"),   # the key's font has no "|"
    "fos": ("FoS on yield", "FoS on yield", "jet_r", "factor of safety"),
}


def field_files(study: Path, case: str, out: Path | None = None) -> tuple[Path, Path]:
    """The 3-D viewer's data for a load case: in `out`, else the newer of the full and the fast results."""
    from ..runmode import latest_results
    safe = case.replace(" ", "_")
    out = out or latest_results(study)[0]
    return out / f"{safe}_fields.vtp", out / f"{safe}_fields.json"


def save_fields(study: Path, case: str, X, cells, U, umag, vm, fos, scale: float, markers: dict, fos_cap: float,
                out: Path | None = None, parts: tuple | None = None, yields=None, run_mode: str = "full",
                proportional: bool = True) -> None:
    """Called by post.plots.render_all for each load case. parts: (part ids, part index per point) and
    yields (yield strength per point) let the viewer's point probe name the part and give an uncapped FoS."""
    import pyvista as pv
    vtp, meta = field_files(study, case, out)
    mesh = pv.PolyData(np.asarray(X, float), cells)
    mesh.point_data["U"] = np.asarray(U, np.float32)
    mesh.point_data["|U| mm"] = np.asarray(umag, np.float32)
    mesh.point_data["von Mises MPa"] = np.asarray(vm, np.float32)
    mesh.point_data["FoS on yield"] = np.asarray(fos, np.float32)
    if parts is not None:
        mesh.point_data["part"] = np.asarray(parts[1], np.int32)
    if yields is not None:
        mesh.point_data["yield MPa"] = np.asarray(yields, np.float32)
    mesh.save(str(vtp))
    meta.write_text(json.dumps({"schema_version": "1.0", "case": case, "deform_scale": scale, "fos_cap": fos_cap,
                                "run_mode": run_mode, "parts": list(parts[0]) if parts is not None else [],
                                "proportional": bool(proportional),
                                "markers": {k: [[list(map(float, p)), t] for p, t in v] for k, v in markers.items()}}))


def show(study: Path, case: str, field: str) -> None:
    import pyvista as pv
    from ..model import load_model
    from .plots import _camera, views_for  # noqa: F401

    vtp, meta_path = field_files(study, case)
    mesh = pv.read(str(vtp))
    meta = json.loads(meta_path.read_text())
    array, bar, cmap, what = FIELDS[field]
    base = mesh.copy(deep=True)                    # full-load values and the undeformed shape (probe, animation)
    if field == "displacement":
        mesh.points = mesh.points + meta["deform_scale"] * mesh.point_data["U"]
    # the colour range is the full load's, so it stays put while the load animation scales the values
    values = np.asarray(mesh.point_data[array])
    clim = (0, meta["fos_cap"]) if field == "fos" else (float(values.min()), float(values.max()))
    lo, hi = np.asarray(mesh.bounds[::2]), np.asarray(mesh.bounds[1::2])
    centre, size = (lo + hi) / 2, float(np.linalg.norm(hi - lo))
    model = load_model(study)
    main = views_for(model, study)["iso"]
    # the title in the view, as the report's captions: assembly (no .SLDASM / .SLDPRT) - study - load case
    title = f"{Path(model.source.assembly).stem} - {model.study.name} - {case}"

    # tests (and a check without a desktop) render off screen to this file instead of opening a window
    shot = os.environ.get("ASMFEA_VIEWER_SCREENSHOT")
    W, H = WINDOW
    if not shot:
        _taskbar_identity()
    pl = pv.Plotter(title=f"AsmFEA: {what}, {case}", window_size=WINDOW, off_screen=bool(shot))
    _window_icon(pl)
    pl.set_background("white")
    mx, my = MARGIN_PX / W, MARGIN_PX / H                   # the frame's inset, as fractions of the window
    surface = pl.add_mesh(mesh, scalars=array, cmap=cmap, clim=clim, show_scalar_bar=True,
                          scalar_bar_args={"title": bar, "color": "black", "fmt": "%.3g", "vertical": True,
                                           "position_x": 1 - 3 * mx - 0.11, "position_y": KEY_BOTTOM,
                                           "width": 0.11, "height": KEY_TOP - KEY_BOTTOM, "title_font_size": 14,
                                           "label_font_size": 13})
    _frame(pl)
    marks = meta["markers"].get(field, [])
    note = ""                                      # the deformation scale is on its slider (bottom)
    fast = "\nFAST RUN - APPROXIMATE" if meta.get("run_mode") == "fast" else ""
    # no instructions in the view (the user's choice): the controls are labelled, the help is in the guide
    top = []                                       # texts stacked down from the top left, in this order
    if not meta.get("proportional", True):
        # the animation scales the full-load result; with contact or preloaded bolts the real response is not
        # proportional to the load, so it's indicative only there (the user's choice: allowed, with a warning)
        warn = pl.add_text("Load animation: this model has contact or preloaded bolts, whose response is not "
                           "proportional to the load.\nThe animation scales the full-load result, so it is "
                           "indicative only (exact at 100 %).", position=(0, 0), font_size=9,
                           color=(0.70, 0.22, 0.0))
        warn.GetTextProperty().SetBold(True)
        top.append(warn)
    top.append(pl.add_text(f"{title}{note}{fast}", position=(0, 0), font_size=11, color="black"))
    for a in top:
        _backing(a.GetTextProperty())
    _stack_top_left(pl, top)
    key = pl.scalar_bar
    key.SetVerticalTitleSeparation(KEY_TITLE_GAP_PX)   # the title clear of the highest value (it sat on it)
    key.DrawBackgroundOn()
    key.GetBackgroundProperty().SetColor(1, 1, 1)
    key.GetBackgroundProperty().SetOpacity(0.88)
    _backing(key.GetTitleTextProperty())
    # the SolidWorks mouse: left rotates, the wheel zooms, right pans (VTK's default zooms on the right)
    pl.enable_custom_trackball_style(left="rotate", shift_left="pan", control_left="spin", middle="pan",
                                     right="pan", shift_right="pan", control_right="pan")

    def reset(state=None):      # the button passes its state; the R key passes nothing
        _camera(pl, centre, size, main)
        pl.render()
    # bottom left: the load animation's play button, then Reset view (a checkbox widget used as a push
    # button: every click resets) and the R key
    bx, by = MARGIN_PX, MARGIN_PX                   # in the band below the border
    rx = bx + BUTTON_PX + 12
    pl.add_checkbox_button_widget(reset, value=False, position=(rx, by), size=BUTTON_PX, border_size=2,
                                  color_on="lightgrey", color_off="lightgrey")
    pl.add_text("Reset view (R)", position=(rx + BUTTON_PX + 8, by + 4), font_size=9, color="black")
    pl.add_key_event("r", reset)
    _camera(pl, centre, size, main)
    min_max = _Markers(pl, base, marks, meta["deform_scale"] if field == "displacement" else 0.0, centre, size)
    spot = _Probe(pl, surface, mesh, meta, field, base=base)
    anim = _LoadAnimation(pl, mesh, base, meta, field, spot, markers=min_max)
    anim.add_button((bx, by))
    anim.add_shape_controls((rx + BUTTON_PX + 160, by))
    min_max.add_tickbox()
    if shot:
        at = os.environ.get("ASMFEA_VIEWER_PROBE")        # tests: probe at this window position ("x,y" pixels)
        if at:
            pl.render()
            spot.click(*(int(v) for v in at.split(",")))
        if os.environ.get("ASMFEA_VIEWER_DEFORM"):         # tests: this deformation scale
            anim.set_scale(float(os.environ["ASMFEA_VIEWER_DEFORM"]))
        if os.environ.get("ASMFEA_VIEWER_GHOST"):          # tests: the undeformed shape shown
            anim.show_ghost(True)
        if os.environ.get("ASMFEA_VIEWER_LOAD"):           # tests: the animation's frame at this load fraction
            anim.set_fraction(float(os.environ["ASMFEA_VIEWER_LOAD"]))
        pl.screenshot(shot)
        pl.close()
        return
    min_max.attach()                                # before the probe: a press on a label is a drag, not a click
    spot.attach()
    pl.show()


WINDOW = (1100, 800)
MARGIN_PX = 12              # the frame's inset from the window edge; text and controls sit inside it
CLICK_PX = 4                # a press and release closer than this is a click (further is a rotate or pan)
LABEL_OFFSET_PX = 36        # a probe's label is set off this far from its point, joined by a leader line
BUTTON_PX = 28              # the play and Reset view buttons
ANIMATION_PERIOD_S = 6.0    # the load animation: 3 s rising from zero to the full load, 3 s falling back
ANIMATION_FRAME_MS = 33     # about 30 frames a second
DEFORM_SLIDER_MAX = 3.0     # the deformation slider runs from 0 to 3 x the automatic exaggeration
GHOST_FEATURE_DEG = 30.0    # the undeformed ghost: edges where the surface turns more than this, and outlines
KEY_TITLE_GAP_PX = 14            # pixels between the key's title and its bar
KEY_BOTTOM, KEY_TOP = 0.12, 0.78   # the colour key's extent (fractions of the window), inside the border
CONTROLS_BAND_PX = 72       # the band of controls below the border: buttons, tickboxes and the slider with its value
MARKER_OFFSET_PX = 70       # a min / max label's centre sits this far out from its point (away from the model)


class _Markers:
    """The min / max markers: a dot on each extreme's node and a framed label with the full load's value,
    joined by a leader line. All three are drawn in window pixels (on top of the model), placed before every
    draw: the dot follows its node at the shape now shown (deformation scale and load fraction, so it rides
    the load animation), and the label sits a little way out from the model's centre, kept inside the frame,
    so zooming in never pushes it off the window. A label can be dragged; it then stays where it was dropped
    (as a fraction of the window) while its leader keeps pointing at the dot. Their values are the full
    load's at every frame of the animation (the user's choice: useful in either mode). The 'Min / max'
    tickbox (bottom right) shows or hides them."""

    def __init__(self, pl, base, marks: list, opening_scale: float, centre, size):
        self.pl, self.base, self.centre, self.size = pl, base, np.asarray(centre, float), size
        self.labels = [m[1] for m in marks]
        self.points = [np.asarray(m[0], float) for m in marks]
        self.nodes = None
        if marks and "U" in base.point_data:
            from scipy.spatial import cKDTree
            X, U = np.asarray(base.points), np.asarray(base.point_data["U"])
            # the saved marker points are on the window's opening shape: their nodes, found there
            _, self.nodes = cKDTree(X + opening_scale * U).query(np.array(self.points))
        self.scale, self.fraction, self.on = float(opening_scale), 1.0, True
        self.items = [self._item(text) for text in self.labels]
        self.drag = None                     # (item, offset of the press from the label's corner) while dragging
        self.box = self.text = None
        self._placed_w = None
        self._tags: dict = {}
        pl.renderer.AddObserver("StartEvent", self._follow)

    # ---------------------------------------------------------------- drawing

    def _item(self, text: str) -> dict:
        import pyvista as pv
        from vtkmodules.vtkRenderingCore import vtkActor2D, vtkPolyDataMapper2D, vtkTextActor
        label = vtkTextActor()
        label.SetInput(text)
        tp = label.GetTextProperty()
        tp.SetFontSize(17)
        tp.SetBold(True)
        tp.SetColor(0, 0, 0)
        tp.SetBackgroundColor(1, 1, 1)
        tp.SetBackgroundOpacity(0.88)
        tp.SetFrame(True)
        tp.SetFrameColor(0.25, 0.25, 0.25)
        tp.SetFrameWidth(1)

        def actor2d(poly, colour, width=1.0):
            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(poly)
            a = vtkActor2D()
            a.SetMapper(mapper)
            a.GetProperty().SetColor(*colour)
            a.GetProperty().SetLineWidth(width)
            a.PickableOff()
            return a
        leader = pv.PolyData(np.zeros((2, 3)), lines=np.array([2, 0, 1]))
        line = actor2d(leader, (0, 0, 0), 1.5)
        ang = np.linspace(0, 2 * np.pi, 20, endpoint=False)

        def disc(r):                                     # a filled circle of radius r pixels round (0, 0)
            pts = np.column_stack([r * np.cos(ang), r * np.sin(ang), np.zeros_like(ang)])
            return pv.PolyData(pts, faces=np.hstack([[len(ang)], np.arange(len(ang))]))
        outer, inner = actor2d(disc(7.0), (0, 0, 0)), actor2d(disc(3.5), (1, 1, 1))
        label.PickableOff()
        for a in (line, outer, inner, label):
            self.pl.renderer.AddViewProp(a)
        return {"label": label, "line": line, "leader": leader.GetPoints(), "dots": (outer, inner), "custom": None}

    def positions(self) -> list:
        """The markers' points on the shape as now shown (deformation scale x load fraction)."""
        if self.nodes is None:
            return self.points
        X, U = np.asarray(self.base.points), np.asarray(self.base.point_data["U"])
        return list(X[self.nodes] + (self.scale * self.fraction) * U[self.nodes])

    def update(self, scale: float, t: float) -> None:
        """The shape is now at this deformation scale and load fraction (placed at the next draw)."""
        self.scale, self.fraction = float(scale), float(t)

    def set_visible(self, on: bool) -> None:
        self.on = bool(on)
        for it in self.items:
            for a in (it["label"], it["line"], *it["dots"]):
                a.SetVisibility(self.on)

    def _display(self, p) -> tuple:
        ren = self.pl.renderer
        ren.SetWorldPoint(*p, 1.0)
        ren.WorldToDisplay()
        x, y, _ = ren.GetDisplayPoint()
        return x, y

    def _limits(self, bw: float, bh: float) -> tuple:
        """Where a label's lower left corner may go: inside the frame, clear of the header, the bottom row
        and the colour key."""
        w, h = self.pl.renderer.GetRenderWindow().GetSize()
        x0, y0 = 2 * MARGIN_PX, CONTROLS_BAND_PX + 6
        x1 = (1 - 3 * MARGIN_PX / w - 0.11) * w - 8 - bw
        y1 = h - 2 * MARGIN_PX - 48 - bh
        return x0, max(x0, x1), y0, max(y0, y1)

    def _follow(self, *_args) -> None:
        if not self.on or not self.items:
            return
        ren = self.pl.renderer
        w, h = ren.GetRenderWindow().GetSize()
        cx, cy = self._display(self.centre)
        for it, p in zip(self.items, self.positions()):
            dx, dy = self._display(p)
            for a in it["dots"]:
                a.SetPosition(dx, dy)
            box = [0.0, 0.0, 0.0, 0.0]
            it["label"].GetBoundingBox(ren, box)
            bw, bh = box[1] - box[0], box[3] - box[2]
            if it["custom"] is not None:                 # dropped here by the user
                ax, ay = it["custom"][0] * w, it["custom"][1] * h
            else:                                        # out from the model's centre, past the point
                v = np.array([dx - cx, dy - cy])
                n = float(np.linalg.norm(v))
                v = v / n if n > 1e-6 else np.array([0.7, 0.7])
                ax, ay = dx + v[0] * MARKER_OFFSET_PX - bw / 2, dy + v[1] * MARKER_OFFSET_PX - bh / 2
            x0, x1, y0, y1 = self._limits(bw, bh)
            ax, ay = min(max(ax, x0), x1), min(max(ay, y0), y1)
            it["label"].SetPosition(ax, ay)
            # the leader runs from the dot to the nearest point of the label's box; a dot outside the border
            # (zoomed in, or panned away) is hidden, and its leader stops at the border, pointing towards it
            ex, ey = min(max(dx, ax), ax + bw), min(max(dy, ay), ay + bh)
            fx0, fy0, fx1, fy1 = frame_rect(w, h)
            inside = fx0 <= dx <= fx1 and fy0 <= dy <= fy1
            for a in it["dots"]:
                a.SetVisibility(self.on and inside)
            sx, sy = (dx, dy) if inside else _clip_to(ex, ey, dx, dy, (fx0, fy0, fx1, fy1))
            it["leader"].SetPoint(0, sx, sy, 0.0)
            it["leader"].SetPoint(1, ex, ey, 0.0)
            it["leader"].Modified()

    # ---------------------------------------------------------------- dragging

    def attach(self) -> None:
        """Mouse handling for dragging labels. The press is observed ahead of the mouse style (priority 10) and,
        on a label, aborted, so the style neither rotates the view nor sees a probe click."""
        iren = self.pl.iren.interactor
        self._tags["press"] = iren.AddObserver("LeftButtonPressEvent", self._press, 10.0)
        self._tags["move"] = iren.AddObserver("MouseMoveEvent", self._move, 10.0)
        iren.AddObserver("LeftButtonReleaseEvent", self._release, 10.0)
        self.pl.iren.add_observer("LeftButtonReleaseEvent", self._release)   # the route the style passes on

    def label_box(self, it) -> list:
        """A label's box in window pixels [xmin, xmax, ymin, ymax] (GetBoundingBox leaves out its position)."""
        b = [0.0, 0.0, 0.0, 0.0]
        it["label"].GetBoundingBox(self.pl.renderer, b)
        px, py = it["label"].GetPosition()
        return [px + b[0], px + b[1], py + b[2], py + b[3]]

    def _hit(self, x: float, y: float):
        if not self.on:
            return None
        for it in reversed(self.items):                  # the topmost first
            box = self.label_box(it)
            if box[0] - 3 <= x <= box[1] + 3 and box[2] - 3 <= y <= box[3] + 3:
                px, py = it["label"].GetPosition()       # held by its position (the frame sits outside it)
                return it, (x - px, y - py)
        return None

    def _press(self, iren, _event) -> None:
        hit = self._hit(*iren.GetEventPosition())
        if hit is not None:
            self.drag = hit
            iren.GetCommand(self._tags["press"]).SetAbortFlag(1)

    def _move(self, iren, _event) -> None:
        if self.drag is None:
            return
        it, (ox, oy) = self.drag
        x, y = iren.GetEventPosition()
        w, h = self.pl.renderer.GetRenderWindow().GetSize()
        it["custom"] = ((x - ox) / w, (y - oy) / h)
        iren.GetCommand(self._tags["move"]).SetAbortFlag(1)
        self.pl.render()

    def _release(self, *_args) -> None:
        self.drag = None

    # ---------------------------------------------------------------- the tickbox

    def add_tickbox(self) -> None:
        """The 'Min / max' tickbox at the bottom right, under the key (kept there as the window is resized)."""
        if not self.labels:
            return

        def toggle(on):
            self.set_visible(on)
            self.pl.render()
        self.box = self.pl.add_checkbox_button_widget(
            toggle, value=True, position=(0, 0), size=BUTTON_PX, border_size=2, color_on=(0.45, 0.47, 0.50),
            color_off="white", background_color=(0.45, 0.47, 0.50))
        self.text = self.pl.add_text("Min / max", position=(0, 0), font_size=9, color="black")
        self.pl.renderer.AddObserver("StartEvent", self._place)
        self._place()

    def _place(self, *_args) -> None:
        ren = self.pl.renderer
        w = ren.GetRenderWindow().GetSize()[0]
        if w == self._placed_w:
            return
        box = [0.0, 0.0, 0.0, 0.0]
        self.text.GetBoundingBox(ren, box)
        tw = max(box[1] - box[0], 60.0)
        x, y = w - MARGIN_PX - tw - 8 - BUTTON_PX, MARGIN_PX
        self.box.GetRepresentation().PlaceWidget([x, x + BUTTON_PX, y, y + BUTTON_PX, 0.0, 0.0])
        self.text.SetPosition(x + BUTTON_PX + 8, y + 4)
        self._placed_w = w


def load_fraction(seconds: float) -> float:
    """The animation's load fraction after ``seconds``: a triangle wave, linear up for half the period and
    linear down for the other half."""
    p = (seconds % ANIMATION_PERIOD_S) / ANIMATION_PERIOD_S
    return 2.0 * p if p < 0.5 else 2.0 - 2.0 * p


def _icon(kind: str, n: int = 56):
    """A tape-recorder button face as a VTK image: 'play' (a triangle) or 'stop' (a square)."""
    import pyvista as pv
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    img = np.zeros((n, n, 4), np.uint8)
    img[...] = (225, 225, 225, 255)                                   # the face
    edge = (xx < 3) | (yy < 3) | (xx > n - 3) | (yy > n - 3)
    img[edge] = (120, 120, 120, 255)                                  # its border
    if kind == "play":
        x0, length, half = n * 0.34, n * 0.38, n * 0.30
        mark = (xx >= x0) & (np.abs(yy - n / 2) <= half * (1 - (xx - x0) / length))
    else:
        mark = (np.abs(yy - n / 2) <= n * 0.22) & (np.abs(xx - n / 2) <= n * 0.22)
    img[mark] = (40, 40, 40, 255)
    im = pv.ImageData(dimensions=(n, n, 1))
    im.point_data["rgba"] = img.reshape(-1, 4)                        # row 0 is the bottom, as VTK draws it
    return im


class _LoadAnimation:
    """The play button: ramps the load from zero to the full load and back (ANIMATION_PERIOD_S) until
    pressed again. A linear static result is proportional to its load, so each frame scales the saved
    full-load fields: the colours (against the full load's fixed scale), the displacement window's deformed
    shape, and an open probe's values. Pressing it again stops the animation and returns the view to the full
    load (the user's choice over pausing). No load readout: it changed too fast to read (the user's call)."""

    def __init__(self, pl, mesh, base, meta: dict, field: str, probe, markers=None):
        self.pl, self.mesh, self.base, self.meta, self.field, self.probe = pl, mesh, base, meta, field, probe
        self.markers = markers              # the min / max markers (_Markers): follow the shape
        # the deformation shown: the displacement window starts at the automatic exaggeration (as its plot),
        # the others undeformed; the slider changes it in any window
        self.auto_scale = float(meta.get("deform_scale") or 0.0)
        self.scale = self.auto_scale if field == "displacement" else 0.0
        self.ghost = None
        self.slider = self.ghost_box = None
        self.array = FIELDS[field][0]
        self.timer = None
        self.observer = None
        self.t0 = 0.0                       # the animation clock's start (perf_counter)
        self.fraction = 1.0

    def add_button(self, at) -> None:
        from vtkmodules.vtkInteractionWidgets import vtkButtonWidget, vtkTexturedButtonRepresentation2D
        rep = vtkTexturedButtonRepresentation2D()
        rep.SetNumberOfStates(2)
        rep.SetButtonTexture(0, _icon("play"))
        rep.SetButtonTexture(1, _icon("stop"))
        rep.SetPlaceFactor(1)
        rep.PlaceWidget([at[0], at[0] + BUTTON_PX, at[1], at[1] + BUTTON_PX, 0.0, 0.0])
        widget = vtkButtonWidget()
        widget.SetInteractor(self.pl.iren.interactor)
        widget.SetRepresentation(rep)
        widget.AddObserver("StateChangedEvent", lambda w, _e: self.play(w.GetRepresentation().GetState() == 1))
        widget.On()
        owner = getattr(self.pl, "widgets", self.pl)   # the plotter keeps it alive and tidies it on close
        owner.button_widgets.append(widget)

    def set_scale(self, scale: float) -> None:
        """The deformation slider: the displacement shown is scale x the real one (0: undeformed)."""
        self.scale = max(float(scale), 0.0)
        if self.slider is not None and abs(self.slider.GetRepresentation().GetValue() - self.scale) > 1e-12:
            self.slider.GetRepresentation().SetValue(self.scale)
        self.set_fraction(self.fraction)

    def show_ghost(self, on: bool) -> None:
        """The tickbox: a light grey outline of the undeformed shape (its feature edges), made when first asked."""
        if self.ghost is None:
            if not on:
                return
            edges = self.base.extract_feature_edges(feature_angle=GHOST_FEATURE_DEG, boundary_edges=True,
                                                    non_manifold_edges=False, manifold_edges=False)
            self.ghost = self.pl.add_mesh(edges, color=(0.45, 0.47, 0.50), line_width=1, opacity=0.7,
                                          pickable=False, reset_camera=False, render=False)
        self.ghost.SetVisibility(bool(on))
        if self.ghost_box is not None and self.ghost_box.GetRepresentation().GetState() != int(bool(on)):
            self.ghost_box.GetRepresentation().SetState(int(bool(on)))

    def add_shape_controls(self, at) -> None:
        """The 'Undeformed shape' tickbox and the deformation slider, along the bottom from ``at`` (pixels)."""
        x, y = at

        def ghost(on):
            self.show_ghost(on)
            self.pl.render()
        self.ghost_box = self.pl.add_checkbox_button_widget(
            ghost, value=False, position=(x, y), size=BUTTON_PX, border_size=2, color_on=(0.45, 0.47, 0.50),
            color_off="white", background_color=(0.45, 0.47, 0.50))
        self.pl.add_text("Undeformed shape", position=(x + BUTTON_PX + 8, y + 4), font_size=9, color="black")
        # the slider: its own label on the left, the value above its knob (0 = undeformed)
        sx = x + BUTTON_PX + 210
        self.pl.add_text("Deformation x", position=(sx, y + 4), font_size=9, color="black")
        w, h = self.pl.window_size
        top = self.auto_scale * DEFORM_SLIDER_MAX if self.auto_scale > 0 else 1.0
        self.slider = self.pl.add_slider_widget(
            lambda value: (self.set_scale(value), self.pl.render()), [0.0, top], value=self.scale,
            pointa=((sx + 120) / w, (y + 14) / h), pointb=(0.72, (y + 14) / h), color="black", style="modern",
            fmt="%.3g", tube_width=0.004, slider_width=0.012, interaction_event="always")
        rep = self.slider.GetRepresentation()
        rep.GetLabelProperty().SetColor(0, 0, 0)
        rep.SetLabelHeight(0.022)

    def play(self, on: bool) -> None:
        """Start the animation from zero load, or stop it and show the full load again."""
        import time
        iren = self.pl.iren.interactor
        if on and self.timer is None:
            self.t0 = time.perf_counter()
            if self.observer is None:
                self.observer = self.pl.iren.add_observer("TimerEvent", self._tick)
            self.timer = iren.CreateRepeatingTimer(ANIMATION_FRAME_MS)
        elif not on and self.timer is not None:
            iren.DestroyTimer(self.timer)
            self.timer = None
            self.set_fraction(1.0)
            self.pl.render()

    def _tick(self, *_args) -> None:
        import time
        if self.timer is None:
            return
        self.set_fraction(load_fraction(time.perf_counter() - self.t0))
        self.pl.render()

    def set_fraction(self, t: float) -> None:
        """Show the results at fraction t of the full load."""
        self.fraction = t = float(t)
        full = np.asarray(self.base.point_data[self.array], float)
        if self.field == "fos":
            cap = float(self.meta["fos_cap"])
            if "yield MPa" in self.base.point_data:
                vm = np.asarray(self.base.point_data["von Mises MPa"], float) * t
                fy = np.asarray(self.base.point_data["yield MPa"], float)
                vals = np.where(vm > 1e-12, fy / np.maximum(vm, 1e-12), cap)
            else:
                vals = full / max(t, 1e-12)
            vals = np.minimum(vals, cap)
        else:
            vals = full * t
        self.mesh.point_data[self.array] = vals.astype(np.float32)
        if "U" in self.base.point_data:
            self.mesh.points = (np.asarray(self.base.points) + (t * self.scale)   # 0: the undeformed shape
                                * np.asarray(self.base.point_data["U"]))
        if self.markers is not None:
            self.markers.update(self.scale, t)
        self.probe.set_fraction(t)


ICON = Path(__file__).with_name("asmfea_icon.ico")      # the add-in's icon (AsmFEA_Icon.png), 16-256 px
APP_ID = "AsmFEA.Viewer"


def _taskbar_identity() -> None:
    """Windows: give this process its own taskbar identity. Without one, the window's taskbar button belongs to
    python.exe and shows Python's icon; with one, it shows the window's own icon (AsmFEA's)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass                                             # cosmetic only: keep the Python icon


def _handle(pointer) -> int:
    """A window handle as VTK's Python wrapping returns it: an int, or a pointer string '_<hex>_p_void'."""
    if not pointer:
        return 0
    if isinstance(pointer, int):
        return pointer
    try:
        return int(str(pointer).lstrip("_").split("_")[0], 16)
    except ValueError:
        return 0


def _window_icon(pl) -> None:
    """Windows: the AsmFEA icon on the window's title bar and taskbar button. Set with WM_SETICON once the
    window exists (its first draw): VTK's own SetIcon before then did nothing (found live)."""
    if sys.platform != "win32" or not ICON.is_file():
        return
    state = {"done": False}

    def apply(*_args):
        if state["done"]:
            return
        try:
            import ctypes
            hwnd = _handle(pl.ren_win.GetGenericWindowId())
            if not hwnd:
                return                                   # off screen, or not made yet: try at the next draw
            state["done"] = True
            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
            IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x0080
            for which, px in ((1, user32.GetSystemMetrics(11)), (0, user32.GetSystemMetrics(49))):  # big, small
                icon = user32.LoadImageW(None, str(ICON), IMAGE_ICON, px, px, LR_LOADFROMFILE)
                if icon:
                    user32.SendMessageW(hwnd, WM_SETICON, which, icon)
        except (AttributeError, OSError, ValueError):
            state["done"] = True                         # cosmetic only: keep VTK's icon
    pl.renderer.AddObserver("StartEvent", apply)


def _clip_to(ex: float, ey: float, dx: float, dy: float, rect: tuple) -> tuple:
    """The point where the segment from (ex, ey), inside rect, towards (dx, dy) leaves rect."""
    x0, y0, x1, y1 = rect
    s = 1.0
    for d, lo, hi, e in ((dx - ex, x0, x1, ex), (dy - ey, y0, y1, ey)):
        if d > 1e-9:
            s = min(s, (hi - e) / d)
        elif d < -1e-9:
            s = min(s, (lo - e) / d)
    s = max(s, 0.0)
    return ex + s * (dx - ex), ey + s * (dy - ey)


def frame_rect(w: int, h: int) -> tuple:
    """The border's rectangle in window pixels (x0, y0, x1, y1): MARGIN_PX in from the top and sides, and above
    the band of controls along the bottom."""
    return MARGIN_PX, CONTROLS_BAND_PX, w - MARGIN_PX, h - MARGIN_PX


def _frame(pl) -> None:
    """The thin grey border, and a white mat over everything outside it, so the model is cut off at the border
    (the user's choice) and the controls below it sit on white. The mat is drawn over the model but under
    everything added after it (titles, key, controls, labels: VTK draws overlays in the order they were added),
    and both are re-placed in pixels whenever the window's size changes."""
    import pyvista as pv
    from vtkmodules.vtkRenderingCore import vtkActor2D, vtkPolyDataMapper2D
    mat = pv.PolyData(np.zeros((16, 3)), faces=np.hstack([[4, 4 * k, 4 * k + 1, 4 * k + 2, 4 * k + 3] for k in range(4)]))
    edge = pv.PolyData(np.zeros((4, 3)), lines=np.array([5, 0, 1, 2, 3, 0]))
    actors = []
    for poly, colour, width in ((mat, (1, 1, 1), 1.0), (edge, (0.62, 0.66, 0.70), 1.5)):
        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(poly)
        a = vtkActor2D()
        a.SetMapper(mapper)
        a.GetProperty().SetColor(*colour)
        a.GetProperty().SetLineWidth(width)
        a.PickableOff()
        pl.add_actor(a, reset_camera=False)
        actors.append(a)
    mat_pts, edge_pts = mat.GetPoints(), edge.GetPoints()
    state = {"size": None}

    def place(*_args):
        w, h = pl.renderer.GetRenderWindow().GetSize()
        if (w, h) == state["size"]:
            return
        state["size"] = (w, h)
        x0, y0, x1, y1 = frame_rect(w, h)
        quads = [(0, 0, w, y0), (0, y1, w, h), (0, y0, x0, y1), (x1, y0, w, y1)]       # below, above, left, right
        for k, (qa, qb, qc, qd) in enumerate(quads):
            for j, (px, py) in enumerate(((qa, qb), (qc, qb), (qc, qd), (qa, qd))):
                mat_pts.SetPoint(4 * k + j, px, py, 0.0)
        for j, (px, py) in enumerate(((x0, y0), (x1, y0), (x1, y1), (x0, y1))):
            edge_pts.SetPoint(j, px, py, 0.0)
        mat_pts.Modified()
        edge_pts.Modified()
    pl.renderer.AddObserver("StartEvent", place)
    place()


def _backing(tp) -> None:
    """A light backing behind text drawn over the model."""
    tp.SetBackgroundColor(1, 1, 1)
    tp.SetBackgroundOpacity(0.88)


def _stack_top_left(pl, actors: list) -> None:
    """Hang the text actors one below another from the frame's top left corner, in pixels, re-measured before
    every draw so they never overlap whatever the window's size."""
    for a in actors:
        a.GetTextProperty().SetVerticalJustificationToTop()

    def place(*_args):
        ren = pl.renderer
        y = ren.GetRenderWindow().GetSize()[1] - 2 * MARGIN_PX
        for a in actors:
            a.SetPosition(2 * MARGIN_PX, y)
            box = [0.0, 0.0, 0.0, 0.0]
            a.GetBoundingBox(ren, box)
            y -= (box[3] - box[2]) + 6
    pl.renderer.AddObserver("StartEvent", place)


def _text(pl, text: str, at: tuple, top: bool = False, size: int = 9):
    """Text placed at a fraction of the window (follows a resize); top: hangs down from that point."""
    actor = pl.add_text(text, position=at, viewport=True, font_size=size, color="black")
    if top:
        actor.GetTextProperty().SetVerticalJustificationToTop()
    return actor


def short_part(pid: str, all_ids=()) -> str:
    """A part's name as the report shows it: the body's name, without the assembly path; with its assembly
    when another part has the same short name (a mirrored sub-assembly's parts)."""
    short = pid.split("/body:")[-1].split("/")[-1]
    if sum(1 for o in all_ids if o.split("/body:")[-1].split("/")[-1] == short) > 1:
        return " / ".join(pid.replace("/body:", "/").split("/")[-2:])
    return short


def probe_values(mesh, cell: int, x, meta: dict, values=None) -> dict:
    """The fields at point x on surface facet ``cell``: interpolated linearly over the facet, as the colours
    are drawn (a quad as two triangles). FoS is yield / von Mises at the point, so it isn't capped as the
    colours are. values: the mesh to read the fields from (the full-load copy while the load animation runs;
    the same facets), else ``mesh``. {part, von_mises, displacement, fos, ids, weights}: ids and weights
    place the point on the facet's corners, so it can follow the facet as the shape deforms."""
    data = values if values is not None else mesh
    ids = np.asarray(mesh.get_cell(cell).point_ids, dtype=np.int64)
    P = np.asarray(mesh.points)[ids]
    x = np.asarray(x, float)
    tris = [(0, 1, 2)] if len(ids) == 3 else [(0, 1, 2), (0, 2, 3)]
    best = None
    for t in tris:
        a, b, c = P[list(t)]
        v0, v1, v2 = b - a, c - a, x - a
        d00, d01, d11, d20, d21 = v0 @ v0, v0 @ v1, v1 @ v1, v2 @ v0, v2 @ v1
        den = d00 * d11 - d01 * d01
        if abs(den) < 1e-30:
            continue
        v = (d11 * d20 - d01 * d21) / den
        w = (d00 * d21 - d01 * d20) / den
        bc = np.array([1 - v - w, v, w])
        if best is None or bc.min() > best[1].min():     # the triangle the point lies in (or nearest to)
            best = (list(t), bc)
    if best is None:
        w_all, corners = np.full(len(ids), 1.0 / len(ids)), list(range(len(ids)))
    else:
        corners, w_all = best[0], np.clip(best[1], 0, 1) / max(np.clip(best[1], 0, 1).sum(), 1e-30)

    def at(name):
        if name not in data.point_data:
            return None
        return float(np.asarray(data.point_data[name])[ids[corners]] @ w_all)
    vm, um, fy = at("von Mises MPa"), at("|U| mm"), at("yield MPa")
    parts = meta.get("parts") or []
    k = int(np.asarray(data.point_data["part"])[ids[corners[int(np.argmax(w_all))]]]) if "part" in data.point_data \
        else None
    return {"part": parts[k] if k is not None and k < len(parts) else None, "von_mises": vm, "displacement": um,
            "fos": (fy / vm if vm and vm > 1e-12 else float("inf")) if fy is not None else at("FoS on yield"),
            "ids": ids[corners], "weights": w_all}


class _Probe:
    """Click on the model: a dot at the point and a label with the part and the values there, until the next
    click. A click on another point replaces it; a click on the background clears it. A drag still rotates
    or pans: only a press and release in (nearly) the same place counts as a click."""

    def __init__(self, pl, surface_actor, mesh, meta: dict, field: str, base=None):
        from vtkmodules.vtkRenderingCore import vtkCellPicker
        self.pl, self.mesh, self.meta, self.field = pl, mesh, meta, field
        self.base = base                    # the full-load values (the load animation scales what is shown)
        self.fraction = 1.0                 # the load fraction shown
        self.values = None
        self.picker = vtkCellPicker()
        self.picker.SetTolerance(0.0005)
        self.picker.PickFromListOn()
        self.picker.AddPickList(surface_actor)
        self.actors: list = []
        self.place = None
        self.down = None
        pl.renderer.AddObserver("StartEvent", self._follow)

    def attach(self) -> None:
        # through pyvista's add_observer: with its mouse styles the window never passes a button release on
        # (pyvista issue 4976), so the release is observed on the style; observing the window itself left
        # every click unseen (found live)
        self.pl.iren.add_observer("LeftButtonPressEvent", self._press)
        self.pl.iren.add_observer("LeftButtonReleaseEvent", self._release)

    def _press(self, *_args) -> None:
        self.down = self.pl.iren.interactor.GetEventPosition()

    def _release(self, *_args) -> None:
        up, down, self.down = self.pl.iren.interactor.GetEventPosition(), self.down, None
        if down is not None and abs(up[0] - down[0]) <= CLICK_PX and abs(up[1] - down[1]) <= CLICK_PX:
            self.click(*up)

    def clear(self) -> None:
        for a in self.actors:
            self.pl.remove_actor(a, render=False)
        self.actors, self.place, self.values = [], None, None

    def click(self, x: int, y: int) -> None:
        """x, y: window pixels from the bottom left, as VTK reports them. Outside the border (the mat, the
        controls) nothing changes: the model there is hidden, and a button press is not a probe click."""
        x0, y0, x1, y1 = frame_rect(*self.pl.renderer.GetRenderWindow().GetSize())
        if not (x0 < x < x1 and y0 < y < y1):
            return
        self.clear()
        if self.picker.Pick(x, y, 0, self.pl.renderer) and self.picker.GetCellId() >= 0:
            p = np.asarray(self.picker.GetPickPosition(), float)
            self.values = probe_values(self.mesh, self.picker.GetCellId(), p, self.meta, values=self.base)
            self._show(p)
        self.pl.render()

    def set_fraction(self, t: float) -> None:
        """The load animation's frame: the label's values at fraction t of the full load."""
        self.fraction = float(t)
        if self.place:
            self.place[0].SetInput(self._label())

    def _label(self) -> str:
        v, t = self.values, self.fraction
        vm = None if v["von_mises"] is None else v["von_mises"] * t
        um = None if v["displacement"] is None else v["displacement"] * t
        fos = None if v["fos"] is None else (v["fos"] / t if t > 1e-9 else float("inf"))
        rows = {"vonmises": f"von Mises {vm:.3g} MPa" if vm is not None else None,
                "displacement": f"displacement {um:.3g} mm" if um is not None else None,
                "fos": None if fos is None else ("FoS > 1000" if fos > 1000 else f"FoS {fos:.3g}")}
        order = [self.field] + [f for f in ("vonmises", "displacement", "fos") if f != self.field]
        lines = ([short_part(v["part"], self.meta.get("parts") or [])] if v["part"] else []) + [rows[f] for f in order if rows[f]]
        if self.meta.get("run_mode") == "fast":
            lines.append("(fast run: approximate)")
        return "\n".join(lines)

    def _show(self, p) -> None:
        import pyvista as pv
        from vtkmodules.vtkRenderingCore import vtkActor2D, vtkPolyDataMapper2D, vtkTextActor
        dot = pv.PolyData(np.asarray(p, float)[None])     # moved by _follow, with the facet it lies on
        self.actors.append(self.pl.add_points(dot, color="black", point_size=14, render_points_as_spheres=True,
                                              pickable=False, reset_camera=False, render=False))
        self.actors.append(self.pl.add_points(dot, color="white", point_size=7, render_points_as_spheres=True,
                                              pickable=False, reset_camera=False, render=False))
        # the label: a framed box set off from the point with a leader line, both in window pixels, moved to
        # follow the point whenever the view is drawn (the point stays on the part as the view turns)
        text = vtkTextActor()
        text.SetInput(self._label())
        tp = text.GetTextProperty()
        tp.SetFontSize(14)
        tp.SetColor(0, 0, 0)
        tp.SetBackgroundColor(1, 1, 1)
        tp.SetBackgroundOpacity(0.92)
        tp.SetFrame(True)
        tp.SetFrameColor(0.25, 0.25, 0.25)
        tp.SetFrameWidth(1)
        tp.SetVerticalJustificationToBottom()
        leader = pv.PolyData(np.zeros((2, 3)), lines=np.array([2, 0, 1]))
        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(leader)
        leader = leader.GetPoints()                       # moved in place by _follow
        line = vtkActor2D()
        line.SetMapper(mapper)
        line.GetProperty().SetColor(0, 0, 0)
        line.GetProperty().SetLineWidth(1.5)
        for a in (line, text):
            a.PickableOff()
            self.pl.renderer.AddViewProp(a)
        self.actors += [line, text]
        self.place = (text, leader, dot.GetPoints())
        self._follow()

    def _follow(self, *_args) -> None:
        """Keep the label beside its point (called before every render while a probe is shown)."""
        if not self.place:
            return
        text, leader, dot = self.place
        # the point on its facet as the shape is now (the displacement window's animation moves it), just in
        # front of the surface so the dot isn't buried
        p = self.values["weights"] @ np.asarray(self.mesh.points)[self.values["ids"]]
        cam = np.asarray(self.pl.camera.position, float)
        q = p + (cam - p) * 1e-3
        dot.SetPoint(0, *q)
        dot.Modified()
        ren = self.pl.renderer
        ren.SetWorldPoint(*q, 1.0)
        ren.WorldToDisplay()
        dx, dy, _ = ren.GetDisplayPoint()
        w, h = ren.GetRenderWindow().GetSize()
        box = [0.0, 0.0, 0.0, 0.0]
        text.GetBoundingBox(ren, box)                     # xmin, xmax, ymin, ymax of the text, in pixels
        bw, bh = box[1] - box[0], box[3] - box[2]
        # up and to the right of the point; flipped where that would leave the frame
        ax = dx + LABEL_OFFSET_PX if dx + LABEL_OFFSET_PX + bw < w - 2 * MARGIN_PX else dx - LABEL_OFFSET_PX - bw
        ay = dy + LABEL_OFFSET_PX if dy + LABEL_OFFSET_PX + bh < h - 2 * MARGIN_PX else dy - LABEL_OFFSET_PX - bh
        text.SetPosition(ax, ay)
        corner = (ax if ax > dx else ax + bw, ay if ay > dy else ay + bh)
        leader.SetPoint(0, dx, dy, 0.0)
        leader.SetPoint(1, corner[0], corner[1], 0.0)
        leader.Modified()


if __name__ == "__main__":
    show(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
