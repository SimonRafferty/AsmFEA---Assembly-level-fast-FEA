"""Assemble one CalculiX input deck from part meshes, connections and study data.

Numbering: each part instance gets a node and element id offset; ids are written in
the assembly frame. deck_info.json records the offsets, set names and applied load
totals so post-processing never has to parse the deck.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from .. import defaults
from ..diagnostics import Diagnostic, make
from ..mesh import PartMesh
from ..mesh.elements import FACES, FACE_NODES, N_CORNER, body_load_weights, element_volumes
from ..model import Model

# Consistent nodal weights of a uniform traction (corners..., midsides...).
TRACTION_WEIGHTS = {3: [0, 0, 0, 1 / 3, 1 / 3, 1 / 3],
                    4: [-1 / 12] * 4 + [1 / 3] * 4}


@dataclass
class Instance:
    part_id: str
    set_name: str              # P1, P2, ... (CalculiX set names stay short and safe)
    mesh: PartMesh
    T: np.ndarray              # 4x4 part -> assembly
    node0: int                 # global id = node0 + local id
    elem0: int                 # global element id = elem0 + 1 + element row
    material: str
    X: np.ndarray = field(default=None)   # assembly-frame coordinates, row i = local id i+1
    _areas: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        P = self.mesh.nodes
        self.X = P @ self.T[:3, :3].T + self.T[:3, 3]

    def face_areas(self, face_idx: int) -> np.ndarray:
        """Areas of the element faces on a geometry face, flat approximation as _face_geom, in
        face_rows order; computed at once per element type and face number and kept (a tie asks for
        them up to six times: 7 s of a 10 s deck, INVESTIGATION-001)."""
        a = self._areas.get(face_idx)
        if a is not None:
            return a
        rows = self.face_rows(face_idx)
        a = np.zeros(len(rows))
        start = 0
        for etype, conn in self.mesh.blocks:
            in_block = (rows[:, 0] >= start) & (rows[:, 0] < start + len(conn))
            for fnum in np.unique(rows[in_block, 1]):
                sel = np.nonzero(in_block & (rows[:, 1] == fnum))[0]
                nc = len(FACES[etype][fnum - 1])
                P = self.X[conn[rows[sel, 0] - start][:, FACE_NODES[etype][fnum - 1][:nc]] - 1]
                if nc == 3:
                    cr = 0.5 * np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0])
                else:
                    cr = 0.5 * np.cross(P[:, 2] - P[:, 0], P[:, 3] - P[:, 1])
                a[sel] = np.linalg.norm(cr, axis=1)
            start += len(conn)
        self._areas[face_idx] = a
        return a

    def face_rows(self, face_idx: int) -> np.ndarray:
        fe = self.mesh.face_elem
        return fe[fe[:, 0] == face_idx][:, 1:]

    def element(self, row: int) -> tuple[str, np.ndarray]:
        for etype, conn in self.mesh.blocks:
            if row < len(conn):
                return etype, conn[row]
            row -= len(conn)
        raise IndexError(row)

    def face_nodes(self, face_idx: int) -> np.ndarray:
        """Local ids of every node on a geometry face (corners and midsides)."""
        ids = []
        for row, fnum in self.face_rows(face_idx):
            etype, conn = self.element(int(row))
            ids.extend(conn[FACE_NODES[etype][fnum - 1]])
        return np.unique(np.asarray(ids, dtype=np.int64))

    def face_elements(self, face_idx: int) -> list[tuple[int, int, str, np.ndarray]]:
        """[(global element id, face number, etype, local conn)]."""
        out = []
        for row, fnum in self.face_rows(face_idx):
            etype, conn = self.element(int(row))
            out.append((self.elem0 + 1 + int(row), int(fnum), etype, conn))
        return out


def _ids(f, ids, per_line=12):
    ids = [int(i) for i in ids]
    for i in range(0, len(ids), per_line):
        f.write(", ".join(str(v) for v in ids[i:i + per_line]) + "\n")


def _face_geom(inst: Instance, etype: str, conn: np.ndarray, fnum: int):
    """(area, unit outward normal, full node local ids) of an element face, flat approx."""
    nodes = conn[FACE_NODES[etype][fnum - 1]]
    nc = len(FACES[etype][fnum - 1])
    P = inst.X[nodes[:nc] - 1]
    if nc == 3:
        cr = 0.5 * np.cross(P[1] - P[0], P[2] - P[0])
    else:
        cr = 0.5 * np.cross(P[2] - P[0], P[3] - P[1])
    a = float(np.linalg.norm(cr))
    # CalculiX face corner order gives an outward normal by the right-hand rule for
    # tets (S1 = 1-2-3 points inward); check against the element centroid instead.
    elem_c = inst.X[conn[:N_CORNER[etype]] - 1].mean(axis=0)
    n = cr / a
    if np.dot(n, P.mean(axis=0) - elem_c) < 0:
        n = -n
    return a, n, nodes


def measured_wall(inst: Instance) -> Optional[float]:
    """post.sections.estimate_wall_thickness of an instance, kept beside its cached mesh
    (<mesh key>.wall.json): it depends only on the mesh and the instance's rotation (its thin test
    uses the assembly-frame box), and took 5.6 s of every deck of a 24-part welded chassis (INVESTIGATION-001)."""
    from ..post.sections import PartTopology, estimate_wall_thickness
    from ..stagecache import code_fingerprint, defaults_digest, digest
    src = inst.mesh.source
    path = src.with_name(src.stem + ".wall.json") if src else None
    tag = digest({"code": code_fingerprint(), "defaults": defaults_digest()})
    rot = digest(np.round(inst.T[:3, :3], 9).tolist())
    cache = {}
    if path and path.is_file():
        try:
            d = json.loads(path.read_text())
            cache = d.get("by_rotation", {}) if d.get("tag") == tag else {}
        except (OSError, ValueError):
            cache = {}
    if rot in cache:
        return cache[rot]
    t = estimate_wall_thickness(inst, PartTopology(inst))
    if path:
        cache[rot] = t
        try:
            path.write_text(json.dumps({"tag": tag, "by_rotation": cache}))
        except OSError:
            pass          # unwritable: measured again next time
    return t


@dataclass
class DeckResult:
    path: Path
    info: dict
    diagnostics: list[Diagnostic]


def scales_in_buckling(ld) -> bool:
    """Loads multiplied by the buckling factor. Self-weight is a preload unless the load says otherwise."""
    if ld.scale_in_buckling is not None:
        return ld.scale_in_buckling
    return ld.type != "gravity"


def _in_strip(X: np.ndarray, r: dict) -> np.ndarray:
    """Which points lie within a weld strip (``{"segments", "width"}``, DECISIONS D22)."""
    from ..resolve.welds import distance_to_segments
    segs = np.asarray(r["segments"], float).reshape(-1, 2, 3)
    return distance_to_segments(X, segs) <= r["width"] + 1e-6


def _contact_faces(inst: Instance, idx: int, c: dict) -> list[tuple[int, int, str, np.ndarray]]:
    """The element faces of a contact side, without those whose centre lies in a weld strip
    cut from the interface (``exclude``): the weld bonds them instead."""
    faces = inst.face_elements(idx)
    excl = c.get("exclude") or []
    if not excl:
        return faces
    centres = np.array([inst.X[cn[FACE_NODES[e][fn - 1]] - 1].mean(axis=0) for _, fn, e, cn in faces])
    out = np.zeros(len(faces), bool)
    for r in excl:
        out |= _in_strip(centres, r)
    return [f for f, o in zip(faces, out) if not o]


def _tie_nodes(inst: Instance, idx: int, c: dict) -> np.ndarray:
    """Local ids of a tie's slave nodes: the whole face, or for a weld strip (``region``) the
    nodes within the bead size of the weld path, so the bonded width follows the bead size
    and not the element size."""
    nodes = inst.face_nodes(idx)
    if c.get("region") is None:
        return nodes[_in_footprint(inst.X[nodes - 1], c)]
    return nodes[_in_strip(inst.X[nodes - 1], c["region"])]


def _in_footprint(X: np.ndarray, c: dict) -> np.ndarray:
    """Which points of a planar tie side lie over the touching area: within the gap of the
    interface plane and inside the oriented box of its boundary samples. A tie must not claim
    nodes it cannot tie: a node is the dependent of one constraint only, and a whole face claimed
    by its first tie left the next ties on it with none (found live: a plate meeting stand-off
    edges floated)."""
    S = np.asarray(c.get("samples") or [], float)
    if c.get("geometry") != "plane" or c.get("normal") is None or len(S) < 3 or c.get("butt"):
        return np.ones(len(X), bool)
    n = np.asarray(c["normal"], float)
    n /= np.linalg.norm(n)
    m = S.mean(axis=0)
    grow = defaults.GAP_TOL + 1e-3
    near = np.abs((X - m) @ n) <= abs(float(c.get("gap", 0.0))) + grow
    P = (S - m) - np.outer((S - m) @ n, n)
    _, _, vt = np.linalg.svd(P, full_matrices=False)
    axes = vt[:2]                                    # in-plane axes of the footprint's oriented box
    s2, x2 = P @ axes.T, (X - m) @ axes.T
    lo, hi = s2.min(axis=0) - grow, s2.max(axis=0) + grow
    return near & np.all((x2 >= lo) & (x2 <= hi), axis=1)


def _nodes_of(faces) -> list[int]:
    return sorted({int(n) for _, fnum, e, cn in faces for n in cn[FACE_NODES[e][fnum - 1]]})


def _face_centroid(inst: Instance, idx: int) -> np.ndarray:
    tot, acc = 0.0, np.zeros(3)
    for _, fn, e, cn in inst.face_elements(idx):
        a, _, nodes = _face_geom(inst, e, cn, fn)
        nc = len(FACES[e][fn - 1])
        acc += a * inst.X[nodes[:nc] - 1].mean(axis=0)
        tot += a
    return acc / tot


def _eps(i: int, j: int, k: int) -> int:
    """Levi-Civita symbol."""
    return int((i - j) * (j - k) * (k - i) / 2)


def _perp_basis(a: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a = a / np.linalg.norm(a)
    t = np.eye(3)[int(np.argmin(np.abs(a)))]
    p = np.cross(a, t)
    p /= np.linalg.norm(p)
    return p, np.cross(a, p)


def plan_joints(model: Model, connections: list[dict], by_part: dict, first_node: int) -> tuple[list[dict], list]:
    """Revolute and sliding joints: a rigid body on each side, joined by equations.

    revolute: both rigid bodies share one reference node (translations equal); the two
              rotations perpendicular to the axis are made equal by equations.
    sliding:  both share one rotation node (rotations equal); the two translations
              perpendicular to the slide direction are made equal by equations.
    The axis/direction comes from the override's params.direction, else the cylindrical
    interface, else the mates.
    """
    diags = []
    groups: dict[tuple, list[dict]] = {}
    for c in connections:
        if c["type"] in ("revolute", "sliding", "hinge", "screw") and c.get("sides"):
            # a hinge (concentric mate on cylinders) is its own joint on its own axis: two hinges on parallel
            # axes between the same parts must not merge into one
            key = c["id"] if c["type"] in ("hinge", "screw") else ""
            groups.setdefault((c["type"], tuple(sorted(c["parts"][:2])), key), []).append(c)
    mates = {m.id: m for m in model.mates}
    out, nxt = [], first_node
    for (kind, (pa, pb), _), cs in sorted(groups.items()):
        if pa not in by_part or pb not in by_part:
            continue
        # the joint's own axis line: a hinge's (on a cylinder, or a round profile's normal), else a cylinder's
        cyl = next((c for c in cs if c.get("axis") is not None and c.get("axis_point") is not None), None)
        mate_dirs = [mates[m] for c in cs for m in c.get("evidence", {}).get("mates", []) if m in mates
                     and mates[m].direction is not None]
        given = next((c["params"]["direction"] for c in cs if c.get("params", {}).get("direction")), None)
        if given is not None:
            # a direction chosen by the user (override): slide along it, joint at the interface
            axis = np.asarray(given, float)
            at = np.mean([np.mean(c.get("samples", [[0, 0, 0]]), axis=0) for c in cs], axis=0)
        elif cyl is not None:
            axis = np.asarray(cyl["axis"], float)
            p0 = np.asarray(cyl["axis_point"], float)
            ts = [float(np.dot(np.asarray(s) - p0, axis)) for s in cyl.get("samples", [])] or [0.0]
            at = p0 + axis * float(np.mean(ts))
        elif mate_dirs:
            axis = np.asarray(mate_dirs[0].direction, float)
            at = np.asarray(mate_dirs[0].point if mate_dirs[0].point is not None else
                            np.mean([np.mean(c.get("samples", [[0, 0, 0]]), axis=0) for c in cs], axis=0))
        else:
            diags.append(make("JOINT_AXIS_UNKNOWN", where={"parts": [pa, pb], "connections": [c["id"] for c in cs]},
                              parts=f"{pa} and {pb}", joint=kind))
            continue
        axis = axis / np.linalg.norm(axis)
        sides = {pa: set(), pb: set()}
        for c in cs:
            for s in c["sides"]:
                inst = by_part[s["part"]]
                sides[s["part"]].update(int(inst.node0 + n) for n in inst.face_nodes(s["face"]))
        # Side A is a *RIGID BODY (ref, rot). Side B's nodes follow A's rigid motion plus
        # one free coordinate (rotation about the axis, or slide along it) held by a dummy
        # node. CalculiX 2.22 crashes on *EQUATIONs between two rigid bodies' reference
        # nodes (chained MPCs, milestone 9), so B gets direct equations instead.
        ref_a, rot_a, free = nxt, nxt + 1, nxt + 2
        new = [ref_a, rot_a, free]
        bodies = [(pa, ref_a, rot_a), (pb, None, free)]
        nxt += 3
        out.append({"kind": kind, "parts": [pa, pb], "connections": [c["id"] for c in cs], "axis": axis.tolist(),
                    "at": at.tolist(), "new_nodes": new, "bodies": bodies, "nodes": sides,
                    "locked": any(c.get("locked") for c in cs),
                    "lead": next((float(c["lead"]) for c in cs if c.get("lead")), 0.0)})
    return out, diags


def write_deck(path: Path, model: Model, instances: list[Instance], connections: list[dict],
               face_map: dict[str, tuple[str, int]], load_cases: list[str], threads_hint: int = 0,
               bolts: Optional[list[dict]] = None, stabilise: Optional[list[str]] = None,
               buckling: bool = True) -> DeckResult:
    """bolts: preloaded-bolt plans (pipeline.plan_preloaded_bolts); stabilise: parts held
    only by contact, which get weak springs to ground; buckling=False leaves out the
    buckling steps (contact decks)."""
    bolts = bolts or []
    stabilise = stabilise or []
    diags: list[Diagnostic] = []
    by_part = {i.part_id: i for i in instances}
    info: dict = {"instances": [], "load_cases": {}, "ties": [], "fixtures": {}, "steps": [], "buckling": {},
                  "contacts": [], "bolts": [], "springs": {}}
    contacts = [c for c in connections if c["type"] == "contact" and c.get("sides")]
    nonlinear = bool(contacts or bolts)

    def faces_of(fid: str):
        part, idx = face_map[fid]
        return by_part[part], idx

    # Pinned fixtures: the face is a rigid body whose reference node (at the face
    # centroid) is held in the chosen translations and rotations; rotation is free
    # otherwise. Their extra nodes are numbered after every mesh node.
    next_node = max((i.node0 + len(i.X) for i in instances), default=0) + 1
    pins = []
    for fx in model.fixtures:
        if fx.type != "pinned":
            continue
        ids, cen, area = set(), np.zeros(3), 0.0
        for fid in fx.faces:
            inst, idx = faces_of(fid)
            ids.update(int(inst.node0 + n) for n in inst.face_nodes(idx))
            c = _face_centroid(inst, idx)
            a = float(inst.face_areas(idx).sum())
            cen += a * c
            area += a
        pins.append({"fx": fx, "nodes": sorted(ids), "ref": next_node, "rot": next_node + 1,
                     "at": (cen / area).tolist()})
        next_node += 2

    joints, jdiags = plan_joints(model, connections, by_part, next_node)
    diags += jdiags
    for j in joints:
        next_node = max(next_node, *j["new_nodes"]) + 1
    # Preloaded bolts: two nodes on the axis at the bearing faces (head, nut).
    for b in bolts:
        b["n1"], b["n2"] = next_node, next_node + 1
        next_node += 2
    next_elem = max((i.elem0 + i.mesh.n_elements for i in instances), default=0) + 1

    with open(path, "w") as f:
        f.write("*HEADING\nasmfea deck\n")
        # Nodes and elements per instance
        f.write("*NODE, NSET=NALL\n")
        for inst in instances:
            for i, x in enumerate(inst.X):
                f.write(f"{inst.node0 + i + 1}, {x[0]:.9g}, {x[1]:.9g}, {x[2]:.9g}\n")
        for p in pins:
            for n in (p["ref"], p["rot"]):
                f.write(f"{n}, {p['at'][0]:.9g}, {p['at'][1]:.9g}, {p['at'][2]:.9g}\n")
        for j in joints:
            for n in j["new_nodes"]:
                f.write(f"{n}, {j['at'][0]:.9g}, {j['at'][1]:.9g}, {j['at'][2]:.9g}\n")
        for b in bolts:
            for n, x in ((b["n1"], b["head"]["point"]), (b["n2"], b["nut"]["point"])):
                f.write(f"{n}, {x[0]:.9g}, {x[1]:.9g}, {x[2]:.9g}\n")
        for inst in instances:
            row = 0
            names = []
            for b, (etype, conn) in enumerate(inst.mesh.blocks):
                name = f"{inst.set_name}_{b}"
                names.append(name)
                f.write(f"*ELEMENT, TYPE={etype}, ELSET={name}\n")
                for r, nodes in enumerate(conn):
                    vals = [str(inst.elem0 + 1 + row + r)] + [str(inst.node0 + int(n)) for n in nodes]
                    for j in range(0, len(vals), 16):
                        tail = "," if j + 16 < len(vals) else ""
                        f.write(", ".join(vals[j:j + 16]) + tail + "\n")
                row += len(conn)
            f.write(f"*ELSET, ELSET={inst.set_name}\n" + "\n".join(names) + "\n")
            info["instances"].append({"part": inst.part_id, "set": inst.set_name, "node0": inst.node0,
                                      "elem0": inst.elem0, "n_nodes": int(len(inst.X)),
                                      "n_elements": int(inst.mesh.n_elements), "material": inst.material})
        f.write("*ELSET, ELSET=EALL\n" + "\n".join(i.set_name for i in instances) + "\n")
        # each part's wall thickness, measured from its mesh where the model gives none (a weldment profile
        # the add-in could not read, found live): joint and membrane-plus-bending assessment need it (D34)
        info["wall_thickness"] = {}
        for inst in instances:
            part = model.part(inst.part_id) if hasattr(model, "part") else next(p for p in model.parts if p.id == inst.part_id)
            if getattr(part, "sheet", None) is not None and part.sheet.thickness:
                continue
            if getattr(part, "member", None) is not None and isinstance(part.member.profile, dict) and part.member.profile.get("t"):
                continue
            t_geo = measured_wall(inst)
            if t_geo:
                info["wall_thickness"][inst.part_id] = t_geo

        # Bolts: an axial spring of the bolt's own stiffness EA/L between the bearing points,
        # plus the preload as a constant force pair, so tension = preload + k x elongation.
        # (A beam *PRE-TENSION SECTION was silently dropped by CalculiX 2.22 when its end
        # nodes are coupling reference nodes; milestone 10, D14.)
        for k, b in enumerate(bolts, start=1):
            b["elem"] = next_elem
            next_elem += 1
            grip = float(np.linalg.norm(np.asarray(b["head"]["point"]) - np.asarray(b["nut"]["point"])))
            b["k"] = model.material(b["material"]).E * np.pi * b["d"] ** 2 / 4 / max(grip, 1e-9)
            f.write(f"*ELEMENT, TYPE=SPRINGA, ELSET=BOLT{k}\n{b['elem']}, {b['n1']}, {b['n2']}\n")

        # Weak springs to ground on parts held only by contact (stated in the report).
        spring_sets = []
        for pid in stabilise:
            inst = by_part.get(pid)
            if inst is None:
                continue
            E = model.material(inst.material).E
            size = float(np.linalg.norm(inst.X.max(axis=0) - inst.X.min(axis=0)))
            k_node = defaults.STAB_SPRING_FRACTION * E * size / len(inst.X)
            for d in (1, 2, 3):
                name = f"STB{inst.set_name}_{d}"
                f.write(f"*ELEMENT, TYPE=SPRING1, ELSET={name}\n")
                for i in range(len(inst.X)):
                    f.write(f"{next_elem}, {inst.node0 + i + 1}\n")
                    next_elem += 1
                spring_sets.append((name, d, k_node))
            info["springs"][pid] = {"k_per_node": k_node, "nodes": len(inst.X), "node0": inst.node0}

        # Materials and sections
        for mat in sorted({i.material for i in instances}):     # sorted: the same inputs give the same deck
            m = model.material(mat)
            f.write(f"*MATERIAL, NAME={_safe(mat)}\n*ELASTIC\n{m.E:.9g}, {m.nu:.9g}\n*DENSITY\n{m.density:.9g}\n")
        for inst in instances:
            f.write(f"*SOLID SECTION, ELSET={inst.set_name}, MATERIAL={_safe(inst.material)}\n")
        for k, b in enumerate(bolts, start=1):
            # SPRINGA: second line blank, then the stiffness (manual *SPRING)
            f.write(f"*SPRING, ELSET=BOLT{k}\n\n{b['k']:.9g}\n")
        for name, d, k_node in spring_sets:
            f.write(f"*SPRING, ELSET={name}\n{d}\n{k_node:.9g}\n")

        # Fixtures: node sets per fixture (pinned faces become rigid bodies)
        fixed_nodes: set[int] = set()   # nodes that may not be tie slaves
        fix_sets = []                   # SPC'd sets: (name, fixture, node ids)
        reaction_sets = []
        for fx in model.fixtures:
            if fx.type == "pinned":
                continue
            ids = set()
            for fid in fx.faces:
                inst, idx = faces_of(fid)
                ids.update(int(inst.node0 + n) for n in inst.face_nodes(idx))
            name = f"FIX{len(fix_sets) + 1}"
            fix_sets.append((name, fx, sorted(ids)))
            reaction_sets.append(name)
            fixed_nodes.update(ids)
            f.write(f"*NSET, NSET={name}\n")
            _ids(f, sorted(ids))
            info["fixtures"][fx.id] = {"set": name, "nodes": len(ids)}
        for k, p in enumerate(pins, start=1):
            name = f"PIN{k}"
            f.write(f"*NSET, NSET={name}\n")
            _ids(f, p["nodes"])
            # RF is the internal element force, so a rigid body's reference node (no
            # elements) reports zero: the reaction is read on the face nodes instead.
            reaction_sets.append(name)
            fixed_nodes.update(p["nodes"])
            info["fixtures"][p["fx"].id] = {"set": name, "nodes": len(p["nodes"]), "ref_node": p["ref"],
                                            "at": p["at"]}
        if reaction_sets:
            # Explicit unique node list: a union of named sets counts nodes shared by two
            # fixture faces twice in the printed totals (milestone 6 plate case).
            all_support = sorted({n for _, _, ids in fix_sets for n in ids} | {n for p in pins for n in p["nodes"]})
            f.write("*NSET, NSET=FIXALL\n")
            _ids(f, all_support)

        # Separating contact (CalculiX manual *CONTACT PAIR, *SURFACE BEHAVIOR, *FRICTION):
        # face-to-face penalty contact, linear pressure-overclosure, optional friction.
        def mean_face_size(inst, idx):
            areas = inst.face_areas(idx)
            return float(np.sqrt(np.mean(areas))) if len(areas) else 1.0

        for k, c in enumerate(contacts, start=1):
            (pa, fa), (pb, fb) = [(s["part"], s["face"]) for s in c["sides"]]
            ia, ib = by_part[pa], by_part[pb]
            if mean_face_size(ia, fa) <= mean_face_size(ib, fb):
                (si, sf), (mi, mf) = (ia, fa), (ib, fb)
            else:
                (si, sf), (mi, mf) = (ib, fb), (ia, fa)
            slave_faces = _contact_faces(si, sf, c)
            if not slave_faces:
                continue        # a weld strip covers the whole interface
            for tag, faces in (("S", slave_faces), ("M", mi.face_elements(mf))):
                f.write(f"*SURFACE, NAME=C{k}{tag}, TYPE=ELEMENT\n")
                for eid, fnum, _, _ in faces:
                    f.write(f"{eid}, S{fnum}\n")
            E = min(model.material(si.material).E, model.material(mi.material).E)
            K = defaults.CONTACT_PENALTY_FACTOR * E / mean_face_size(si, sf)
            mu = float(c.get("friction") or 0.0)
            f.write(f"*SURFACE INTERACTION, NAME=SI{k}\n*SURFACE BEHAVIOR, PRESSURE-OVERCLOSURE=LINEAR\n{K:.6g}\n")
            if mu > 0:
                f.write(f"*FRICTION\n{mu:.6g}, {defaults.CONTACT_STICK_FRACTION * K:.6g}\n")
            f.write(f"*CONTACT PAIR, INTERACTION=SI{k}, TYPE=SURFACE TO SURFACE\nC{k}S, C{k}M\n")
            # for the opening (post): CalculiX's contact output keeps penetrations only, so the
            # gap is measured from displacements, slave node against nearest master node,
            # along the master face's outward normal
            nrm = np.zeros(3)
            for _, fn, e, cn in mi.face_elements(mf):
                a, n, _ = _face_geom(mi, e, cn, fn)
                nrm += a * n
            info["contacts"].append({"connection": c["id"], "name": f"C{k}", "slave_part": si.part_id,
                                     "master_part": mi.part_id, "friction": mu, "penalty": K,
                                     "slave_nodes": [int(si.node0 + n) for n in _nodes_of(slave_faces)],
                                     "master_nodes": [int(mi.node0 + n) for n in mi.face_nodes(mf)],
                                     "master_normal": (nrm / max(np.linalg.norm(nrm), 1e-300)).tolist()})

        # Preloaded bolts: an axial spring between two reference nodes. Each reference node
        # follows the area-weighted mean displacement of the bearing annulus under the head or
        # the nut through explicit *EQUATIONs (reference node dependent). The preload is a
        # force pair in every step. D14: CalculiX 2.22 drops a beam pre-tension section whose
        # ends are coupling reference nodes, and a *DISTRIBUTING coupling's reference node
        # stayed at zero with a spring attached (milestone 10 probe), so neither is used.
        for k, b in enumerate(bolts, start=1):
            axis = np.asarray(b["axis"], float)
            for end, node in (("head", b["n1"]), ("nut", b["n2"])):
                inst = by_part[b[end]["part"]]
                centre = np.asarray(b[end]["point"], float)
                rows = []
                if b[end]["face"] is None:
                    diags.append(make("BOLT_NO_BEARING_FACE", where={"parts": [b[end]["part"]],
                                                                     "connections": [b["id"]]}, bolt=b["id"], end=end))
                    continue
                for eid, fnum, e, cn in inst.face_elements(b[end]["face"]):
                    nodes = cn[FACE_NODES[e][fnum - 1]]
                    c = inst.X[nodes - 1].mean(axis=0) - centre
                    r = np.linalg.norm(c - np.dot(c, axis) * axis)
                    if r <= b["r_bearing"]:
                        rows.append((eid, fnum))
                if not rows:
                    diags.append(make("BOLT_NO_BEARING_FACE", where={"parts": [b[end]["part"]], "connections": [b["id"]]},
                                      bolt=b["id"], end=end))
                    continue
                # nodal weights: each annulus face shares its area equally among its nodes
                weight: dict[int, float] = defaultdict(float)
                for eid, fn in rows:
                    e_, cn_ = inst.element(eid - inst.elem0 - 1)
                    a, _, fnodes = _face_geom(inst, e_, cn_, fn)
                    for n in fnodes:
                        weight[int(inst.node0 + n)] += a / len(fnodes)
                total = sum(weight.values())
                b[f"{end}_nodes"] = sorted(weight)
                # share of this end's force that goes straight into fixed nodes (RF misses it)
                b[f"{end}_fixed"] = sum(w for n, w in weight.items() if n in fixed_nodes) / total
                for d in (1, 2, 3):
                    terms = [(node, d, 1.0)] + [(n, d, -w / total) for n, w in sorted(weight.items())]
                    f.write(f"*EQUATION\n{len(terms)}\n")
                    flat = [f"{nn}, {dd}, {c:.12g}" for nn, dd, c in terms]
                    for i0 in range(0, len(flat), 4):  # at most 4 terms per line
                        f.write(", ".join(flat[i0:i0 + 4]) + "\n")
            info["bolts"].append({"connection": b["id"], "fastener": b["fastener"], "head_node": b["n1"],
                                  "nut_node": b["n2"], "preload": b["preload"], "axis": b["axis"], "k": b["k"],
                                  "head_nodes": b.get("head_nodes", []), "nut_nodes": b.get("nut_nodes", []),
                                  "head_fixed": b.get("head_fixed", 0.0), "nut_fixed": b.get("nut_fixed", 0.0)})
        if bolts:
            f.write("*NSET, NSET=BOLTNODES\n" + "\n".join(f"{b['n1']}, {b['n2']}" for b in bolts) + "\n")

        def bolt_pairs():
            """The preload as a force pair: head pulled towards the nut and vice versa."""
            lines = []
            for b in bolts:
                a = np.asarray(b["axis"], float)   # from nut towards head
                for node, sign in ((b["n1"], -1.0), (b["n2"], 1.0)):
                    for dd in range(3):
                        if abs(a[dd]) > 1e-12:
                            lines.append(f"{node}, {dd + 1}, {sign * b['preload'] * a[dd]:.9g}\n")
            return lines

        slaved: set[int] = set()
        # Internal ties between the regions of one part (milestone 4: a swept span and the
        # end region beside it). They go first, so a node on the edge of a cut plane stays
        # tied to its own part; a connection tie that also claims it then drops it.
        info["internal_ties"] = []
        for inst in instances:
            for k, (sidx, midx) in enumerate(inst.mesh.ties, start=1):
                name = f"{inst.set_name}R{k}"
                slave = [int(inst.node0 + n) for n in inst.face_nodes(int(sidx))]
                slave = [n for n in slave if n not in slaved and n not in fixed_nodes]
                slaved.update(slave)
                masters = inst.face_elements(int(midx))
                info["internal_ties"].append({"part": inst.part_id, "name": name, "slave_nodes": len(slave),
                                              "master_faces": len(masters)})
                if not slave or not masters:
                    continue
                f.write(f"*NSET, NSET={name}S\n")
                _ids(f, slave)
                f.write(f"*SURFACE, NAME={name}SLAVE, TYPE=NODE\n{name}S\n")
                f.write(f"*SURFACE, NAME={name}MASTER, TYPE=ELEMENT\n")
                for eid, fnum, _, _ in masters:
                    f.write(f"{eid}, S{fnum}\n")
                # the two regions share the cut plane exactly; the tolerance only absorbs round-off
                f.write(f"*TIE, NAME={name}, POSITION TOLERANCE=0.01, ADJUST=NO\n{name}SLAVE, {name}MASTER\n")

        # Ties: finer side is the slave (nodal surface), coarser side the master.
        bond_tol = defaults.GAP_TOL if model.study.bond_tolerance is None else float(model.study.bond_tolerance)
        section_sets: list[str] = []      # tie slave sets whose nodal forces are printed (D33)
        for k, c in enumerate(c for c in connections if c["type"] == "tie"):
            (pa, fa), (pb, fb) = [(s["part"], s["face"]) for s in c["sides"]]
            ia, ib = by_part[pa], by_part[pb]
            size = lambda inst, idx: float(np.mean(inst.face_areas(idx)))  # noqa: E731
            area = lambda inst, idx: float(np.sum(inst.face_areas(idx)))  # noqa: E731
            butt = c.get("butt")
            master_faces = None
            section = bool(butt)      # the slave face cuts through its member (an end or an edge): D33
            if butt:
                # a weldment butt joint (D29): the end face is tied whole, to the flat and the corner radii
                si, sf = by_part[butt["slave"]["part"]], int(butt["slave"]["face"])
                mi = by_part[butt["masters"][0]["part"]]
                master_faces = [int(m["face"]) for m in butt["masters"]]
                mf = master_faces[0]
            elif (area(ia, fa) >= defaults.TIE_AREA_RATIO * area(ib, fb)) != (area(ib, fb) >= defaults.TIE_AREA_RATIO * area(ia, fa)):
                # an edge on a face (very different sizes): the small face is the slave, so all its
                # nodes lie over the big one; a coarse big face as slave had none in a 3 mm strip
                (si, sf), (mi, mf) = ((ib, fb), (ia, fa)) if area(ia, fa) > area(ib, fb) else ((ia, fa), (ib, fb))
                section = True
            elif size(ia, fa) <= size(ib, fb):
                (si, sf), (mi, mf) = (ia, fa), (ib, fb)
            else:
                (si, sf), (mi, mf) = (ib, fb), (ia, fa)
            slave = [int(si.node0 + n) for n in _tie_nodes(si, sf, c)]
            # A node may be the dependent of one constraint only: drop nodes already
            # slaved by another tie or held by a fixture.
            slave = [n for n in slave if n not in slaved and n not in fixed_nodes]
            slaved.update(slave)
            name = f"T{k + 1}"
            tie_rec = {"connection": c["id"], "name": name, "slave_part": si.part_id,
                       "master_part": mi.part_id, "slave_nodes": len(slave),
                       "weld": c.get("evidence", {}).get("weld")}
            if section and slave and c.get("region") is None:
                # structural stress at the joint (D33): the slave face is a section through its member;
                # its nodal forces (printed as RF) and each node's share of the face area give the line
                # force and moment along the joint
                share: dict[int, float] = {}
                keep = set(slave)
                for _, fn, e, cn in si.face_elements(sf):
                    a_f, _, fnodes = _face_geom(si, e, cn, fn)
                    for n in fnodes:            # an equal, positive share per face node: for joint length
                        g = int(si.node0 + n)
                        if g in keep:
                            share[g] = share.get(g, 0.0) + a_f / len(fnodes)
                # the section's facets (global node ids, CalculiX face order: corners then midsides), so
                # post can turn the consistent nodal forces back into the traction field (D33)
                facets = [[int(si.node0 + n) for n in cn[FACE_NODES[e][fn - 1]]] for _, fn, e, cn in si.face_elements(sf)]
                tie_rec.update(section=True, node_area={str(g): round(v, 6) for g, v in share.items()}, facets=facets)
                section_sets.append(f"{name}S")
            info["ties"].append(tie_rec)
            if not slave:
                continue
            f.write(f"*NSET, NSET={name}S\n")
            _ids(f, slave)
            f.write(f"*SURFACE, NAME={name}SLAVE, TYPE=NODE\n{name}S\n")
            f.write(f"*SURFACE, NAME={name}MASTER, TYPE=ELEMENT\n")
            for face in (master_faces or [mf]):
                for eid, fnum, _, _ in mi.face_elements(face):
                    f.write(f"{eid}, S{fnum}\n")
            tol = (float(butt["tolerance"]) if butt else bond_tol) + 0.05
            # A butt joint across a gap (D31): ADJUST=YES moves the butting end's nodes onto the surface
            # it meets, as if the member were extended to it (the gap filled by weld), instead of rigid
            # links across the gap, whose ends gave a false stress peak.
            adjust = "YES" if butt and defaults.BUTT_ADJUST else "NO"
            f.write(f"*TIE, NAME={name}, POSITION TOLERANCE={tol:.6g}, ADJUST={adjust}\n{name}SLAVE, {name}MASTER\n")

        # Faces that slide but do not separate (a hinge's parts): each slave node keeps the master face's
        # displacement along the face normal, by one equation, and is free in the face's plane
        info["slides"] = []
        for k, c in enumerate(c for c in connections if c["type"] == "slide" and c.get("sides")):
            (pa, fa), (pb, fb) = [(s["part"], s["face"]) for s in c["sides"]]
            ia, ib = by_part[pa], by_part[pb]
            mean = lambda inst, idx: float(np.mean(inst.face_areas(idx)))  # noqa: E731
            (si, sf), (mi, mf) = ((ia, fa), (ib, fb)) if mean(ia, fa) <= mean(ib, fb) else ((ib, fb), (ia, fa))
            slave = [int(si.node0 + n) for n in _tie_nodes(si, sf, c)]
            slave = [n for n in slave if n not in slaved and n not in fixed_nodes]
            n_eq = _write_slide(f, c, si, mi, mf, slave, bond_tol)
            slaved.update(slave)
            info["slides"].append({"connection": c["id"], "slave_part": si.part_id, "master_part": mi.part_id,
                                   "slave_nodes": n_eq})

        for k, p in enumerate(pins, start=1):
            f.write(f"*RIGID BODY, NSET=PIN{k}, REF NODE={p['ref']}, ROT NODE={p['rot']}\n")

        # Joints: rigid reference on each side, equations leave the free motion free.
        info["joints"] = []
        coords = {}
        for inst in instances:
            coords[inst.part_id] = inst
        joint_dummies = []
        subst = _coupling_substitutions(joints, connections)
        info["couplings"] = [{"connection": cid, "joints": js} for cid, js in subst.pop("_records", [])]
        for k, j in enumerate(joints, start=1):
            (pa, ra, qa), (pb, _, free) = j["bodies"]
            nodes_a = [n for n in sorted(j["nodes"][pa]) if n not in slaved and n not in fixed_nodes]
            fixed_nodes.update(nodes_a)
            f.write(f"*NSET, NSET=J{k}A\n")
            _ids(f, nodes_a)
            f.write(f"*RIGID BODY, NSET=J{k}A, REF NODE={ra}, ROT NODE={qa}\n")
            nodes_b = [n for n in sorted(j["nodes"][pb]) if n not in slaved and n not in fixed_nodes]
            fixed_nodes.update(nodes_b)
            ib = coords[pb]
            a = np.asarray(j["axis"])
            x_ref = np.asarray(j["at"])
            for n in nodes_b:
                r = ib.X[n - ib.node0 - 1] - x_ref
                # free coordinate: rotation phi about a (u += phi a x r) or slide s along a (u += s a)
                if j["kind"] in ("revolute", "hinge"):
                    g = np.cross(a, r)
                elif j["kind"] == "screw":                 # turn plus the advance it drives
                    g = np.cross(a, r) + j["lead"] / (2 * np.pi) * a
                else:
                    g = a
                # a joint coupled to another (gear, rack and pinion): its free motion is a multiple of that one's
                fnode, fscale = subst.get(free, (free, 1.0))
                for i in range(3):
                    # u_n,i - u_ref,i - (theta x r)_i - g_i * free = 0 ; (theta x r)_i = sum_j eps_ijk r_k theta_j
                    terms = [(n, i + 1, 1.0), (ra, i + 1, -1.0)]
                    for jj in range(3):
                        c = sum(_eps(i, jj, kk) * r[kk] for kk in range(3))
                        if abs(c) > 1e-12:
                            terms.append((qa, jj + 1, -c))
                    if abs(g[i] * fscale) > 1e-12:
                        terms.append((fnode, 1, -g[i] * fscale))
                    f.write(f"*EQUATION\n{len(terms)}\n")
                    flat = [f"{nn}, {d}, {c:.12g}" for nn, d, c in terms]
                    for s in range(0, len(flat), 4):  # at most 12 entries (4 terms) per line
                        f.write(", ".join(flat[s:s + 4]) + "\n")
            joint_dummies.append((free, j.get("locked", False) or free in subst))
            info["joints"].append({"kind": j["kind"], "parts": j["parts"], "connections": j["connections"],
                                   "axis": j["axis"], "at": j["at"], "nodes": j["new_nodes"]})

        # Boundary conditions (held over all steps)
        if fix_sets or pins or joint_dummies:
            f.write("*BOUNDARY\n")
            for dn, locked in joint_dummies:  # only DOF 1 of a joint's dummy node is used
                f.write(f"{dn}, {1 if locked else 2}, 3\n")      # a locked hinge (loose pin) holds it too
            for name, fx, _ in fix_sets:
                dofs = [1, 2, 3] if fx.type == "fixed" else sorted(fx.dofs)
                for d in dofs:
                    f.write(f"{name}, {d}, {d}\n")
            for p in pins:
                for d in sorted(p["fx"].dofs):
                    f.write(f"{p['ref']}, {d}, {d}\n")
                for d in sorted(p["fx"].hold_rotations):
                    f.write(f"{p['rot']}, {d}, {d}\n")

        # Constrained DOFs per node. CalculiX's RF leaves out external load applied
        # directly at constrained DOFs, so the balance check subtracts that share
        # ("into_supports"), computed from consistent nodal loads.
        # CalculiX prints RF as the internal nodal force in all three directions at every
        # printed node (not only the constrained ones), so the printed total is the
        # external load on those nodes plus the support reaction. Count all external load
        # on every reaction-set node, in every direction (milestone 6 plate case).
        held: dict[int, np.ndarray] = {}
        for _, fx, ids in fix_sets:
            for n in ids:
                held[n] = np.ones(3, dtype=bool)
        for p in pins:
            for n in p["nodes"]:
                held[n] = np.ones(3, dtype=bool)

        def into_supports(gid: int, force: np.ndarray) -> np.ndarray:
            m = held.get(gid)
            return np.where(m, force, 0.0) if m is not None else np.zeros(3)

        # One step per load case
        total_mass = 0.0
        held_mass = np.zeros(3)     # mass whose weight lands directly on held DOFs, per direction
        for inst in instances:
            rho = model.material(inst.material).density
            vols_all = 0.0
            for etype, conn in inst.mesh.blocks:
                vols = element_volumes(etype, inst.X[conn - 1])
                vols_all += float(vols.sum())
                gids = inst.node0 + conn
                hit = np.isin(gids, np.fromiter(held.keys(), dtype=np.int64)) if held else np.zeros(gids.shape, bool)
                if hit.any():
                    w = body_load_weights(etype)
                    rows = np.nonzero(hit.any(axis=1))[0]
                    for r in rows:
                        for k in np.nonzero(hit[r])[0]:
                            held_mass += rho * vols[r] * w[k] * held[int(gids[r, k])]
            total_mass += vols_all * rho
        info["total_mass"] = total_mass
        remote_points: dict[str, dict] = {}      # remote loads: their faces' centroid, point, moment
        info["remote_loads"] = remote_points

        def build_loads(loads):
            """(cload {node: force}, dload lines, applied total, share applied onto held DOFs)."""
            applied = np.zeros(3)
            supports = np.zeros(3)   # part of `applied` that lands on constrained DOFs
            cload: dict[int, np.ndarray] = {}
            dload_lines = []
            for ld in loads:
                if ld.type == "force":
                    faces = [(inst, el) for fid in ld.faces for inst, idx in [faces_of(fid)]
                             for el in inst.face_elements(idx)]
                    geo = [(inst, _face_geom(inst, e, cn, fn)) for inst, (_, fn, e, cn) in faces]
                    area = sum(g[0] for _, g in geo)
                    d = np.asarray(ld.direction, float)
                    traction = ld.magnitude * d / np.linalg.norm(d) / area
                    for inst, (a, _, nodes) in geo:
                        w = TRACTION_WEIGHTS[len(nodes) // 2]
                        for n, wi in zip(nodes, w):
                            gid = int(inst.node0 + n)
                            cload[gid] = cload.get(gid, np.zeros(3)) + traction * a * wi
                    applied += traction * area
                elif ld.type == "remote_force":
                    # A force acting at a point off the faces (offset from their area-weighted centroid,
                    # or an absolute point): the force spread by area as for a plain force, plus the
                    # moment (P - C) x F spread as a rigid-body pattern about the centroid, using the
                    # faces' second-moment tensor I = sum w (|r|^2 1 - r r^T). The nodal forces then sum
                    # exactly to F and to the moment about C: what a distributing coupling does, written
                    # as nodal loads so no coupling card is needed.
                    weight: dict[int, float] = {}
                    pos: dict[int, np.ndarray] = {}
                    for fid in ld.faces:
                        inst, idx = faces_of(fid)
                        for _, fn, e, cn in inst.face_elements(idx):
                            a, _, nodes = _face_geom(inst, e, cn, fn)
                            for n, wi in zip(nodes, TRACTION_WEIGHTS[len(nodes) // 2]):
                                gid = int(inst.node0 + n)
                                weight[gid] = weight.get(gid, 0.0) + a * wi
                                pos[gid] = inst.X[int(n) - 1]
                    gids = sorted(weight)
                    w = np.array([weight[g] for g in gids])
                    X = np.array([pos[g] for g in gids])
                    C = (w[:, None] * X).sum(axis=0) / w.sum()
                    P = np.asarray(ld.point, float) if ld.point is not None else C + np.asarray(ld.offset or [0, 0, 0], float)
                    d = np.asarray(ld.direction, float)
                    F = ld.magnitude * d / np.linalg.norm(d)
                    M = np.cross(P - C, F)
                    r = X - C
                    inertia = (w[:, None, None] * ((r * r).sum(axis=1)[:, None, None] * np.eye(3)
                                                  - r[:, :, None] * r[:, None, :])).sum(axis=0)
                    alpha = np.linalg.pinv(inertia) @ M
                    f = (w / w.sum())[:, None] * F + w[:, None] * np.cross(alpha, r)
                    for g, fi in zip(gids, f):
                        cload[g] = cload.get(g, np.zeros(3)) + fi
                    applied += F
                    remote_points[ld.id] = {"centroid": C.tolist(), "point": P.tolist(), "moment": M.tolist()}
                elif ld.type == "pressure":
                    for fid in ld.faces:
                        inst, idx = faces_of(fid)
                        for eid, fn, e, cn in inst.face_elements(idx):
                            a, n, nodes = _face_geom(inst, e, cn, fn)
                            dload_lines.append(f"{eid}, P{fn}, {ld.magnitude:.9g}")
                            fvec = -ld.magnitude * a * n
                            applied += fvec
                            for nd, wi in zip(nodes, TRACTION_WEIGHTS[len(nodes) // 2]):
                                supports += into_supports(int(inst.node0 + nd), fvec * wi)
                elif ld.type == "gravity":
                    d = np.asarray(ld.direction, float)
                    d = d / np.linalg.norm(d)
                    dload_lines.append(f"EALL, GRAV, {ld.magnitude:.9g}, {d[0]:.9g}, {d[1]:.9g}, {d[2]:.9g}")
                    applied += total_mass * ld.magnitude * d
                    supports += held_mass * ld.magnitude * d
                else:
                    diags.append(make("FEATURE_NOT_IMPLEMENTED", where={"loads": [ld.id]},
                                      feature=f"load type '{ld.type}'"))
            for gid, v in cload.items():
                supports += into_supports(gid, v)
            return cload, dload_lines, applied, supports

        def write_loads(cload, dload_lines):
            # OP=NEW: each step carries only its own loads (CalculiX otherwise accumulates),
            # so the bolt preload pairs are repeated in every step.
            f.write("*CLOAD, OP=NEW\n")
            for gid, v in sorted(cload.items()):
                for d in range(3):
                    if abs(v[d]) > 0:
                        f.write(f"{gid}, {d + 1}, {v[d]:.9g}\n")
            f.write("".join(bolt_pairs()))
            f.write("*DLOAD, OP=NEW\n" + "".join(l + "\n" for l in dload_lines))

        # Contact makes every step nonlinear: automatic incrementation (initial, period,
        # minimum, maximum) instead of a single linear increment.
        static = "*STATIC, SOLVER=PARDISO\n"
        if nonlinear:
            static += (f"{defaults.CONTACT_INC_INITIAL:g}, 1.0, {defaults.CONTACT_INC_MIN:g}, "
                       f"{defaults.CONTACT_INC_MAX:g}\n")

        # Nonlinear steps: write results only at the end of each step (CalculiX always
        # writes a step's final increment), not every increment.
        fq = ", FREQUENCY=1000" if nonlinear else ""

        step = 0
        if bolts:
            # Step 1: the preload alone, so the joint is clamped before any load case.
            f.write("*STEP\n" + static)
            write_loads({}, [])
            f.write(f"*NODE FILE{fq}\nU\n*EL FILE{fq}\nS\n")
            f.write(f"*NODE PRINT, NSET=BOLTNODES{fq}\nU\n*END STEP\n")
            step += 1
            info["steps"].append({"kind": "bolt_preload", "case": None, "step": step})

        # One static step per load case
        for case in load_cases:
            loads = [ld for ld in model.loads if ld.load_case == case]
            cload, dload_lines, applied, supports = build_loads(loads)
            f.write("*STEP\n" + static)
            write_loads(cload, dload_lines)
            f.write(f"*NODE FILE{fq}\nU\n*EL FILE{fq}\nS\n")
            if contacts:
                f.write(f"*CONTACT FILE{fq}\nCDIS, CSTR\n")
            if reaction_sets:
                f.write(f"*NODE PRINT, NSET=FIXALL, TOTALS=ONLY{fq}\nRF\n")
            if bolts:
                f.write(f"*NODE PRINT, NSET=BOLTNODES{fq}\nU\n")
            for sset in section_sets:
                # RF at a printed node is the internal force there (CLAUDE.md gotcha): at a tie slave node,
                # the force the joint carries
                f.write(f"*NODE PRINT, NSET={sset}{fq}\nRF\n")
            f.write("*END STEP\n")
            step += 1
            info["steps"].append({"kind": "static", "case": case, "step": step})
            info["load_cases"][case] = {"applied": applied.tolist(), "into_supports": supports.tolist(),
                                        "loads": [ld.id for ld in loads]}

        # Linear buckling (CalculiX manual 7.5 *BUCKLE, 7.124 *STEP): with PERTURBATION the
        # last static step is the reference state and its loads are the preload; only the
        # loads inside the *BUCKLE step are scaled by the buckling factor. So every
        # buckling case gets its own preload step (its non-scaling loads, possibly none)
        # immediately before the perturbation step.
        st = model.study
        if buckling and st and "buckling" in st.analyses:
            for case in (st.buckling_cases or st.load_cases):
                loads = [ld for ld in model.loads if ld.load_case == case]
                pre = [ld for ld in loads if not scales_in_buckling(ld)]
                scal = [ld for ld in loads if scales_in_buckling(ld)]
                c1, d1, a1, _ = build_loads(pre)
                f.write("*STEP\n*STATIC, SOLVER=PARDISO\n")
                write_loads(c1, d1)
                f.write("*END STEP\n")
                step += 1
                info["steps"].append({"kind": "preload", "case": case, "step": step})
                c2, d2, a2, _ = build_loads(scal)
                f.write(f"*STEP, PERTURBATION\n*BUCKLE, SOLVER=PARDISO\n{st.buckling_modes}\n")
                write_loads(c2, d2)
                f.write("*NODE FILE\nU\n*END STEP\n")
                step += 1
                info["steps"].append({"kind": "buckle", "case": case, "step": step})
                info["buckling"][case] = {"preload": [ld.id for ld in pre], "scaled": [ld.id for ld in scal],
                                          "scaled_total": a2.tolist(), "preload_total": a1.tolist(),
                                          "modes": st.buckling_modes}
                if not scal:
                    diags.append(make("BUCKLING_NO_SCALED_LOAD", where={"loads": [ld.id for ld in pre]}, case=case))
    return DeckResult(path, info, diags)


def _coupling_substitutions(joints: list[dict], connections: list[dict]) -> dict:
    """{free node: (root free node, scale)} for joints coupled by gear and rack & pinion connections.

    A joint's free coordinate q (DOF 1 of its dummy node) is the motion of its side B relative to side A
    about/along its axis. A coupling term's motion (of `part` relative to its carrier, about/along the term's
    axis) is sigma * q, sigma = +-1 from which side the part is and how the axes point. The coupling
    sum(scale_i * sigma_i * q_i) = 0 is used to write q_2 = -(s_1/s_2) q_1 straight into joint 2's equations:
    no equation joins the two dummy nodes, so no MPC is chained (CalculiX 2.22 crashes on chained MPCs
    between rigid-body nodes, milestone 9). Joint 2's own dummy node is then held."""
    out: dict = {}
    records = []
    by_conn = {cid: j for j in joints for cid in j["connections"]}
    for c in connections:
        if c["type"] not in ("gear", "rack_pinion"):
            continue
        coeffs = []
        for t in c["couples"]:
            j = by_conn.get(t["connection"])
            if j is None:
                break
            pb = j["bodies"][1][0]
            free = j["bodies"][1][2]
            sigma = (1.0 if t["part"] == pb else -1.0) * float(np.sign(np.dot(j["axis"], t["axis"])) or 1.0)
            coeffs.append((free, t["scale"] * sigma))
        if len(coeffs) != 2:
            continue
        (q1, s1), (q2, s2) = coeffs
        r1, k1 = out.get(q1, (q1, 1.0))
        if q2 in out or r1 == q2:
            continue                       # a closed gear loop: the second relation is redundant or locking
        out[q2] = (r1, -s1 / s2 * k1)
        records.append((c["id"], [q1, q2]))
    out["_records"] = records
    return out


def _write_slide(f, c: dict, si: Instance, mi: Instance, mf: int, slave: list[int], tol: float) -> int:
    """*EQUATIONs holding each slave node to the master face along the face normal only. Returns the count."""
    from ..hybrid.solve import _face_project
    faces, ids = [], []
    for _, fn, e, cn in mi.face_elements(mf):
        fnodes = cn[FACE_NODES[e][fn - 1]]
        ids.append([int(mi.node0 + n) for n in fnodes])
        faces.append((e, mi.X[fnodes - 1]))
    if not faces or not slave:
        return 0
    P = np.array([si.X[n - si.node0 - 1] for n in slave])
    hits = _face_project(P, faces, max(tol, 0.0) + 0.05)
    given = np.asarray(c["normal"], float) if c.get("normal") is not None else None
    count = 0
    for n, h in zip(slave, hits):
        if h is None:
            continue
        j, N, _ = h
        if given is not None:
            nrm = given
        else:
            X = faces[j][1]
            nrm = np.cross(X[1] - X[0], X[2] - X[0])
        nrm = nrm / np.linalg.norm(nrm)
        d0 = int(np.argmax(np.abs(nrm)))
        terms = [(n, d0 + 1, float(nrm[d0]))]
        terms += [(n, d + 1, float(nrm[d])) for d in range(3) if d != d0 and abs(nrm[d]) > 1e-9]
        for m, w in zip(ids[j], N):
            if abs(w) > 1e-12:
                terms += [(m, d + 1, -float(w * nrm[d])) for d in range(3) if abs(nrm[d]) > 1e-9]
        f.write(f"*EQUATION\n{len(terms)}\n")
        flat = [f"{nn}, {d}, {cf:.12g}" for nn, d, cf in terms]
        for s in range(0, len(flat), 4):
            f.write(", ".join(flat[s:s + 4]) + "\n")
        count += 1
    return count


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)[:70]
