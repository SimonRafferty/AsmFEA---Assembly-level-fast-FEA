"""The stages: resolve -> mesh -> solve -> post. Each reads and writes only the study folder.

Stages talk to the job through a JobState (progress, results, diagnostics) and raise
diagnostics.Blocked when they cannot continue.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np

from . import defaults, perf
from .config import load_config
from .deck import Instance, write_deck
from .diagnostics import Blocked, Diagnostic, make, raise_if_blocking
from .geometry import resolve_geometry
from .jobs import JobState, read_json, write_json_atomic
from .mesh import MeshCancelled, PartMesh, cache_key, element_size, mesh_parts
from .model import Model, load_model
from .post import buckling, checks, convergence, summarise
from .resolve import resolve
from .solve import estimate_memory_gb, memory_check, run_ccx
from .runmode import FAST, FULL, latest_solve_file, mode_of, model_key, results_dir
from .stagecache import Recorder, StageCache, file_digest, force_requested, key as stage_key, note_reuse, replay
from .validate import load_checked

SCHEMA_VERSION = "1.0"


def asmfea_version() -> str:
    try:
        return version("asmfea")
    except PackageNotFoundError:
        return "dev"


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, doc)


def _exceptions_doc(diags: list[Diagnostic]) -> dict:
    return {"schema_version": SCHEMA_VERSION, "exceptions": [d.model_dump() for d in diags]}


# --------------------------------------------------------------------------- #

STUDY_INCOMPLETE = {"LOAD_CASE_UNDEFINED", "NO_FIXTURES"}


def stage_resolve(study: Path, js: JobState) -> None:
    # a view chosen after the last solve (api.replot) gives way to the view this export recorded
    (study / "view.json").unlink(missing_ok=True)
    js.activity("resolve: reading model.json and study.json")
    model, diags = load_checked(study)
    # Missing loads or fixtures stop a run, but not the connection analysis: connections do not depend on them
    # (found live: a study with only a fixture and a hinge never listed its connections). They are reported,
    # and raised once the connections are written.
    if model is None or any(d.severity == "blocking" and d.code not in STUDY_INCOMPLETE for d in diags):
        _write(study / "exceptions.json", _exceptions_doc(diags))
        raise_if_blocking(diags)
    n = len(model.parts)
    cache = StageCache(study)
    k = resolve_key(study, model)
    rec = None if force_requested(js) else cache.get("resolve", k)
    if rec:
        # geometry and connections unchanged (only load magnitudes, results settings or nothing changed):
        # the checks of loads and fixtures above ran again; STEP loading and the interface search did not
        gr = [Diagnostic(**d) for d in rec["data"]["gr_diags"]]
        diags += gr
        _write(study / "exceptions.json", _exceptions_doc(diags))
        replay(js, {"results": rec["data"]["results"], "verify": rec["data"]["verify"]})
        note_reuse(js, "resolve", f"geometry and connections reused (inputs unchanged since {rec['when']})")
        js.add_diags([d for d in diags if d.severity != "blocking"])
        raise_if_blocking(diags)
        return
    js.activity(f"resolve: loading {n} parts from STEP", 0, n)
    steps = {study / p.step for p in model.parts}
    with perf.span("step_load", parts=n, files=len(steps), bytes=sum(s.stat().st_size for s in steps if s.exists())):
        geoms, face_map, gdiags = resolve_geometry(
            study, model, on_part=lambda k: js.activity(f"resolve: loading {n} parts from STEP", k, n))
    diags += gdiags
    if any(d.severity == "blocking" for d in gdiags):
        _write(study / "exceptions.json", _exceptions_doc(diags))
        raise_if_blocking(diags)
    js.activity(f"resolve: checking {n * (n - 1) // 2} part pairs for interfaces, then preflight",
                0, n * (n - 1) // 2)
    with perf.span("resolver", pairs=n * (n - 1) // 2) as pinfo:
        conns, removed, rdiags = resolve(model, geoms, face_map)
        pinfo["interfaces"] = len(conns)
    diags += rdiags
    _write(study / "geometry.json", {"schema_version": SCHEMA_VERSION,
                                     "parts": {k: g.to_json() for k, g in geoms.items()},
                                     "face_map": {k: list(v) for k, v in face_map.items()}})
    _write(study / "connections.json", {"schema_version": SCHEMA_VERSION, "connections": conns,
                                        "removed_parts": removed})
    _write(study / "exceptions.json", _exceptions_doc(diags))
    counts = defaultdict(int)
    for c in conns:
        counts[c["type"]] += 1
    line = (f"resolve: {len(geoms)} parts, {len(conns)} connections ("
            + ", ".join(f"{v} {t}" for t, v in sorted(counts.items())) + f"), {len(removed)} parts removed, "
            f"{len(diags)} exceptions")
    check = f"{len(face_map)}/{len(model.face_refs)} referenced faces matched to STEP geometry"
    js.result(line)
    js.verify(check)
    js.add_diags([d for d in diags if d.severity != "blocking"])
    raise_if_blocking(diags)
    outputs = [study / "geometry.json", study / "connections.json"]
    outputs += sorted({study / g.body_step for g in geoms.values() if g.body_step})
    cache.put("resolve", k, outputs, {"gr_diags": [d.model_dump() for d in gdiags + rdiags],
                                      "results": [line], "verify": [check]})


def _model_doc(model: Model) -> dict:
    """The merged model as plain data, without what only names the export (its time) or the plot view."""
    d = model.model_dump(by_alias=True, mode="json")
    d["source"] = {k: v for k, v in (d.get("source") or {}).items() if k not in ("timestamp", "view")}
    return d


def resolve_key(study: Path, model: Model) -> str:
    """What geometry, interfaces and connections depend on: the model with each load reduced to its
    faces (a load's size and direction cannot change a connection) and the study's bond tolerance.
    Each part's geometry enters through its geometry_hash (in the model, as for the mesh cache): the
    add-in's fingerprint of the body (volume, centroid, box, every face's type and area). The STEP files'
    bytes are not used: every export from SolidWorks rewrites them with new dates inside, header and data,
    so a byte hash made resolve run again after every export (found live on M8)."""
    d = _model_doc(model)
    d["loads"] = [{k: ld.get(k) for k in ("id", "load_case", "type", "faces")} for ld in d.get("loads", [])]
    d["study"] = {"bond_tolerance": (d.get("study") or {}).get("bond_tolerance")}
    # a missing file is part of the key, not an error here: resolve then reports STEP_MISSING as usual
    present = {s: (study / s).is_file() for s in sorted({p.step for p in model.parts})}
    return stage_key("resolve", {"model": d, "steps_present": present})


def mesh_failure_diag(users: list[str], task: dict, result: dict) -> Diagnostic:
    """MESH_INVERTED when every mesher that ran only produced inverted elements,
    else MESH_PART_FAILED. Skipped (unavailable) backends are listed in the evidence."""
    attempts = [{k: v for k, v in a.items() if k != "trace"} for a in result["attempts"]]
    raw = "\n".join(a.get("trace", "") for a in result["attempts"])[-3000:]
    ev = {"attempts": attempts, "size": task["size"]}
    ran = [a for a in attempts if not a.get("skipped")]
    inv = [a for a in ran if "inverted:" in a["error"]]
    if inv and len(inv) == len(ran):
        count = inv[-1]["error"].split("inverted:")[1].split()[0]
        return make("MESH_INVERTED", where={"parts": users}, part=", ".join(users), count=count, evidence=ev, raw=raw)
    return make("MESH_PART_FAILED", where={"parts": users}, part=", ".join(users), evidence=ev, raw=raw)


def sweep_fallback_diags(users: list[str], result: dict) -> list[Diagnostic]:
    """MESH_SWEEP_FALLBACK when a region meant to be swept was tet-meshed (D6: reported)."""
    swept = {r["region"] for r in result.get("regions", []) if r.get("mesher") == "native_sweep"}
    refused = [a for a in result.get("attempts", [])
               if (a.get("mesher") == "split"                    # the member could not be split at its spans
                   or (a.get("mesher") == "native_sweep" and not a.get("skipped") and a.get("region") not in swept))]
    for a in refused:
        a.setdefault("region", "the whole member")
    if not refused:
        return []
    regions = sorted({a.get("region", "?") for a in refused})
    ev = {"attempts": [{k: v for k, v in a.items() if k != "trace"} for a in refused]}
    return [make("MESH_SWEEP_FALLBACK", where={"parts": users}, part=", ".join(users), regions=", ".join(regions),
                 reason=refused[0].get("error", "?"), evidence=ev)]


def tagging_diags(users: list[str], stats: dict | None) -> list[Diagnostic]:
    if stats and stats.get("unclassified"):
        return [make("MESH_FACETS_UNTAGGED", where={"parts": users}, part=", ".join(users),
                     count=stats["unclassified"], evidence=stats)]
    return []


# Every run solves twice (PLAN.md, convergence check): base mesh and a global refinement.
LEVELS = (("base", 1.0), ("refined", defaults.REFINE_RATIO))


def levels_for(model: Model, mode: str = FULL) -> tuple:
    """Both meshes when the run planner (D60) keeps the convergence check (balanced, accuracy, or the study's
    override); only the base mesh for speed, when the study turns the check off, or in a fast run."""
    from .planner import plan_study
    return LEVELS if plan_study(model.study, fast=(mode == FAST)).convergence_check else LEVELS[:1]


def mesh_route(part) -> tuple[str, dict]:
    """How a part is meshed (PLAN.md Meshing, D6 option A): ("member", axis and clean spans),
    ("skin", thickness) for sheet and members without spans, or ("tet", {})."""
    if part.kind == "member" and part.member is not None:
        m = part.member
        wall = m.profile.get("t")
        wall = float(wall) if isinstance(wall, (int, float)) and wall > 0 else None
        path = m.path
        spans = [[c.s0, c.s1] for c in m.clean_spans]
        if path.get("type") == "line" and path.get("p0") and path.get("p1") and spans:
            p0, p1 = np.asarray(path["p0"], float), np.asarray(path["p1"], float)
            if np.linalg.norm(p1 - p0) > 0:
                return "member", {"path": "line", "origin": p0.tolist(),
                                  "axis": ((p1 - p0) / np.linalg.norm(p1 - p0)).tolist(), "spans": spans, "wall": wall}
        if path.get("type") == "arc" and path.get("p0") and path.get("p1") and path.get("center") and spans:
            # p0 to p1 about the centre; the turn is the short way unless the path gives its normal
            c = np.asarray(path["center"], float)
            r0, r1 = np.asarray(path["p0"], float) - c, np.asarray(path["p1"], float) - c
            R = float(np.linalg.norm(r0))
            n = np.asarray(path.get("normal") or np.cross(r0, r1), float)
            if R > 0 and abs(np.linalg.norm(r1) - R) < 1e-6 * R and np.linalg.norm(n) > 1e-9:
                return "member", {"path": "arc", "centre": c.tolist(), "normal": (n / np.linalg.norm(n)).tolist(),
                                  "start": (r0 / R).tolist(), "radius": R, "spans": spans, "wall": wall}
        return "skin", {"thickness": wall}
    if part.kind == "sheet" and part.sheet is not None:
        return "skin", {"thickness": part.sheet.thickness}
    if part.kind == "solid" and part.body is not None:
        # plate- or bar-like solids swept through their thickness (D56); the sweep finds the thickness, and a
        # part it cannot sweep falls back to tets as sheet does
        d = sorted(abs(b - a) for a, b in zip(part.body.bbox_min, part.body.bbox_max))
        fill = part.body.volume / (d[0] * d[1] * d[2]) if d[0] > 0 else 0.0
        if fill >= defaults.SOLID_SKIN_FILL and (d[1] >= defaults.SOLID_SKIN_ASPECT * d[0]
                                                 or d[2] >= defaults.SOLID_SKIN_ASPECT * d[1]):
            return "skin", {"thickness": None, "max_size": d[2] / defaults.SOLID_SKIN_SPAN_DIV}
    return "tet", {}


def _spans_clear_of_joints(part, rx: dict, conns: list[dict]) -> list:
    """A member's clean spans less every bonded footprint on it (the tie's sample points projected on the
    member's path), with the add-in's own margin (1.5 section depths each side) and minimum span (2 depths)."""
    spans = [list(s) for s in rx.get("spans", [])]
    if rx.get("path") != "line" or not spans:
        return spans
    prof = part.member.profile if part.member else {}
    depth = max(float(prof.get("b") or 0.0), float(prof.get("h") or prof.get("b") or 0.0)) or 0.0
    if depth <= 0 and part.body is not None:
        # a profile the add-in could not read (a welded chassis' tubes): the section's size from the body's box,
        # across the path
        ext = np.abs(np.asarray(part.body.bbox_max, float) - np.asarray(part.body.bbox_min, float))
        across = [e for k, e in enumerate(ext) if abs(np.asarray(rx["axis"], float)[k]) < 0.9]
        depth = float(max(across)) if across else 0.0
    if depth <= 0:
        return spans
    o, u = np.asarray(rx["origin"], float), np.asarray(rx["axis"], float)
    T = np.asarray(part.transform, float).reshape(4, 4)
    cuts = []
    for c in conns:
        if part.id not in c.get("parts", []) or not c.get("samples"):
            continue
        P = np.asarray(c["samples"], float)
        Pp = (P - T[:3, 3]) @ T[:3, :3]                  # into the part frame (the path is in the part frame)
        s = (Pp - o) @ u
        cuts.append((float(s.min()) - 1.5 * depth, float(s.max()) + 1.5 * depth))
    for lo, hi in cuts:
        nxt = []
        for s0, s1 in spans:
            for a, b in ((s0, min(s1, lo)), (max(s0, hi), s1)):
                if b - a >= 2.0 * depth:
                    nxt.append([a, b])
        spans = nxt
    return spans


def mesh_tasks(study: Path, model: Model, cfg, mode: str = FULL) -> tuple[dict, dict]:
    """(unique tasks by cache key, manifest {part: {level: {...}}}). A fast run meshes at the fast sizes, with
    the coarse size as each part's fallback."""
    from dataclasses import asdict
    from .mesh.backends import CHAINS, registry

    geo = read_json(study / "geometry.json")
    from .planner import plan_study
    fidelity = plan_study(model.study, fast=(mode == FAST)).fidelity     # D60: from favour, or the override
    user_size = model.study.mesh.size if model.study else None
    conn_doc = read_json(study / "connections.json")
    removed = set(conn_doc.get("removed_parts", []))
    conns = conn_doc.get("connections", [])
    backends = registry(cfg)
    versions = {n: backends[n].version() for c in CHAINS.values() for n in c}
    tasks, manifest = {}, {}
    for part in (p for p in model.parts if p.id not in removed):
        g = geo["parts"][part.id]
        dims = ([b - a for a, b in zip(part.body.bbox_min, part.body.bbox_max)] if part.body is not None
                else [g["bbox"][k + 3] - g["bbox"][k] for k in range(3)])       # no body description (D53)
        base = element_size(dims, user_size, fidelity)
        route, rx = mesh_route(part)
        if route == "skin" and part.kind == "solid" and any(
                c["type"] == "tie" and c.get("geometry") == "line" and part.id in c["parts"] for c in conns):
            # a solid bonded along an edge (a weldment's edge contact): one wedge through its thickness gave the
            # bond's narrow strip a single row of nodes, a near-hinge (6.5 % of the load lost on m11); tets, as before
            route, rx = "tet", {}
        if mode == FAST and route == "member":
            # long layers are exact only on truly clean spans: cut the spans at every bonded footprint the
            # resolver found (the add-in's clean spans missed 22 of 34 on a welded chassis: tubes butting into
            # tube sides, stand-offs; speed investigation)
            clear = _spans_clear_of_joints(part, rx, conns)
            if clear:
                rx = {**rx, "spans": clear, "layer_t": defaults.FAST_SWEEP_LAYER_T, "section_t": defaults.FAST_SWEEP_SECTION_T}
            else:                       # no span left between joints: the whole member skin-swept (as mesh_route)
                route, rx = "skin", {"thickness": rx.get("wall")}
        chain = CHAINS["tet" if route == "tet" else "swept"]
        manifest[part.id] = {}
        for level, factor in levels_for(model, mode):
            size = base * factor
            # swept sizes follow the wall thickness, scaled by fidelity and the level
            sweep_factor = defaults.FIDELITY_FACTOR[fidelity] * factor
            if route == "skin" and not user_size:
                # the tet fallback of a sheet whose sweep is refused: about one quadratic tet through the
                # thickness, not the minimum size (a 3 mm plate gave 218 000 tets at 1 mm, found live)
                t = rx.get("thickness") or min(d for d in dims if d > 0)    # thickness found by the sweep: the thin dimension
                size = max(size, defaults.SKIN_FALLBACK_SIZE_T * t * sweep_factor)
            key = cache_key(part.geometry_hash, g["solid_index"], size, [(n, versions[n]) for n in chain],
                            {"route": route, **rx, "factor": sweep_factor})
            manifest[part.id][level] = {"key": key, "size": size, "route": route}
            tasks.setdefault(key, {
                "key": key, "part": part.id, "level": level, "body_step": str(study / g["body_step"]),
                "element": "tet" if route == "tet" else "swept", "route": route, "route_extra": rx,
                "factor": sweep_factor, "size": size, "out": str(study / "mesh" / f"{key}.npz"),
                "workdir": str(study / "mesh" / "work" / key), "face_areas": [f["area"] for f in g["faces"]],
                "chain": chain, "timeout_s": defaults.MESH_TIMEOUT_S, "config": asdict(cfg)})
            if mode == FAST:
                fb = defaults.FAST_FALLBACK_FACTOR / defaults.FAST_FIDELITY_FACTOR
                tasks[key]["fallback"] = {"size": size * fb, "factor": sweep_factor * fb}
                tasks[key]["gmsh_optimize"] = True       # untangle quadratic tets (angled SHS ends inverted)
    return tasks, manifest


def stage_mesh(study: Path, js: JobState) -> None:
    model = load_model(study)
    cfg = load_config()
    mode = mode_of(js)
    (study / "mesh").mkdir(exist_ok=True)
    tasks, manifest = mesh_tasks(study, model, cfg, mode)
    done = [0]

    def on_done(task, res):
        done[0] += 1
        how = "cached" if res.get("cached") else (res.get("mesher") if res["ok"] else "FAILED")
        js.activity(f"mesh: {done[0]}/{len(tasks)} unique parts meshed (last: {task['part']}, {how})",
                    done[0], len(tasks))
        js.log(f"mesh {task['part']}: {json.dumps({k: v for k, v in res.items() if k != 'attempts'})}")
        for a in res.get("attempts", []):
            if a.get("timed_out"):
                js.add_diags([make("MESH_TIMEOUT", where={"parts": [task["part"]]}, part=task["part"],
                                   mesher=a["mesher"], seconds=task["timeout_s"], evidence=a)])

    def on_tick(lines: dict[str, str]) -> bool:
        if lines:
            last = "; ".join(f"{p}: {l}" for p, l in list(lines.items())[:3])
            js.activity(f"mesh: {done[0]}/{len(tasks)} done; running {last}"[:300], done[0], len(tasks))
        return js.cancel_requested()

    js.activity(f"mesh: {len(tasks)} unique parts ({len(model.parts)} instances)", 0, len(tasks))
    try:
        with perf.span("meshing", unique=len(tasks), instances=len(model.parts), workers=cfg.threads) as pinfo:
            results = mesh_parts(list(tasks.values()), cfg.threads, on_done, on_tick, force=force_requested(js))
            pinfo["cached"] = sum(1 for r in results.values() if r.get("cached"))
            note_reuse(js, "mesh", f"reused for {pinfo['cached']} of {len(tasks)} unique meshes")
            pinfo["part_seconds"] = {t["part"] + "/" + t["level"]: round(results[k].get("seconds") or 0.0, 2)
                                     for k, t in tasks.items() if not results[k].get("cached")}
    except MeshCancelled:
        raise Cancelled()
    diags = []
    for key, t in tasks.items():
        r = results[key]
        users = [(p, lv) for p, levels in manifest.items() for lv, m in levels.items() if m["key"] == key]
        parts = sorted({p for p, _ in users})
        if not r["ok"]:
            diags.append(mesh_failure_diag(parts, t, r))
        else:
            for p, lv in users:
                manifest[p][lv].update({k: r[k] for k in ("mesher", "nodes", "elements", "quality", "tagging",
                                                          "regions", "internal_ties", "seconds")
                                        if k in r})
                manifest[p][lv]["fallback"] = any(not a.get("skipped") for a in r.get("attempts", []))
                if r.get("fell_back"):
                    manifest[p][lv]["coarse_size"] = True
                if r.get("size_reduced"):
                    manifest[p][lv]["size_reduced"] = r["size_reduced"]
            diags += tagging_diags(parts, r.get("tagging"))
            if r.get("size_reduced") and t["level"] == "base":
                diags.append(make("MESH_SIZE_REDUCED", where={"parts": parts}, part=", ".join(parts),
                                  factor=f"{r['size_reduced']:g}", size=f"{t['size'] * r['size_reduced']:.3g}",
                                  evidence={"size_reduced": r["size_reduced"], "attempts": r.get("attempts", [])[-6:]}))
            if t["level"] == "base":
                diags += sweep_fallback_diags(parts, r)
    _write(study / "mesh" / ("manifest_fast.json" if mode == FAST else "manifest.json"),
           {"schema_version": SCHEMA_VERSION, "parts": manifest})
    coarse = sorted(p for p, lv in manifest.items() if any(v.get("coarse_size") for v in lv.values()))
    if coarse:
        js.result(f"mesh (fast): {len(coarse)} part(s) meshed at the coarse size (they would not mesh at the fast size): "
                  + ", ".join(coarse[:8]) + ("..." if len(coarse) > 8 else ""))
    js.add_diags(diags)
    raise_if_blocking(diags)
    for level, _ in levels_for(model, mode):
        nodes = sum(PartMesh.load(study / "mesh" / f"{m[level]['key']}.npz").nodes.shape[0]
                    for m in manifest.values())
        js.result(f"mesh ({level}): {len(manifest)} parts, {nodes:,} nodes, ~{3 * nodes:,} DOF")


# --------------------------------------------------------------------------- #

def build_instances(study: Path, model: Model, level: str = "base", mode: str = FULL) -> list[Instance]:
    manifest = read_json(study / "mesh" / ("manifest_fast.json" if mode == FAST else "manifest.json"))["parts"]
    removed = set(read_json(study / "connections.json").get("removed_parts", []))
    out, node0, elem0 = [], 0, 0
    cache: dict[str, PartMesh] = {}
    for i, part in enumerate(model.parts, start=1):
        if part.id in removed:
            continue
        key = manifest[part.id][level]["key"]
        pm = cache.setdefault(key, PartMesh.load(study / "mesh" / f"{key}.npz"))
        T = np.asarray(part.transform, float).reshape(4, 4)
        inst = Instance(part.id, f"P{i}", pm, T, node0, elem0, part.material)
        out.append(inst)
        node0 += pm.nodes.shape[0]
        elem0 += pm.n_elements
    return out


def joint_band(part, measured: float | None = None) -> float:
    """Width of the band round a bonded joint where stresses are not assessed (D32): 1.5 x the wall
    thickness (sheet, member profile), and at least JOINT_BAND_MIN."""
    t = 0.0
    if part is not None and getattr(part, "sheet", None) is not None and part.sheet.thickness:
        t = float(part.sheet.thickness)
    elif part is not None and getattr(part, "member", None) is not None:
        prof = part.member.profile if hasattr(part.member, "profile") else None
        t = float(getattr(prof, "t", 0.0) or (prof.get("t", 0.0) if isinstance(prof, dict) else 0.0) or 0.0)
    t = t or float(measured or 0.0)         # the thickness measured from the mesh (D34) when the model has none
    return max(defaults.JOINT_BAND_T * t, defaults.JOINT_BAND_MIN)


def _perimeter_nodes(inst: Instance, face_idx: int, inside: set[int]) -> set[int]:
    """Local ids of a tie face's bonded nodes along the edges of the bonded area: on facets that lie
    on the face's own boundary, or that straddle the edge of the bonded area (``inside``)."""
    from collections import Counter
    from .mesh.elements import FACE_NODES, FACES
    facets = []
    for _, fnum, et, conn in inst.face_elements(face_idx):
        facets.append(([int(conn[i]) for i in FACES[et][fnum - 1]], [int(conn[i]) for i in FACE_NODES[et][fnum - 1]]))
    edges = Counter(frozenset(e) for corners, _ in facets for e in zip(corners, corners[1:] + corners[:1]))
    out: set[int] = set()
    for corners, nodes in facets:
        on_face_edge = any(edges[frozenset(e)] == 1 for e in zip(corners, corners[1:] + corners[:1]))
        ins = [n in inside for n in nodes]
        if any(ins) and (on_face_edge or not all(ins)):
            out.update(n for n, i in zip(nodes, ins) if i)
    return out


def tie_zone_nodes(instances: list[Instance], conns: list[dict], model: Model | None = None,
                   measured: dict | None = None) -> dict[str, list[int]]:
    """Global ids of nodes where stresses are not assessed: the bonded area of every tie (its
    touching footprint, weld strip, or butt joint) and everything within the joint band of it
    (D32). A sharp edge bonded to another part is a singular point whose peak grows without limit
    as the mesh is refined; it is read away from the joint, as a weld would be checked separately."""
    from scipy.spatial import cKDTree
    from .deck import _in_footprint, _in_strip
    by = {i.part_id: i for i in instances}
    parts = {p.id: p for p in model.parts} if model is not None else {}
    seeds: dict[str, set] = {}
    for c in conns:
        if c["type"] != "tie":
            continue
        for s in c["sides"]:
            inst = by[s["part"]]
            ids = seeds.setdefault(inst.part_id, set())
            nodes = np.asarray(inst.face_nodes(int(s["face"])))
            if len(nodes) == 0:
                continue
            X = inst.X[nodes - 1]
            if c.get("region") is not None:
                nodes = nodes[_in_strip(X, c["region"])]
            elif not c.get("butt"):
                nodes = nodes[_in_footprint(X, c)]
            # the band grows from the EDGES of the bonded area, where a bond ends singularly; inside a
            # bonded patch (a plate lapped on a tube) the stress is ordinary and stays assessed
            ids.update(int(inst.node0 + n) for n in _perimeter_nodes(inst, int(s["face"]), {int(n) for n in nodes}))
        b = c.get("butt")
        if b:
            # a weldment butt joint is also tied to the corner radii beside the flat (D29): their elements
            # under the butting end are tie zone too, or the artificial peak where the end is tied onto a
            # radius counts as real stress (found live: 1149 MPa on a tube's radius). Only under the end:
            # a radius runs the whole length of its tube.
            si = by[b["slave"]["part"]]
            Y = si.X[np.asarray(si.face_nodes(int(b["slave"]["face"]))) - 1]
            lo, hi = Y.min(axis=0) - b["tolerance"], Y.max(axis=0) + b["tolerance"]
            for m in b["masters"]:
                inst = by[m["part"]]
                ids = seeds.setdefault(inst.part_id, set())
                # every face element at once, grouped by node count (one at a time in Python was most of
                # this step: 7 s on a 53-member frame)
                groups: dict[int, list] = {}
                for _, _, _, conn in inst.face_elements(int(m["face"])):
                    groups.setdefault(len(conn), []).append(conn)
                for conns_ in groups.values():
                    C = np.asarray(conns_, dtype=np.int64)
                    P = inst.X[C - 1]                                          # (elements, nodes, 3)
                    under = np.all((P >= lo) & (P <= hi), axis=2).any(axis=1)
                    if under.any():
                        ids.update((inst.node0 + C[under]).ravel().tolist())
    zones = {}
    for pid, ids in seeds.items():
        if not ids:
            continue
        inst = by[pid]
        band = joint_band(parts.get(pid), (measured or {}).get(pid))
        seed_xyz = inst.X[np.asarray(sorted(ids)) - inst.node0 - 1]
        d, _ = cKDTree(seed_xyz).query(inst.X, k=1, distance_upper_bound=band + 1e-9)
        zones[pid] = sorted(int(inst.node0 + 1 + i) for i in np.nonzero(np.isfinite(d))[0])
    return zones


def _history_path(name: str = "solve_history.json") -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "asmfea"
    base.mkdir(parents=True, exist_ok=True)
    return base / name


DECK_HISTORY = "deck_history.json"


def estimate_deck_s(dof: int) -> float | None:
    """How long writing a deck of this size takes on this machine: the nearest earlier deck (by DOF), scaled by
    (dof ratio)^1.1 (the deck's tie, joint and load work grows a little faster than its size). None before the
    first deck is recorded."""
    try:
        hist = json.loads(_history_path(DECK_HISTORY).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not hist:
        return None
    best = min(hist, key=lambda h: abs(np.log(h["dof"] / dof)))
    return max(best["seconds"] * (dof / best["dof"]) ** 1.1, 1.0)


def _record_deck_history(dof: int, seconds: float) -> None:
    p = _history_path(DECK_HISTORY)
    try:
        hist = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        hist = []
    hist.append({"dof": dof, "seconds": round(seconds, 2), "when": time.time()})
    p.write_text(json.dumps(hist[-200:]))


def estimate_solve_s(dof: int) -> float | None:
    """Scale the nearest earlier run on this machine by (dof ratio)^1.3."""
    try:
        hist = json.loads(_history_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not hist:
        return None
    best = min(hist, key=lambda h: abs(np.log(h["dof"] / dof)))
    return best["seconds"] * (dof / best["dof"]) ** 1.3


def _record_history(dof: int, seconds: float, steps: int) -> None:
    p = _history_path()
    try:
        hist = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        hist = []
    hist.append({"dof": dof, "seconds": seconds, "steps": steps, "when": time.time()})
    p.write_text(json.dumps(hist[-200:]))


def _without_removed(model: Model, removed: set[str], face_map: dict) -> Model:
    """The model without the loads (and load faces) on parts left out of the analysis: a part free to move
    on a hinge is left out with its loads, which it could not carry (PART_EXCLUDED_MECHANISM)."""
    if not removed:
        return model
    loads = []
    for ld in model.loads:
        faces = [f for f in ld.faces if not (f in face_map and face_map[f][0] in removed)]
        if ld.faces and not faces:
            continue
        loads.append(ld.model_copy(update={"faces": faces}))
    return model.model_copy(update={"loads": loads})


def solve_level(study: Path, js: JobState, model: Model, level: str, cfg, plan=None) -> dict:
    """Write and solve the decks of one mesh level in run/<job>/<level>/.

    Linear studies: one deck with every load case (and the buckling steps).
    Contact or preloaded bolts: load cases cannot be superposed, so each is its own
    deck (case_<i>/), and buckling runs on a linear stand-in deck (buckling/) where
    contact is tied and bolts are bonded (stated in the report).
    """
    geo = read_json(study / "geometry.json")
    conns = read_json(study / "connections.json")["connections"]
    face_map = {k: tuple(v) for k, v in geo["face_map"].items()}
    model = _without_removed(model, set(read_json(study / "connections.json").get("removed_parts", [])), face_map)
    d = js.dir / level
    d.mkdir(parents=True, exist_ok=True)
    instances = build_instances(study, model, level, mode_of(js))
    bolts = plan_preloaded_bolts(model, conns, geo)
    contact = any(c["type"] == "contact" for c in conns) or bool(bolts)
    exc = read_json(study / "exceptions.json")["exceptions"] if (study / "exceptions.json").exists() else []
    stabilise = sorted({p for e in exc if e["code"] == "CONTACT_ONLY_SUPPORT" for p in e["where"]["parts"]})
    cases = list(model.study.load_cases)
    superposed = _superposition(model, conns, bolts, js) if level == "base" else _superposition(model, conns, bolts)
    if superposed:
        # one step per load (superpose.py): a load case is then the sum of its loads, so a later change to the
        # size of loads needs no new solve; post reads the cases from cases/, rebuilt for the loads of the day
        from .superpose import per_load_model
        solved = per_load_model(model)
        specs = [{"dir": "loads", "cases": list(solved.study.load_cases), "kind": "linear", "conns": conns, "bolts": [],
                  "buckling": False, "model": solved}]
    elif not contact:
        specs = [{"dir": ".", "cases": cases, "kind": "linear", "conns": conns, "bolts": [], "buckling": True}]
    else:
        specs = [{"dir": f"case_{i}", "cases": [c], "kind": "contact", "conns": conns, "bolts": bolts,
                  "buckling": False} for i, c in enumerate(cases, start=1)]
        if "buckling" in model.study.analyses:
            stand_in = [dict(c, type="tie") if c["type"] == "contact" else c for c in conns]
            specs.append({"dir": "buckling", "cases": [], "kind": "buckling stand-in (contact tied, bolts bonded)",
                          "conns": stand_in, "bolts": [], "buckling": True})
    dof = 3 * sum(len(i.X) for i in instances)
    stats, runs = {"dof": dof, "seconds": 0.0, "peak_rss_gb": 0.0}, []
    for spec in specs:
        rd = d / spec["dir"]
        rd.mkdir(parents=True, exist_ok=True)
        label = f"{level}" + ("" if spec["dir"] == "." else f" {spec['dir']}")
        # elapsed time against an estimate from earlier decks on this machine (the user's request: the step
        # reports no progress of its own and can take minutes on a large model)
        t_deck = time.time()
        with js.timed(f"solve ({label}): assembling the deck", estimate_deck_s(dof)), \
                perf.span("deck", level=level, dof=dof) as pinfo:
            deck = write_deck(rd / "study.inp", spec.get("model", model), instances, spec["conns"], face_map, spec["cases"],
                              bolts=[dict(b) for b in spec["bolts"]], stabilise=stabilise if spec["kind"] == "contact" else [],
                              buckling=spec["buckling"])
            pinfo["inp_bytes"] = sum(f.stat().st_size for f in rd.glob("*") if f.suffix in (".inp", ".nam", ".sur", ".equ"))
        _record_deck_history(dof, time.time() - t_deck)
        if deck.diagnostics:
            js.add_diags(deck.diagnostics)
            raise_if_blocking(deck.diagnostics)
        with perf.span("tie_zones", level=level):
            zones = tie_zone_nodes(instances, spec["conns"], model, deck.info.get("wall_thickness"))
        deck.info.update({"dof": dof, "level": level, "kind": spec["kind"], "tie_zone_nodes": zones,
                          "elements": sum(i.mesh.n_elements for i in instances)})
        if "model" in spec:
            from .superpose import solved_map
            deck.info["superposition"] = solved_map(deck.info, spec["model"])
        _write(rd / "deck_info.json", deck.info)
        # the run planner (D60) chooses the solver from the deck's size and the memory free when the run began:
        # CalculiX when it fits the budget with a margin, the hybrid solver otherwise (linear decks only)
        from .planner import choose_solver, plan_study
        plan = plan or plan_study(model.study)
        if os.environ.get("ASMFEA_TEST_FORCE_HYBRID"):                       # test switch: as if it would not fit
            plan.solver_pref = "hybrid"
        solver_choice, plan_notes = choose_solver(plan, dof, spec["kind"])
        for line in plan_notes:
            js.log(f"plan: {line}")
        mem = memory_check(dof)
        too_big = [d for d in mem if d.code == "SOLVE_MEMORY_EXCEEDS_RAM"]
        use_hybrid = solver_choice == "hybrid"
        if not use_hybrid:
            js.add_diags(mem)
            raise_if_blocking(mem)
        est = estimate_solve_s(dof) if spec["kind"] == "linear" and not use_hybrid else None
        js.log(f"deck ({label}) written: {dof:,} DOF, cases {spec['cases']}, estimate {est}")
        tick = lambda st: (js.solver({**st, "level": label}, est), js.cancel_requested())[1]
        solver_used = "calculix"
        if use_hybrid:
            # CalculiX would not fit in memory: the hybrid solver condenses each part onto its joints and
            # needs about the largest part's memory instead (D49). A deck it cannot solve is refused as before.
            from .hybrid.deckread import Unsupported
            from .solve import run_hybrid
            try:
                with perf.span("hybrid", level=level, dof=dof) as pinfo:
                    outcome = run_hybrid(rd, "study", on_tick=tick, budget_gb=plan.budget_gb,
                                         exact_part_unknowns=plan.exact_part_unknowns,
                                         exact_total_unknowns=plan.exact_total_unknowns, reserve_gb=plan.reserve_gb,
                                         dof=dof)
                    pinfo["peak_rss_gb"] = outcome.peak_rss_gb
                solver_used = "hybrid"
            except Unsupported as e:
                # a deck the hybrid cannot take (bolts, equations, buckling): CalculiX after all, unless it
                # cannot fit in the computer at all (it would page to a standstill)
                js.log(f"hybrid solver cannot take this deck ({e}); solving with CalculiX")
                js.add_diags(mem)
                raise_if_blocking(too_big)
                use_hybrid = False
                plan.solver = "calculix"
        if use_hybrid:
            import psutil
            ram = psutil.virtual_memory().total / 1e9
            js.add_diags([make("SOLVER_HYBRID", dof=dof, need_gb=estimate_memory_gb(dof), ram_gb=ram,
                               peak_gb=outcome.peak_rss_gb, evidence={"dof": dof, "level": level, "ram_gb": ram,
                                                                      "peak_rss_gb": outcome.peak_rss_gb})])
        else:
            with perf.span("calculix", level=level, dof=dof, threads=cfg.threads) as pinfo:
                outcome = run_ccx(cfg, rd, "study", dof, on_tick=tick)
                pinfo.update(perf_ccx_log(rd / "ccx.log"))
        if outcome.cancelled:
            raise Cancelled()
        if outcome.ok and outcome.diagnostics:            # warnings from a solve that succeeded (a retry, D60)
            js.add_diags(outcome.diagnostics)
        if not outcome.ok:
            for dg in outcome.diagnostics:
                if dg.code == "CONTACT_NOT_CONVERGED":
                    pairs = [c["id"] for c in spec["conns"] if c["type"] == "contact"]
                    dg.where.connections = pairs
                    dg.where.parts = sorted({p for c in spec["conns"] if c["id"] in pairs for p in c["parts"][:2]})
                    dg.what = dg.what.replace("converge.", f"converge (load cases {spec['cases']}; contact pairs "
                                                           f"{', '.join(pairs)}).")
            js.add_diags(outcome.diagnostics)
            raise Blocked(outcome.diagnostics)
        if spec["kind"] == "linear":
            _record_history(dof, outcome.seconds, len(spec["cases"]))
        stats["seconds"] += outcome.seconds
        stats["peak_rss_gb"] = max(stats["peak_rss_gb"], outcome.peak_rss_gb)
        extra = {"solver_info": outcome.info} if outcome.info else {}     # the hybrid's joints, for the grade
        if "model" in spec:
            from .superpose import prepare_cases
            runs.append({"dir": "cases", "cases": cases, "kind": spec["kind"], "superposed_from": spec["dir"],
                         "solver": solver_used, **extra})
            prepare_cases(d, model)
        else:
            runs.append({"dir": spec["dir"], "cases": spec["cases"], "kind": spec["kind"], "solver": solver_used,
                         **extra})
        js.result(f"solve ({label}): {dof:,} DOF, {len(spec['cases'])} load case(s) in {outcome.seconds:.1f} s, "
                  f"peak solver memory {outcome.peak_rss_gb:.1f} GB")
    _write(d / "runs.json", runs)
    return stats


def perf_ccx_log(path: Path) -> dict:
    """What CalculiX reports about the system it solved (INVESTIGATION-001): equations, non-zeros,
    equation solver and its threads, and its own total time."""
    import re
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    out = {}
    for key, rx in (("equations", r"number of equations\s+(\d+)"),
                    ("nonzeros", r"number of nonzero lower triangular matrix elements\s+(\d+)"),
                    ("solver", r"Factoring the system of equations using the (.+?) solver"),
                    ("solver_threads", r"number of threads =\s*(\d+)"),
                    ("ccx_total_s", r"Total CalculiX Time:\s*([\d.]+)")):
        m = re.search(rx, text)
        if m:
            out[key] = m.group(1).strip()
    out["tie_warnings"] = text.count("*WARNING in gentiedmpc")
    return out


def _superposition(model: Model, conns: list[dict], bolts: list[dict], js=None) -> bool:
    """Whether this solve uses load superposition (the study asks for it and its results are linear in the
    loads). When asked for but not usable, says why (SUPERPOSITION_NOT_USED) if a job is given."""
    if not model.study.load_superposition:
        return False
    from .superpose import not_usable
    why = not_usable(model, conns, bolts)
    if why and js is not None:
        js.add_diags([make("SUPERPOSITION_NOT_USED", where={"loads": [ld.id for ld in model.loads]}, reason=why,
                           evidence={"analyses": list(model.study.analyses)})])
    return why is None


def plan_preloaded_bolts(model: Model, conns: list[dict], geo: dict) -> list[dict]:
    """Bolt axis, the bearing faces at both ends of the clamped stack, and the preload,
    for every bolted joint switched to preload mode."""
    out = []
    for j in conns:
        if j["type"] != "bolted" or not j.get("preload"):
            continue
        f = model.part(j["fastener"])
        T = np.asarray(f.transform, float).reshape(4, 4)
        axis = T[:3, :3] @ np.asarray(f.fastener.axis, float)
        axis /= np.linalg.norm(axis)
        holes = [(h["part"], geo["parts"][h["part"]]["faces"][h["face"]]) for h in j["holes"]]
        p0 = np.asarray(holes[0][1]["axis_point"], float)
        p0 = p0 - np.dot(p0, axis) * axis   # a point on the bolt axis
        ends = []
        for part, face in holes:
            ts = [float(np.dot(np.asarray(s), axis)) for s in face["samples"]]
            ends += [(min(ts), part), (max(ts), part)]
        (t_lo, part_lo), (t_hi, part_hi) = min(ends), max(ends)

        def bearing(part, t, sign):
            best = None
            for k, fc in enumerate(geo["parts"][part]["faces"]):
                if fc["surface"] != "plane" or np.dot(fc["normal"], axis) * sign < 0.999:
                    continue
                if abs(np.dot(np.asarray(fc["centroid"]), axis) - t) > 0.1:
                    continue
                best = k
            return best
        head_face, nut_face = bearing(part_hi, t_hi, 1.0), bearing(part_lo, t_lo, -1.0)
        mat = model.part(part_hi).material
        out.append({"id": j["id"], "fastener": j["fastener"], "d": f.fastener.d, "axis": axis.tolist(),
                    "preload": j["preload"], "material": mat,
                    "r_bearing": defaults.BOLT_BEARING_RADIUS * f.fastener.d,
                    "head": {"part": part_hi, "face": head_face, "point": (p0 + t_hi * axis).tolist()},
                    "nut": {"part": part_lo, "face": nut_face, "point": (p0 + t_lo * axis).tolist()}})
    return out


def chosen_cases(model: Model, js: JobState) -> Model:
    """The load cases this run solves (job.json "cases", from Run's drop-down; all when absent):
    the others are left out of the model for this run, with their loads."""
    try:
        want = read_json(js.path).get("cases") or []
    except (OSError, ValueError):
        want = []
    keep = [c for c in model.study.load_cases if c in want]
    if not want or not keep:
        return model
    model.study.load_cases = keep
    model.study.buckling_cases = [c for c in model.study.buckling_cases if c in keep]
    model.loads = [ld for ld in model.loads if ld.load_case in keep]
    return model


def solve_key(study: Path, model: Model, cfg, levels: list[str], superposed: bool = False, mode: str = FULL) -> str:
    """What the decks and their results depend on: the whole model (loads, fixtures, materials, the load
    cases chosen) except the pass/fail limits, the resolved geometry and connections, the mesh of every
    part at every level, and the solver binary."""
    d = _model_doc(model)
    st = dict(d.get("study") or {})
    st.pop("limits", None)
    d["study"] = st
    if superposed:
        # each load is solved at its own size once and scaled afterwards: its size is not an input of the decks
        d["loads"] = [{k: v for k, v in ld.items() if k != "magnitude"} for ld in d.get("loads", [])]
    manifest = read_json(study / "mesh" / ("manifest_fast.json" if mode == FAST else "manifest.json"))["parts"]
    meshes = {p: {lv: m[lv]["key"] for lv in levels} for p, m in manifest.items()}
    files = {n: file_digest(study / n) for n in ("geometry.json", "connections.json", "exceptions.json")
             if (study / n).exists()}
    ccx = Path(cfg.ccx) if cfg.ccx else None
    tool = [str(ccx), ccx.stat().st_size, ccx.stat().st_mtime_ns] if ccx and ccx.is_file() else None
    return stage_key("solve", {"model": d, "meshes": meshes, "files": files, "levels": levels, "ccx": tool,
                               "superposed": superposed})


def run_outputs(run: Path, levels: list[str]) -> list[Path]:
    """The files post reads from a solved run."""
    out = [run / "solve_stats.json"]
    for lv in levels:
        out.append(run / lv / "runs.json")
        for r in read_json(run / lv / "runs.json"):
            rd = run / lv / r.get("superposed_from", r["dir"])     # superposed: the per-load deck (cases/ is derived)
            out += [rd / "deck_info.json", rd / "study.frd"] + ([rd / "study.dat"] if (rd / "study.dat").exists() else [])
    return out


def stage_solve(study: Path, js: JobState) -> None:
    model = chosen_cases(load_model(study), js)
    cfg = load_config()
    mode = mode_of(js)
    levels = [lv for lv, _ in levels_for(model, mode)]
    cache = StageCache(study)
    conns = read_json(study / "connections.json")["connections"]
    sup = _superposition(model, conns, plan_preloaded_bolts(model, conns, read_json(study / "geometry.json")))
    k = solve_key(study, model, cfg, levels, sup, mode)
    stage = "solve_fast" if mode == FAST else "solve"
    rec = None if force_requested(js) else cache.get(stage, k)
    if rec:
        # nothing that affects the decks has changed since that run: its results stand (CalculiX is
        # deterministic for a given deck)
        run_id = rec["data"]["run"]
        latest_solve_file(study, mode).write_text(run_id)
        replay(js, rec["data"])
        js.result(f"solve: reused the results of job {run_id} (nothing that affects the solve changed)")
        note_reuse(js, "solve", f"results of job {run_id} reused")
        return
    js = Recorder(js)
    # the run planner (D60): this computer's free memory and cores, measured now, decide the solver and its budget
    from .planner import plan_study
    plan = plan_study(model.study, fast=(mode == FAST))
    for line in plan.notes:
        js.log(f"plan: {line}")
    stats = {level: solve_level(study, js, model, level, cfg, plan) for level in levels}
    _write(js.dir / "plan.json", plan.as_dict())
    _write(js.dir / "solve_stats.json", stats)
    latest_solve_file(study, mode).write_text(js.dir.name)
    cache.put(stage, k, run_outputs(js.dir, levels), {**js.data(), "run": js.dir.name})


def _stress_classification(study: Path, model: Model, lv: str, run: Path, res: dict, js: JobState, mode: str = FULL) -> None:
    """Per load case: each part's membrane-plus-bending stress (its static FoS uses it) and the
    Battelle structural stress in the wall each section tie's member is bonded onto (D34)."""
    instances = build_instances(study, model, lv, mode)
    topo: dict = {}
    with perf.span("stress_classification", level=lv):
        _classify(study, model, lv, run, res, js, instances, topo, mode)


_CLS: dict = {}          # a classification worker's model, meshes and results, loaded once (_classify_init)


def _classify_init(study: str, lv: str, mode: str, rd: str, thick: dict, band: dict) -> None:
    """A classification worker: load the model, the meshes and the results once (the results from the arrays
    read_frd keeps beside the .frd, so quickly)."""
    from .post.frd import read_frd
    study_p = Path(study)
    model = load_model(study_p)
    _CLS.update(inst={i.part_id: i for i in build_instances(study_p, model, lv, mode)},
                frd=read_frd(Path(rd) / "study.frd"), thick=thick, band={k: set(v) for k, v in band.items()},
                topo={}, stress={})


def _classify_part(task: tuple) -> tuple:
    """One part's membrane plus bending stress for one load case's step, in a worker: (part id, result)."""
    from .post.frd import von_mises
    from .post.sections import PartTopology, membrane_bending
    step, part_id = task
    if step not in _CLS["stress"]:
        sn, sv = _CLS["frd"][step]["STRESS"]
        stress = np.zeros((int(sn.max()) + 1, 6))
        stress[sn.astype(np.int64)] = sv[:, :6]
        _CLS["stress"] = {step: (sn, von_mises(sv[:, :6]), stress)}            # one step at a time
    sn, vm, stress = _CLS["stress"][step]
    inst = _CLS["inst"][part_id]
    sel = (sn > inst.node0) & (sn <= inst.node0 + inst.X.shape[0])
    tp = _CLS["topo"].setdefault(part_id, PartTopology(inst))
    return part_id, membrane_bending(inst, tp, np.column_stack([sn[sel], vm[sel]]), stress,
                                     _CLS["band"].get(part_id, set()), thin=_CLS["thick"].get(part_id))


def _classify(study, model, lv, run, res, js, instances, topo, mode: str = FULL) -> None:
    from .post import _wall_thickness
    from .post.frd import read_frd, von_mises
    from .post.sections import PartTopology, membrane_bending, receiving_walls
    from .stagecache import code_fingerprint, defaults_digest, digest

    def apply(lc: dict, part_id: str, mb: dict | None) -> None:
        p = lc["parts"].get(part_id)
        if p is not None and mb and mb["membrane_bending"] > 0:
            mat = model.material(next(i.material for i in instances if i.part_id == part_id))
            p.update(membrane_bending=mb["membrane_bending"], membrane_bending_at=mb["at"], membrane=mb["membrane"],
                     min_fos=mat.yield_ / mb["membrane_bending"])

    for r in read_json(run / lv / "runs.json"):
        if not r["cases"]:
            continue
        rd = run / lv / r["dir"]
        info = read_json(rd / "deck_info.json")
        step_of = {s["case"]: s["step"] for s in info["steps"] if s["kind"] == "static"}
        band = {k: set(v) for k, v in info.get("tie_zone_nodes", {}).items()}
        thick = _wall_thickness(model, info)
        # membrane plus bending depends on this run's results and meshes, the wall thicknesses, the code and
        # the defaults, not on the pass/fail limits: kept beside the results (35 s of a 53 s post on the
        # 53-member frame, INVESTIGATION-001), so a change of limits or view does not repeat it
        saved_path = rd / "classification.json"
        ck = digest({"code": code_fingerprint(), "defaults": defaults_digest(), "thick": thick,
                     "results": file_digest(rd / "study.frd")})
        try:
            saved = json.loads(saved_path.read_text())
            saved = saved["by_case"] if saved.get("key") == ck else None
        except (OSError, ValueError, KeyError):
            saved = None
        if saved is not None and not defaults.RECEIVING_WALLS and all(c in saved for c in r["cases"]):
            for case in r["cases"]:
                lc = res["load_cases"].get(case)
                for part_id, mb in (saved[case].items() if lc is not None else []):
                    apply(lc, part_id, mb)
            continue
        frd = read_frd(rd / "study.frd")
        found: dict = {}
        # a large model's parts are classified in worker processes, each loading the meshes and results once
        # itself (pure Python per part: 22 s on one core for a 4.5 M-DOF frame); a small one here, as before
        big = (sum(len(i.X) for i in instances) >= defaults.CLASSIFY_POOL_MIN_NODES
               and len(instances) >= 4 and not defaults.RECEIVING_WALLS)
        pool = None
        if big:
            try:
                import multiprocessing as mp
                from concurrent.futures import ProcessPoolExecutor
                workers = min(defaults.CLASSIFY_WORKERS, len(instances), max(1, (os.cpu_count() or 2) - 1))
                pool = ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"),
                                           initializer=_classify_init,
                                           initargs=(str(study), lv, mode, str(rd), thick,
                                                     {k: sorted(v) for k, v in band.items()}))
            except Exception:                                       # noqa: BLE001 - classified here instead
                pool = None
        try:
            for case in r["cases"]:
                lc = res["load_cases"].get(case)
                if lc is None or case not in step_of:
                    continue
                k = step_of[case]
                un, uv = frd[k]["DISP"]
                js.activity(f"post: membrane plus bending and receiving walls, load case {case}")
                found[case] = {}
                todo = [inst for inst in instances if lc["parts"].get(inst.part_id) is not None]
                if pool is not None:
                    for part_id, mb in pool.map(_classify_part, [(k, inst.part_id) for inst in todo]):
                        found[case][part_id] = mb
                        apply(lc, part_id, mb)
                else:
                    sn, sv = frd[k]["STRESS"]
                    stress = np.zeros((int(sn.max()) + 1, 6))   # by global node id (a dict took seconds on 1 M nodes)
                    stress[sn.astype(np.int64)] = sv[:, :6]
                    vm = von_mises(sv[:, :6])
                    for inst in todo:
                        sel = (sn > inst.node0) & (sn <= inst.node0 + inst.X.shape[0])
                        tp = topo.setdefault(inst.part_id, PartTopology(inst))
                        mb = membrane_bending(inst, tp, np.column_stack([sn[sel], vm[sel]]), stress,
                                              band.get(inst.part_id, set()), thin=thick.get(inst.part_id))
                        found[case][inst.part_id] = mb
                        apply(lc, inst.part_id, mb)
                # the receiving wall (D34): mesh-sensitive on small or wrapping footprints, so off by default
                disp = {int(n): uv[i, :3] for i, n in enumerate(un)} if defaults.RECEIVING_WALLS else {}
                for rec in (receiving_walls(instances, model, info, disp, thick, topo) if defaults.RECEIVING_WALLS else []):
                    lc.setdefault("joints", []).append(rec)
                    pr = lc["parts"].get(rec["part"])
                    if pr is not None and rec["structural_stress"] > pr.get("joint_stress", 0.0):
                        pr.update(joint_stress=rec["structural_stress"], joint_stress_at=rec["at"], joint_fos=rec["fos"],
                                  joint_with=rec["other"])
        finally:
            if pool is not None:
                pool.shutdown()
        try:
            saved_path.write_text(json.dumps({"key": ck, "by_case": found}))
        except OSError:
            pass            # unwritable: classified again next time


def post_key(study: Path, model: Model, run: Path, levels: list[str]) -> str:
    """What results.json and the plots depend on: the solved run's results (by content), the whole model
    including limits and the export's view, and a view chosen after the solve (view.json)."""
    d = model.model_dump(by_alias=True, mode="json")
    d["source"] = {k: v for k, v in (d.get("source") or {}).items() if k != "timestamp"}
    view = file_digest(study / "view.json") if (study / "view.json").exists() else None
    results = {p.relative_to(run).as_posix(): file_digest(p) for p in run_outputs(run, levels)}
    return stage_key("post", {"model": d, "run": run.name, "results": results, "view": view, "levels": levels})


def write_reuse(study: Path, js) -> None:
    """<results>/reuse.json: what this job reused, for the report."""
    try:
        reuse = read_json(js.path).get("reuse", {})
    except (OSError, ValueError):
        reuse = {}
    _write(results_dir(study, mode_of(js)) / "reuse.json",
           {"schema_version": SCHEMA_VERSION, "job": js.dir.name, "reuse": reuse})


def stage_post(study: Path, js: JobState) -> None:
    model = chosen_cases(load_model(study), js)
    mode = mode_of(js)
    latest = latest_solve_file(study, mode)
    run = study / "run" / latest.read_text().strip() if latest.exists() else js.dir
    levels = [lv for lv, _ in levels_for(model, mode)]
    if not all((run / lv / "runs.json").exists() for lv in levels):
        raise Blocked([make("RESULTS_MISSING")])
    from .superpose import prepare_cases
    superposed = False
    for lv in levels:
        if any(r.get("superposed_from") for r in read_json(run / lv / "runs.json")):
            prepare_cases(run / lv, model)       # the load cases as sums of the per-load results, at today's sizes
            superposed = True
    if superposed:
        js.result("post: the load cases are sums of the per-load results (superposition: fast load-size changes)")
    cache = StageCache(study)
    k = post_key(study, model, run, levels)
    stage = "post_fast" if mode == FAST else "post"
    rec = None if force_requested(js) else cache.get(stage, k)
    if rec:
        replay(js, rec["data"])
        # the panes show the newer of the full and fast results: this run's are the newest, though reused
        (results_dir(study, mode) / "results.json").touch()
        note_reuse(js, "post", f"results and plots reused (from job {rec['data'].get('job', '?')})")
        write_reuse(study, js)
        return
    outer = js
    js = Recorder(js)
    out = results_dir(study, mode)
    _post(study, js, model, run, levels, mode)
    res = read_json(out / "results.json")
    outputs = [out / "results.json"] + [out / n for n in res.get("images", []) if (out / n).is_file()]
    cache.put(stage, k, outputs, {**js.data(), "job": outer.dir.name})
    write_reuse(study, outer)


def _post(study: Path, js, model: Model, run: Path, levels: list[str], mode: str = FULL) -> None:
    boxes = np.array([g["bbox"] for g in read_json(study / "geometry.json")["parts"].values()])
    model_size = float(np.linalg.norm(boxes[:, 3:].max(axis=0) - boxes[:, :3].min(axis=0)))
    per, infos, diags, frds = {}, {}, [], {}
    for lv in levels:
        js.activity(f"post: reading the {lv} results of run {run.name}")
        per[lv] = {"load_cases": {}, "buckling": {}}
        for r in read_json(run / lv / "runs.json"):
            rd = run / lv / r["dir"]
            if not (rd / "study.frd").exists():
                raise Blocked([make("RESULTS_MISSING")])
            info = read_json(rd / "deck_info.json")
            infos.setdefault(lv, info)
            tz = {k: np.asarray(v, dtype=np.int64) for k, v in info.get("tie_zone_nodes", {}).items()}
            if r["cases"]:
                with perf.span("summarise", level=lv, frd_bytes=(rd / "study.frd").stat().st_size,
                               dat_bytes=(rd / "study.dat").stat().st_size if (rd / "study.dat").exists() else 0):
                    part_res, d = summarise(model, info, rd / "study.frd", rd / "study.dat", tz, model_size,
                                            singular_by_imbalance=mode != FAST)
                per[lv]["load_cases"].update(part_res["load_cases"])
                for c in r["cases"]:
                    frds.setdefault(lv, {})[c] = rd
            else:
                d = []
            bk, bd = buckling(info, rd / "study.dat")
            for case, v in bk.items():
                v["deck"] = r["kind"]
                frds.setdefault(lv, {})[f"buckling:{case}"] = rd
            per[lv]["buckling"].update(bk)
            d += bd
            diags += [x for x in d if lv == levels[-1] or x.severity == "blocking"]
    raise_if_blocking(diags)
    reported = levels[-1]
    # static stress classification on the reported mesh (D34): membrane plus bending of member
    # stresses (notch peaks left to fatigue), and the receiving walls of bonded joints
    _stress_classification(study, model, reported, run, per[reported], js, mode)
    conv = None
    if len(levels) == 2:
        conv, cdiags = convergence(per["base"], per["refined"])
        diags += cdiags
    # The refined solve is reported; the base solve is the convergence reference (D12).
    res = per[reported]
    exc = read_json(study / "exceptions.json")["exceptions"] if (study / "exceptions.json").exists() else []
    open_exc = [e for e in exc if e["severity"] != "info"]
    manifest = read_json(study / "mesh" / ("manifest_fast.json" if mode == FAST else "manifest.json"))["parts"]
    stats = read_json(run / "solve_stats.json") if (run / "solve_stats.json").exists() else {}
    res.update({
        "schema_version": SCHEMA_VERSION,
        "job": run.name,
        "study": model.study.name,
        "reported_mesh": reported,
        "convergence": conv,
        "base": per["base"] if reported != "base" else None,
        "mesh": {lv: {"dof": infos[lv]["dof"], "elements": infos[lv].get("elements"),
                      "nodes": infos[lv]["dof"] // 3, "total_mass_t": infos[lv]["total_mass"]} for lv in levels}
        | {"instances": infos[reported]["instances"],
           "fallback_parts": sorted(p for p, m in manifest.items() if any(v.get("fallback") for v in m.values())),
           "meshers": sorted({v.get("mesher", "?") for m in manifest.values() for v in m.values()})},
        "timings": read_json(js.path).get("timings", {}) | {"solve_by_level": stats},
        "versions": {"asmfea": asmfea_version(), "solver": "CalculiX (see run log)",
                     "meshers": sorted({v.get("mesher", "?") for m in manifest.values() for v in m.values()})},
        "warnings": [d.model_dump() for d in diags],
        "superposed": any(r.get("superposed_from") for lv in levels for r in read_json(run / lv / "runs.json")),
        "hybrid": any(r.get("solver") == "hybrid" for lv in levels for r in read_json(run / lv / "runs.json")),
        # fast or full (ADDENDUM-FAST-RUN-MODE); a full run records the model it was solved for, so a report can
        # tell when the model has changed since
        "run_mode": mode,
        "model_key": model_key(model),
    })
    floating = sum(1 for e in exc if e["code"] == "PART_FLOATING")
    res["checks"] = checks(model, res, len(open_exc), floating=floating)
    # how accurate these results are likely to be, from how they were solved (D60)
    from .accuracy import grade
    plan = read_json(run / "plan.json") if (run / "plan.json").exists() else {}
    all_runs = [r for lv in levels for r in read_json(run / lv / "runs.json")]
    res["plan"] = plan or None
    res["accuracy"] = grade(mode, plan.get("fidelity"), conv, all_runs, res["mesh"]["fallback_parts"],
                            plan.get("favour"))
    js.verify(res["accuracy"]["summary"])
    out = results_dir(study, mode)
    _write(out / "results.json", res)
    js.add_diags(diags)
    for case, r in res["load_cases"].items():
        js.result(f"[{case}] max displacement {r['max_disp']:.4g} mm; "
                  + "; ".join(f"{p}: peak von Mises {v['peak_vm_excl_ties']:.4g} MPa (raw {v['peak_vm']:.4g})"
                              for p, v in r["parts"].items()))
        if r["reaction_balance_err"] is not None:
            js.verify(f"[{case}] reactions balance applied load to {100 * r['reaction_balance_err']:.3f} %")
    if conv:
        js.verify(f"convergence: worst change {conv['worst_change']:.1%} between base and refined meshes")
    for row in res["checks"]:
        js.verify(f"{row['status']} {row['check']}: {row['value']} (limit {row['limit']})")
    try:
        from .post.plots import render_all, views_for
        js.activity("post: rendering plot images")
        # the main view the plots are drawn from, so a client can tell when they are out of date
        t, u = views_for(model, study)["iso"]
        res["plot_view"] = {"toward_viewer": list(t), "up": list(u)}
        with perf.span("plots") as pinfo:
            images = render_all(study, model, frds[reported], res, level=reported,
                                on_progress=lambda s: js.activity(f"post: {s}"), mode=mode)
            pinfo["images"] = len(images)
        res["images"] = images
        _write(out / "results.json", res)
        js.result(f"post: {len(images)} plot images in {out.name}/")
    except ImportError:
        js.log("post: PyVista not installed (asmfea[report]); no plot images")


class Cancelled(Exception):
    pass


def stage_report(study: Path, js: JobState) -> None:
    if mode_of(js) == FAST:
        # a fast run is approximate: reports are written from full runs only (ADDENDUM-FAST-RUN-MODE)
        js.result("report: not written for a fast run (approximate results); do a full run for a report")
        return
    if not (study / "results" / "results.json").exists():
        raise Blocked([make("RESULTS_MISSING")])
    try:
        from .report import build_report
    except ImportError:
        js.log("report: python-docx not installed (asmfea[report]); no report written")
        return
    from .report import build_summary
    write_reuse(study, js)
    js.activity("report: writing the Word report")
    with perf.span("report"):
        path = build_report(study)
    js.result(f"report: {path.relative_to(study)}")
    js.activity("report: writing the half-page summary")
    with perf.span("summary"):
        js.result(f"report: {build_summary(study).relative_to(study)} (summary)")


STAGES = {"resolve": stage_resolve, "mesh": stage_mesh, "solve": stage_solve, "post": stage_post,
          "report": stage_report}
