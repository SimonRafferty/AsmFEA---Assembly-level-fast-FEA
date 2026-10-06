"""The hybrid solver: a stand-in for CalculiX on linear static studies that keeps memory to the largest part.

``run(workdir, job)`` reads the deck AsmFEA wrote (``job``.inp and deck_info.json), solves it part by part
(hybrid.solve) and writes ``job``.frd and ``job``.dat in CalculiX's layout, so post-processing, plots,
the 3-D viewer and the report read its results as they read CalculiX's. A deck it cannot solve raises
deckread.Unsupported before anything is written."""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from . import elements as el
from .deckread import Deck, Unsupported, read  # noqa: F401
from .solve import PartData, Settings, solve


def build_parts(deck: Deck, info: dict) -> tuple[list[PartData], list]:
    """The deck's parts (one per instance, with its internal ties) and the ties between parts."""
    from .solve import _material_of
    mat = _material_of(deck)
    internal = {}
    for it in info.get("internal_ties", []):
        internal.setdefault(it["part"], set()).add(it["name"])
    by_name = {t[0]: t for t in deck.ties}
    used = set()
    parts = []
    for inst in info["instances"]:
        n0, nn = int(inst["node0"]), int(inst["n_nodes"])
        e0, ne = int(inst["elem0"]), int(inst["n_elements"])
        nodes = np.arange(n0 + 1, n0 + nn + 1, dtype=np.int64)
        blocks = []
        for et, (ids, conn) in deck.blocks.items():
            sel = (ids > e0) & (ids <= e0 + ne)
            if not sel.any():
                continue
            mats = np.array([mat.get(int(e), "") for e in ids[sel]])
            for mname in sorted(set(mats)):
                if not mname:
                    raise Unsupported(f"elements of {inst['part']} have no material section")
                s2 = mats == mname
                blocks.append((et, ids[sel][s2], conn[sel][s2], mname))
        ties = [by_name[n] for n in sorted(internal.get(inst["part"], ())) if n in by_name]
        used.update(t[0] for t in ties)
        parts.append(PartData(inst["part"], nodes, blocks, ties))
    external = [t for t in deck.ties if t[0] not in used]
    return parts, external


def _table(deck: Deck):
    """(sorted node ids, their coordinates (n, 3), id -> row lookup array)."""
    ids = np.array(sorted(deck.nodes), dtype=np.int64)
    xyz = np.array([deck.nodes[int(n)] for n in ids], dtype=float)
    row = np.full(int(ids.max()) + 1, -1, dtype=np.int64)
    row[ids] = np.arange(len(ids))
    return ids, xyz, row


def nodal_stresses(deck: Deck, parts: list[PartData], U: np.ndarray, nsteps: int) -> np.ndarray:
    """(top, 6, n_steps) by node id: each element's stresses at its nodes, averaged over the elements of the
    same part sharing the node (CalculiX averages over all elements at a node; tied parts share no nodes)."""
    ids, xyz, row = _table(deck)
    Ua = U[ids]
    S = np.zeros((len(ids), 6, nsteps))
    cnt = np.zeros(len(ids))
    for part in parts:
        for etype, eids, conn, mname in part.blocks:
            m = deck.materials[mname]
            r = row[conn]
            X = xyz[r]
            for a in range(0, len(conn), 20000):
                sl = slice(a, a + 20000)
                for k in range(nsteps):
                    se = el.nodal_stress(etype, X[sl], Ua[r[sl], :, k], m["E"], m["nu"])
                    np.add.at(S[:, :, k], r[sl].ravel(), se.reshape(-1, 6))
            np.add.at(cnt, r.ravel(), 1.0)
    used = np.nonzero(cnt)[0]
    S[used] /= cnt[used, None, None]
    out = np.zeros((len(U), 6, nsteps))
    out[ids] = S
    return out


def _block(ids: np.ndarray, vals: np.ndarray) -> str:
    """CalculiX's fixed-width node records: ' -1', id in 10 columns, values in 12 (1.23456E+00)."""
    fmt = " -1%10d" + "%12.5E" * vals.shape[1]
    return "\n".join(fmt % rec for rec in zip(ids.tolist(), *vals.T.tolist()))


def write_frd(path: Path, deck: Deck, U: np.ndarray, S: np.ndarray, nsteps: int) -> None:
    ids, xyz, row = _table(deck)
    out = ["    1C", "    1Uasmfea hybrid solver", "    1UPGM               AsmFEA hybrid"]
    out.append(f"    2C{len(ids):30d}{'':37s}1")
    out.append(_block(ids, xyz))
    out.append(" -3")
    for k in range(nsteps):
        for name, comps, data in (("DISP", 4, U), ("STRESS", 6, S)):
            out.append(f"    1PSTEP{k + 1:25d}{1:12d}{k + 1:12d}")
            out.append(f"  100CL  101 1.000000000{len(ids):12d}{'':21s}0    1{k + 1:12d}")
            out.append(f" -4  {name:8s}{comps:4d}    1")
            labels = ["D1", "D2", "D3", "ALL"] if name == "DISP" else ["SXX", "SYY", "SZZ", "SXY", "SYZ", "SZX"]
            out += [f" -5  {lab:8s}    1    2    {j + 1}    0" for j, lab in enumerate(labels)]
            width = 3 if name == "DISP" else 6
            out.append(_block(ids, data[ids, :width, k]))
            out.append(" -3")
    out.append(" 9999")
    Path(path).write_text("\n".join(out) + "\n")


def write_dat(path: Path, deck: Deck, RF: np.ndarray, nsteps: int) -> None:
    out = []
    for k, step in enumerate(deck.steps):
        out += ["", f"                        S T E P{k + 1:8d}", "", "", "                                INCREMENT     1", ""]
        for nset, totals in step.prints:
            nodes = deck.node_ids(nset)
            if totals:
                tot = RF[np.asarray(nodes, dtype=np.int64), :, k].sum(axis=0)
                out += [f" total force (fx,fy,fz) for set {nset.upper()} and time  0.1000000E+01", "",
                        "      " + " ".join(f"{x:13.6E}" for x in tot), ""]
            else:
                out += [f" forces (fx,fy,fz) for set {nset.upper()} and time  0.1000000E+01", ""]
                out += [f" {int(n):9d} " + " ".join(f"{x:13.6E}" for x in RF[int(n), :, k]) for n in nodes]
                out.append("")
    Path(path).write_text("\n".join(out) + "\n")


def run(workdir: Path, job: str = "study", settings: Settings | None = None, log=print) -> dict:
    """Solve ``workdir``/``job``.inp; returns timings. Raises Unsupported for a deck it cannot solve."""
    workdir = Path(workdir)
    t0 = time.perf_counter()
    deck = read(workdir / f"{job}.inp")
    info = json.loads((workdir / "deck_info.json").read_text())
    parts, external = build_parts(deck, info)
    nsteps = len(deck.steps)
    U, RF, timings = solve(deck, parts, external, settings or Settings(), log=log)
    t1 = time.perf_counter()
    S = nodal_stresses(deck, parts, U, nsteps)
    write_frd(workdir / f"{job}.frd", deck, U, S, nsteps)
    write_dat(workdir / f"{job}.dat", deck, RF, nsteps)
    timings["output_s"] = time.perf_counter() - t1
    timings["read_s"] = timings.get("setup_s", 0.0)
    timings["wall_s"] = time.perf_counter() - t0
    return timings
