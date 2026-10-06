"""Weld beads (DECISIONS D22): a weld bonds the touching faces within its bead size of
the weld path; the rest of the interface keeps its own connection (contact, a joint, or
none). A weld on both sides or all around bonds the whole interface.

The strip is carried on the connections as geometry the deck filters element faces by:
the weld connection gets ``region`` (bond inside it), the interface it was cut from gets
``exclude`` (leave it out). Both are ``{"segments": [[p, q], ...], "width": w}``.
"""
from __future__ import annotations

import numpy as np

from .. import defaults
from ..diagnostics import Diagnostic, make
from ..model import Model, Weld

STEP = 2.0   # mm between the path points used to match a weld to an interface


def segments(w: Weld) -> np.ndarray:
    """The welded segments of the path, (n, 2, 3); intermittent welds keep only the welded runs."""
    segs = []
    for poly in w.path:
        P = np.asarray(poly, float)
        for a, b in zip(P[:-1], P[1:]):
            if np.linalg.norm(b - a) > 1e-9:
                segs.append((a, b))
    it = w.intermittent
    if not it or it.length <= 0:
        return np.asarray(segs).reshape(-1, 2, 3)
    gap = it.gap if it.gap > 0 else max(it.pitch - it.length, 0.0)
    period = it.length + gap
    if gap <= 0:
        return np.asarray(segs).reshape(-1, 2, 3)
    out, s0 = [], 0.0          # s0: path length at the start of the current segment
    for a, b in segs:
        L = float(np.linalg.norm(b - a))
        # welded where (s mod period) < length; split the segment at those boundaries
        cuts = [0.0]
        k = np.floor(s0 / period)
        while True:
            for edge in (k * period + it.length, (k + 1) * period):
                t = edge - s0
                if 0 < t < L:
                    cuts.append(t)
            if (k + 1) * period >= s0 + L:
                break
            k += 1
        cuts = sorted(set(cuts + [L]))
        for t0, t1 in zip(cuts[:-1], cuts[1:]):
            mid = s0 + 0.5 * (t0 + t1)
            if mid % period < it.length:
                out.append((a + (b - a) * t0 / L, a + (b - a) * t1 / L))
        s0 += L
    return np.asarray(out).reshape(-1, 2, 3)


def distance_to_segments(X: np.ndarray, segs: np.ndarray) -> np.ndarray:
    """Distance from each point (n, 3) to the nearest segment (m, 2, 3)."""
    X = np.asarray(X, float).reshape(-1, 3)
    if len(segs) == 0:
        return np.full(len(X), np.inf)
    A, B = segs[:, 0], segs[:, 1]
    AB = B - A
    L2 = np.maximum((AB * AB).sum(axis=1), 1e-300)
    t = np.clip(((X[:, None, :] - A[None]) * AB[None]).sum(axis=2) / L2[None], 0.0, 1.0)
    Q = A[None] + t[..., None] * AB[None]
    return np.linalg.norm(X[:, None, :] - Q, axis=2).min(axis=1)


def _densify(segs: np.ndarray) -> np.ndarray:
    pts = []
    for a, b in segs:
        n = max(int(np.ceil(np.linalg.norm(b - a) / STEP)), 1)
        pts += [a + (b - a) * i / n for i in range(n + 1)]
    return np.asarray(pts).reshape(-1, 3)


def _reaches(c: dict, pts: np.ndarray, size: float) -> float:
    """Share of the path points within reach (bead size) of a planar interface's area."""
    S = np.asarray(c.get("samples") or [], float)
    if len(S) == 0 or c.get("normal") is None:
        return 0.0
    n = np.asarray(c["normal"], float)
    n /= np.linalg.norm(n)
    reach = size + defaults.GAP_TOL
    plane = np.abs((pts - S.mean(axis=0)) @ n) <= reach
    lo, hi = S.min(axis=0) - reach, S.max(axis=0) + reach
    inside = np.all((pts >= lo) & (pts <= hi), axis=1)
    return float(np.mean(plane & inside))


def _strip_samples(segs: np.ndarray, normal, width: float) -> list[list[float]]:
    """Points spanning the strip (path points offset across it), for the mechanism check."""
    n = np.asarray(normal, float)
    out = []
    for a, b in segs:
        t = (b - a) / np.linalg.norm(b - a)
        side = np.cross(n, t)
        side = side / max(np.linalg.norm(side), 1e-300) * 0.5 * width
        for p in (a, b):
            out += [(p + side).tolist(), (p - side).tolist()]
    return out


def apply_welds(model: Model, conns: list[dict], removed: list[str]) -> list[Diagnostic]:
    """Add a bonded strip for every weld bead; returns diagnostics."""
    diags: list[Diagnostic] = []
    for w in model.welds:
        if w.partial:
            diags.append(make("FEATURE_NOT_IMPLEMENTED", where={"parts": list(w.parts)},
                              feature=f"a weld bead with a from-to length ({w.id})"))
            continue
        segs = segments(w)
        pts = _densify(segs)
        whole = w.side in ("both_sides", "all_around")
        hits = []
        for c in conns:
            if c.get("source") == "weld" or len(c.get("sides", [])) != 2 or any(p in removed for p in c["parts"]):
                continue
            if not set(w.parts) & set(c["parts"][:2]):
                continue
            if len(w.parts) >= 2 and not set(c["parts"][:2]) <= set(w.parts):
                continue
            share = _reaches(c, pts, w.size) if len(pts) else 0.0
            if share >= 0.5:
                hits.append(c)
        if not hits:
            diags.append(make("WELD_NO_INTERFACE", where={"parts": list(w.parts)}, weld=w.id,
                              parts=" and ".join(w.parts), size=w.size,
                              evidence={"weld": w.id, "size": w.size, "path_points": len(pts)}))
            continue
        for c in hits:
            ev = c.setdefault("evidence", {})
            ev.setdefault("welds", []).append(w.id)
            if c["type"] == "tie" and "exclude" not in c:
                continue        # already bonded over the whole interface
            rule = (f"weld bead {w.id} ({w.size:g} mm, {w.side.replace('_', ' ')}): "
                    + ("the whole touching interface bonded" if whole
                       else f"touching faces within {w.size:g} mm of the weld path bonded"))
            if whole:
                c.update(type="tie", source="weld")
                c.pop("exclude", None)
                c["evidence"]["rule"] = rule
                continue
            region = {"segments": segs.tolist(), "width": float(w.size)}
            c.setdefault("exclude", []).append(region)
            weld_c = {k: v for k, v in c.items() if k not in ("exclude", "override", "friction", "params")}
            weld_c.update(id=f"{c['id']}~{w.id}", type="tie", source="weld", region=region,
                          samples=_strip_samples(segs, c["normal"], w.size),
                          evidence={"rule": rule, "weld": w.id, "interface": c["id"]})
            conns.append(weld_c)
    return diags
