"""Read an AsmFEA CalculiX deck (study.inp) for the hybrid solver.

Only what AsmFEA writes for a linear static study is read: nodes, solid elements (C3D10, C3D15, C3D20R),
element and node sets, node and element-face surfaces, ties, materials (elastic, density), solid sections,
fixed boundaries, and static steps with nodal forces, gravity and node prints. Any other keyword (equations,
contact, rigid bodies, couplings, springs, beams, buckling...) raises Unsupported: those studies are solved
by CalculiX."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..mesh.elements import FACE_NODES

SOLID = {"C3D10": 10, "C3D15": 15, "C3D20R": 20}
IGNORED = {"*HEADING", "*NODE FILE", "*EL FILE", "*END STEP"}


class Unsupported(Exception):
    """The deck needs something the hybrid solver does not do: CalculiX solves it instead."""


@dataclass
class Step:
    cloads: list = field(default_factory=list)          # (node, dof 1..3, value)
    gravity: list = field(default_factory=list)         # (elset, magnitude, (dx, dy, dz))
    prints: list = field(default_factory=list)          # (nset, totals_only)


@dataclass
class Deck:
    nodes: dict = field(default_factory=dict)            # id -> (3,)
    blocks: dict = field(default_factory=dict)           # etype -> (ids (m,), conn (m, n))
    elsets: dict = field(default_factory=dict)           # name -> list of ids or names
    nsets: dict = field(default_factory=dict)
    surfaces: dict = field(default_factory=dict)         # name -> ("node", [items]) | ("element", [(eid, face)])
    ties: list = field(default_factory=list)             # (name, slave surface, master surface, tolerance)
    materials: dict = field(default_factory=dict)        # name -> {"E", "nu", "rho"}
    sections: list = field(default_factory=list)         # (elset, material)
    boundary: list = field(default_factory=list)         # (set or node, dof from, dof to)
    steps: list = field(default_factory=list)

    # ---------------------------------------------------------------- sets

    def element_ids(self, name: str) -> np.ndarray:
        out, stack, seen = [], [name], set()
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            if n.upper() == "EALL":
                return np.concatenate([ids for ids, _ in self.blocks.values()])
            for v in self.elsets.get(n, []):
                if isinstance(v, int):
                    out.append(v)
                else:
                    stack.append(v)
        return np.array(sorted(set(out)), dtype=np.int64)

    def node_ids(self, name: str) -> np.ndarray:
        if name.isdigit():
            return np.array([int(name)], dtype=np.int64)
        out, stack, seen = [], [name], set()
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            for v in self.nsets.get(n, []):
                if isinstance(v, int):
                    out.append(v)
                else:
                    stack.append(v)
        return np.array(sorted(set(out)), dtype=np.int64)

    def surface_nodes(self, name: str) -> np.ndarray:
        kind, items = self.surfaces[name]
        if kind == "node":
            return np.unique(np.concatenate([self.node_ids(str(i)) for i in items]))
        conn = self.element_conn()
        out = set()
        for eid, fnum in items:
            etype, row = conn[eid]
            out.update(int(n) for n in row[FACE_NODES[etype][fnum - 1]])
        return np.array(sorted(out), dtype=np.int64)

    def surface_faces(self, name: str) -> list[tuple[str, np.ndarray]]:
        """An element-face surface's faces: (etype of the face's element, node ids corners then midsides)."""
        kind, items = self.surfaces[name]
        if kind != "element":
            raise ValueError(f"{name} is a node surface")
        conn = self.element_conn()
        return [(conn[eid][0], conn[eid][1][FACE_NODES[conn[eid][0]][fnum - 1]]) for eid, fnum in items]

    def element_conn(self) -> dict:
        if not hasattr(self, "_conn"):
            self._conn = {int(i): (et, c) for et, (ids, conn) in self.blocks.items() for i, c in zip(ids, conn)}
        return self._conn


def _opts(line: str) -> dict:
    out = {}
    for part in line.split(",")[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip()
        elif part.strip():
            out[part.strip().upper()] = True
    return out


def read(path: Path) -> Deck:
    d = Deck()
    lines = Path(path).read_text(errors="replace").splitlines()
    i, n = 0, len(lines)
    step = None
    pending_el = {}
    while i < n:
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("**"):
            continue
        if not line.startswith("*"):
            continue
        kw = line.split(",")[0].strip().upper()
        o = _opts(line)
        body = []
        while i < n and not lines[i].lstrip().startswith("*"):
            if lines[i].strip() and not lines[i].lstrip().startswith("**"):
                body.append(lines[i].strip())
            i += 1
        if kw == "*NODE":
            for b in body:
                v = b.split(",")
                d.nodes[int(v[0])] = np.array([float(x) for x in v[1:4]])
            if "NSET" in o:
                d.nsets.setdefault(o["NSET"], []).extend(int(b.split(",")[0]) for b in body)
        elif kw == "*ELEMENT":
            et = o.get("TYPE", "").upper()
            if et not in SOLID:
                raise Unsupported(f"element type {et}")
            per = SOLID[et] + 1
            nums = [int(x) for b in body for x in b.replace(",", " ").split()]
            arr = np.array(nums, dtype=np.int64).reshape(-1, per)
            ids, conn = arr[:, 0], arr[:, 1:]
            if et in d.blocks:
                ids0, conn0 = d.blocks[et]
                ids, conn = np.concatenate([ids0, ids]), np.vstack([conn0, conn])
            d.blocks[et] = (ids, conn)
            if "ELSET" in o:
                d.elsets.setdefault(o["ELSET"], []).extend(int(x) for x in arr[:, 0])
        elif kw in ("*ELSET", "*NSET"):
            name = o.get("ELSET") or o.get("NSET")
            vals = [x.strip() for b in body for x in b.split(",") if x.strip()]
            if o.get("GENERATE"):
                a, b_, s = (int(x) for x in vals[:3])
                items = list(range(a, b_ + 1, s))
            else:
                items = [int(v) if v.lstrip("-").isdigit() else v for v in vals]
            (d.elsets if kw == "*ELSET" else d.nsets).setdefault(name, []).extend(items)
        elif kw == "*SURFACE":
            name, typ = o["NAME"], str(o.get("TYPE", "ELEMENT")).upper()
            if typ == "NODE":
                d.surfaces[name] = ("node", [b.split(",")[0].strip() for b in body])
            else:
                items = []
                for b in body:
                    a, f = (x.strip() for x in b.split(",")[:2])
                    if not a.isdigit():
                        raise Unsupported("element-set surfaces")
                    items.append((int(a), int(f.upper().lstrip("S"))))
                d.surfaces[name] = ("element", items)
        elif kw == "*TIE":
            s, m = (x.strip() for x in body[0].split(",")[:2])
            d.ties.append((o.get("NAME", f"T{len(d.ties) + 1}"), s, m, float(o.get("POSITION TOLERANCE", 0.0))))
        elif kw == "*MATERIAL":
            current = o["NAME"]
            d.materials[current] = {}
        elif kw == "*ELASTIC":
            E, nu = (float(x) for x in body[0].split(",")[:2])
            d.materials[current].update(E=E, nu=nu)
        elif kw == "*DENSITY":
            d.materials[current]["rho"] = float(body[0].split(",")[0])
        elif kw == "*SOLID SECTION":
            d.sections.append((o["ELSET"], o["MATERIAL"]))
        elif kw == "*BOUNDARY":
            if step is not None:
                raise Unsupported("boundary conditions inside a step")
            for b in body:
                v = [x.strip() for x in b.split(",")]
                if len(v) > 3 and float(v[3]) != 0.0:
                    raise Unsupported("prescribed non-zero displacements")
                d.boundary.append((v[0], int(v[1]), int(v[2]) if len(v) > 2 and v[2] else int(v[1])))
        elif kw == "*STEP":
            if o.get("PERTURBATION"):
                raise Unsupported("perturbation steps (buckling)")
            step = Step()
            d.steps.append(step)
        elif kw == "*STATIC":
            pass
        elif kw == "*CLOAD":
            for b in body:
                v = [x.strip() for x in b.split(",")]
                for nd in d.node_ids(v[0]):
                    step.cloads.append((int(nd), int(v[1]), float(v[2])))
        elif kw == "*DLOAD":
            for b in body:
                v = [x.strip() for x in b.split(",")]
                if v[1].upper() != "GRAV":
                    raise Unsupported(f"distributed load {v[1]}")
                step.gravity.append((v[0], float(v[2]), tuple(float(x) for x in v[3:6])))
        elif kw == "*NODE PRINT":
            step.prints.append((o["NSET"], str(o.get("TOTALS", "")).upper() == "ONLY"))
        elif kw in IGNORED:
            pass
        else:
            raise Unsupported(f"keyword {kw}")
    return d
