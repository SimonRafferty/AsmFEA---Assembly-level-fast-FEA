"""Per-part meshing: each part in its own worker process, through the backend fallback
chain (mesh/backends), tagged against the CAD faces by geometry (mesh/tagging), cached.

A part mesh is stored in the part frame as an .npz in <study>/mesh/:
  nodes      (N, 3) float, node i has local id i + 1
  etypes     CalculiX element type per block ("C3D10", "C3D15", "C3D20R")
  conn_<k>   (n_k, nn) int, local node ids, CalculiX node order
  face_elem  (M, 3) int rows (face index, element row, CalculiX face number 1..6)
  ties       (K, 2) int rows (slave face index, master face index): internal ties between
             regions of one part; region cut faces are numbered from the face count up
Element rows count through the blocks in order, starting at 0. Face indices are the
geometry stage's numbering (geometry.py).
"""
from __future__ import annotations

import hashlib
import json
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import psutil

from .. import defaults

MESH_FORMAT_VERSION = "asmfea-mesh-3"   # bump when the cached file layout or tagging changes


@dataclass
class PartMesh:
    nodes: np.ndarray
    blocks: list[tuple[str, np.ndarray]]
    face_elem: np.ndarray            # (M, 3) face index, element row, face number
    # (K, 2) internal ties between regions of one part (slave face index, master face index);
    # region cut faces have indices from the part's face count up
    ties: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), dtype=np.int64))
    source: Optional[Path] = field(default=None, compare=False, repr=False)   # the cached file it came from

    @staticmethod
    def load(path: Path) -> "PartMesh":
        z = np.load(path, allow_pickle=False)
        etypes = [str(s) for s in z["etypes"]]
        blocks = [(t, z[f"conn_{i}"]) for i, t in enumerate(etypes)]
        ties = z["ties"] if "ties" in z.files else np.zeros((0, 2), dtype=np.int64)
        return PartMesh(z["nodes"], blocks, z["face_elem"], ties, source=Path(path))

    def save(self, path: Path) -> None:
        arrays = {f"conn_{i}": c for i, (_, c) in enumerate(self.blocks)}
        tmp = path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, nodes=self.nodes, etypes=np.array([t for t, _ in self.blocks]),
                            face_elem=self.face_elem, ties=self.ties, **arrays)
        tmp.replace(path)

    @property
    def n_elements(self) -> int:
        return sum(len(c) for _, c in self.blocks)


def element_size(part_bbox_dims: list[float], study_size: Optional[float], fidelity: str) -> float:
    if study_size:
        base = study_size
    else:
        base = defaults.SIZE_FRACTION_OF_MIN_DIM * min(d for d in part_bbox_dims if d > 0)
        base = min(max(base, defaults.SIZE_MIN), defaults.SIZE_MAX)
    return base * defaults.FIDELITY_FACTOR[fidelity]


def cache_key(geometry_hash: str, solid_index: int, size: float, chain: list[tuple[str, str]],
              route: Optional[dict] = None) -> str:
    """Geometry, settings (size and the swept route's data), and every backend (name,
    version) in the chain (Change 001 s7)."""
    s = json.dumps({"g": geometry_hash, "i": solid_index, "size": round(size, 6), "chain": chain,
                    "route": route or {}, "fmt": MESH_FORMAT_VERSION}, sort_keys=True, default=str)
    return hashlib.sha1(s.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Worker (runs in a child process)

def mesh_part_job(task: dict) -> dict:
    """Mesh one unique body, tag its faces, save the cached PartMesh.

    route "tet": the whole body through the tet chain. "skin": the whole body swept through
    its thickness, tets as the fallback. "member": the body is split at its clean-span ends;
    spans are section-swept and the end regions skin-swept, each with tets as the fallback,
    and every cut plane becomes an internal tie (M0: span plus tie, end side slave).
    """
    from ..config import Config
    from ..geometry import part_faces, read_step
    from .backends import CHAINS, registry

    t0 = time.perf_counter()
    import os
    if os.environ.get("ASMFEA_TEST_REFUSE_FULL_SIZE") == task["part"] and not task.get("size_reduced"):
        # tests only: this part "will not mesh" at its normal size, so the smaller-size retry can be checked
        # on clean geometry (the real failures came from a converted model's faces, which cannot be shipped)
        return {"ok": False, "attempts": [{"mesher": "test", "error": "refused at the normal size (test switch)"}]}
    work = Path(task["workdir"])
    work.mkdir(parents=True, exist_ok=True)
    progress = work / "progress.txt"

    def on_line(line: str):
        if line.strip():
            try:
                progress.write_text(line[:200])
            except OSError:
                pass

    cfg = Config(**task["config"])
    backends = registry(cfg)
    attempts: list[dict] = []
    try:
        solid = read_step(Path(task["body_step"]))[0]
        faces = part_faces(solid)
        areas = task["face_areas"]
        if len(faces) != len(areas) or any(abs(f.area - a) > 1e-6 * max(a, 1.0) for f, a in zip(faces, areas)):
            raise RuntimeError("face numbering of the body file differs from the geometry stage")
    except Exception as e:
        return {"ok": False, "attempts": [{"mesher": "geometry", "error": repr(e)[:300]}],
                "seconds": time.perf_counter() - t0}
    nf = len(faces)
    route = task.get("route", "tet")
    rx = task.get("route_extra", {})
    factor = float(task.get("factor", 1.0))

    # regions: (name, geometry file, chain, extra for native_sweep, [(cut face index, FaceInfo)], is span)
    regions: list[tuple] = []
    ties: list[tuple[int, int]] = []
    if route == "member":
        regions, ties = _member_regions(solid, nf, rx, factor, work, attempts)
        if not regions:
            route = "tet"
    if route == "skin":
        regions = [("whole", Path(task["body_step"]), CHAINS["swept"],
                    {"mode": "skin", "thickness": rx.get("thickness"), "factor": factor}, [], False)]
    elif route == "tet":
        regions = [("whole", Path(task["body_step"]), CHAINS["tet"], {}, [], False)]

    X_all, blocks_all, fe_all, used, infos = [], [], [], [], []
    stats = {"facets": 0, "unclassified": 0, "ambiguous": 0}
    quality = {"inverted": 0, "min_tet_quality": 1.0}
    n0 = row0 = 0
    for name, geom, chain, extra, cut_faces, _ in regions:
        res = _mesh_region(task, backends, name, geom, chain, extra, faces + [f for _, f in cut_faces],
                           work, attempts, on_line)
        if res is None:
            return {"ok": False, "attempts": attempts, "seconds": time.perf_counter() - t0, "failed_region": name}
        r, X, blocks, fe, st = res
        X_all.append(X)
        blocks_all += [(t, c + n0) for t, c in blocks]
        if len(fe):
            fe = fe.copy()
            fe[:, 1] += row0
            fe_all.append(fe)
        n0 += len(X)
        row0 += sum(len(c) for _, c in blocks)
        used.append(r.backend)
        for k in ("facets", "unclassified", "ambiguous"):
            stats[k] += st.get(k, 0)
        quality["inverted"] += r.quality.get("inverted", 0)
        quality["min_tet_quality"] = min(quality["min_tet_quality"], r.quality.get("min_tet_quality", 1.0))
        infos.append({"region": name, "mesher": r.backend, "nodes": r.nodes, "elements": r.elements,
                      **{k: v for k, v in r.quality.items() if k in ("mode", "thickness", "wall", "layers", "algorithm")}})
    face_elem = np.vstack(fe_all) if fe_all else np.zeros((0, 3), dtype=np.int64)
    stats["faces_with_facets"] = len({int(i) for i in face_elem[:, 0] if i < nf})
    stats["faces"] = nf
    try:
        PartMesh(np.vstack(X_all), blocks_all, face_elem,
                 np.asarray(ties, dtype=np.int64).reshape(-1, 2)).save(Path(task["out"]))
    except Exception as e:
        attempts.append({"mesher": "save", "error": repr(e)[:300]})
        return {"ok": False, "attempts": attempts, "seconds": time.perf_counter() - t0}
    return {"ok": True, "mesher": "+".join(dict.fromkeys(used)), "attempts": attempts, "nodes": n0,
            "elements": row0, "quality": quality, "tagging": stats, "regions": infos,
            "internal_ties": len(ties), "seconds": time.perf_counter() - t0}


def _member_regions(solid, nf, rx, factor, work, attempts):
    """Split a member at its clean-span ends (straight or arc path). Returns (regions, ties);
    ([], []) if it cannot be split, with the reason in attempts."""
    from ..geometry import MemberPath, face_info, part_faces, split_member, write_step
    from .backends import CHAINS
    path = MemberPath(rx)
    spans = [(float(a), float(b)) for a, b in rx["spans"]]
    cuts = sorted({s for sp in spans for s in sp})
    try:
        pieces = split_member(solid, path, cuts)
    except Exception as e:
        attempts.append({"mesher": "split", "error": repr(e)[:300]})
        return [], []
    regions = []
    for k, (piece, lo, hi) in enumerate(pieces):
        step = work / f"region{k}.step"
        write_step(step, [piece])
        in_span = bool(np.isfinite(lo) and np.isfinite(hi)
                       and any(a - 1e-6 <= 0.5 * (lo + hi) <= b + 1e-6 for a, b in spans))
        # the cut faces of this piece: index nf + 2c for the face above cut c (the piece
        # below the plane), nf + 2c + 1 for the face below it (the piece above)
        cut_faces = []
        for f in part_faces(piece):
            if f.surface != "plane":
                continue
            s = path.s_of(f.centroid)
            for ci, c in enumerate(cuts):
                if abs(s - c) < 1e-4 and abs(abs(float(np.dot(f.normal, path.tangent(c)))) - 1) < 1e-9:
                    idx = nf + 2 * ci + (0 if np.isclose(c, hi) else 1)
                    cut_faces.append((idx, face_info(f.shape, idx)))
        if in_span:
            extra = {"mode": "section", **{k_: v for k_, v in rx.items() if k_ != "spans"},
                     "s0": lo, "s1": hi, "factor": factor}
        else:
            extra = {"mode": "skin", "thickness": rx.get("wall"), "factor": factor}
        regions.append((f"{'span' if in_span else 'end'}{k}", step, CHAINS["swept"], extra, cut_faces, in_span))
    ties = []
    for ci in range(len(cuts)):
        below = next((r for r in regions if any(i == nf + 2 * ci for i, _ in r[4])), None)
        above = next((r for r in regions if any(i == nf + 2 * ci + 1 for i, _ in r[4])), None)
        if below is None or above is None:
            attempts.append({"mesher": "split", "error": f"cut {ci} at s={cuts[ci]:.3f} has no face on one side"})
            return [], []
        # the end side is the slave, the span side the master (M0)
        ties.append((nf + 2 * ci + 1, nf + 2 * ci) if below[5] else (nf + 2 * ci, nf + 2 * ci + 1))
    return regions, ties


def _mesh_region(task, backends, name, geom, chain, extra, tag_list, work, attempts, on_line):
    """Run one region through its chain and tag it. A swept mesh whose boundary does not land
    on the CAD faces is refused and the chain moves on. Returns (result, X, blocks,
    face_elem, stats) or None when every mesher failed."""
    from .backends import MeshRequest, load_neutral
    from .tagging import tag_faces
    for mesher in chain:
        b = backends[mesher]
        if not b.available():
            attempts.append({"mesher": mesher, "region": name, "error": "backend unavailable", "skipped": True})
            continue
        if task.get("gmsh_optimize"):
            extra = {**extra, "gmsh_optimize": True}
        req = MeshRequest(part=task["part"], geometry_file=Path(geom), element=task["element"],
                          size_max=task["size"], out_dir=work / f"{name}_{mesher}", timeout_s=task["timeout_s"],
                          threads=1, extra=extra)
        try:
            r = b.mesh(req, on_line)
        except Exception as e:
            attempts.append({"mesher": mesher, "region": name, "error": repr(e)[:300],
                             "trace": traceback.format_exc()[-1500:]})
            continue
        if not r.ok:
            attempts.append({"mesher": mesher, "region": name, "error": (r.error or "failed")[:500],
                             "timed_out": r.timed_out, "log": str(r.log_path) if r.log_path else None})
            continue
        try:
            X, blocks = load_neutral(r.mesh_file)
            on_line(f"tagging {name}")
            fe, st = tag_faces(np.vstack([np.zeros((1, 3)), X]), blocks, tag_list, r.surface_tolerance)
        except Exception as e:
            attempts.append({"mesher": mesher, "region": name, "error": f"post-processing failed: {e!r}"[:300],
                             "trace": traceback.format_exc()[-1500:]})
            continue
        if mesher == "native_sweep" and (st["unclassified"] or st.get("ambiguous")):
            attempts.append({"mesher": mesher, "region": name,
                             "error": f"swept boundary does not match the CAD faces ({st['unclassified']} "
                                      f"unclassified, {st.get('ambiguous', 0)} ambiguous facets)"})
            continue
        return r, X, blocks, fe, st
    return None


# --------------------------------------------------------------------------- #

def kill_children() -> None:
    for c in psutil.Process().children(recursive=True):
        try:
            c.kill()
        except psutil.Error:
            pass


def _report_path(task: dict) -> Path:
    return Path(task["out"]).with_suffix(".report.json")


def _save_report(task: dict, r: dict) -> None:
    """What the mesher did (regions, refused sweeps), kept beside the cached mesh: without it a
    cache hit lost the MESH_SWEEP_FALLBACK warning (found live)."""
    keep = {k: v for k, v in r.items() if k in ("mesher", "attempts", "regions", "nodes", "elements", "quality",
                                                 "tagging", "internal_ties", "seconds", "size_reduced")}
    for a in keep.get("attempts", []):
        a.pop("trace", None)
    try:
        _report_path(task).write_text(json.dumps(keep, default=str))
    except OSError:
        pass


def _report(task: dict) -> dict:
    try:
        return json.loads(_report_path(task).read_text())
    except (OSError, ValueError):
        return {}


def mesh_parts(tasks: list[dict], workers: int, on_done: Callable[[dict, dict], None],
               on_tick: Callable[[dict[str, str]], bool] = lambda p: False, force: bool = False) -> dict[str, dict]:
    """Run mesh_part_job over tasks in a process pool; cached outputs are skipped (all are remeshed
    when force).

    on_tick(latest progress line per running part) is called about once a second and
    returns True to cancel (all mesher processes are then killed).
    """
    results: dict[str, dict] = {}
    todo = []
    for t in tasks:
        if Path(t["out"]).exists() and not force:
            results[t["key"]] = {**_report(t), "ok": True, "cached": True}
            on_done(t, results[t["key"]])
        else:
            todo.append(t)
    if not todo:
        return results
    pool = ProcessPoolExecutor(max_workers=max(1, min(workers, len(todo))))
    try:
        futs = {pool.submit(mesh_part_job, t): t for t in todo}
        pending = set(futs)
        while pending:
            done, pending = wait(pending, timeout=defaults.HEARTBEAT_S, return_when=FIRST_COMPLETED)
            for fut in done:
                t = futs[fut]
                try:
                    r = fut.result()
                except Exception as e:  # worker process died
                    r = {"ok": False, "attempts": [{"mesher": "process", "error": repr(e)[:300]}]}
                if not r.get("ok") and t.get("fallback"):
                    # a fast run's part that will not mesh at the fast size: meshed at the coarse size instead
                    t2 = {**t, **t["fallback"], "fallback": None, "fell_back": True}
                    fut2 = pool.submit(mesh_part_job, t2)
                    futs[fut2] = t2
                    pending.add(fut2)
                    continue
                ladder = t.get("size_ladder", list(defaults.MESH_RETRY_SIZE_FACTORS))
                if not r.get("ok") and ladder:
                    # no mesher could mesh it (a surface mesh crossing itself on small features, elements still
                    # inverted): again with smaller elements, which follow the small features
                    size0 = t.get("size0", t["size"])
                    t2 = {**t, "size": size0 * ladder[0], "size0": size0, "size_reduced": ladder[0],
                          "size_ladder": ladder[1:], "earlier_attempts": t.get("earlier_attempts", []) + r.get("attempts", [])}
                    fut2 = pool.submit(mesh_part_job, t2)
                    futs[fut2] = t2
                    pending.add(fut2)
                    continue
                if not r.get("ok") and t.get("earlier_attempts"):
                    r["attempts"] = t["earlier_attempts"] + r.get("attempts", [])
                if r.get("ok"):
                    if t.get("fell_back"):
                        r["fell_back"] = True
                    if t.get("size_reduced"):
                        r["size_reduced"] = t["size_reduced"]
                    _save_report(t, r)
                results[t["key"]] = r
                on_done(t, r)
            lines = {}
            for fut in pending:
                p = Path(futs[fut]["workdir"]) / "progress.txt"
                try:
                    t = futs[fut]
                    lines[f"{t['part']} ({t.get('level', '')})"] = p.read_text()
                except OSError:
                    pass
            if on_tick(lines):
                kill_children()
                pool.shutdown(wait=False, cancel_futures=True)
                raise MeshCancelled()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


class MeshCancelled(Exception):
    pass
