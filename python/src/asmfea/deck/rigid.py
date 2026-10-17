"""Rigid groups in the deck (D96, spikes/rigid/RESULTS.md).

Each group of rigid parts has a reference node r at its centre of mass and a rotation node q whose translation DOFs
carry the group's turns theta. Nothing of the group is meshed: every node of another part that its connections join
to it follows its rigid motion by three linear equations u_n - u_r - theta x (x_n - x_r) = 0, node dependent (the form
of a joint's side B, proven in CalculiX 2.22 with element-free r and q, P1-P10). No *RIGID BODY is used: CalculiX
2.22 crashes on equations between rigid-body reference nodes, and plain equations chain safely (P6).

Holds on a group (a fixture on a rigid face) are rows over its six motions [t, theta] about x_r, reduced to plain
boundary conditions where a row holds one DOF and to equations otherwise (reduced row echelon form, so each held DOF
is the dependent of one row only). Loads on rigid faces become a force on r and a moment on q.
"""
from __future__ import annotations

import numpy as np


def plan_groups(doc: dict | None, first_node: int) -> tuple[list[dict], int]:
    """(groups with their nodes allocated, next free node). Each group: id, parts, r, q, x (reference point),
    mass, and the per-part masses and centres of mass for gravity."""
    if not doc:
        return [], first_node
    out, nxt = [], first_node
    for g in doc.get("groups", []):
        parts = {p: doc["parts"][p] for p in g["parts"] if p in doc.get("parts", {})}
        out.append({"id": g["id"], "parts": list(g["parts"]), "r": nxt, "q": nxt + 1, "index": len(out),
                    "x": np.asarray(g["ref"], float), "mass": float(g["mass"]), "part_data": parts,
                    "rows": [], "coupled": set(), "held": False,
                    "tie_nodes": set(), "normals": []})     # for the hybrid solver: bonded nodes, sliding nodes
        nxt += 2
    return out, nxt


def skew_terms(d: np.ndarray) -> list[tuple[int, int, float]]:
    """(i, j, c) with (theta x d)_i = sum_j c theta_j."""
    out = []
    for i in range(3):
        for j in range(3):
            k = 3 - i - j
            if i == j or k < 0 or k > 2:
                continue
            c = (1.0 if (i, j, k) in ((0, 1, 2), (1, 2, 0), (2, 0, 1)) else -1.0) * d[k]
            if abs(c) > 1e-12:
                out.append((i, j, c))
    return out


def coupling_equations(n: int, x_n, g: dict) -> list[list[tuple[int, int, float]]]:
    """The three equations holding node n to group g's rigid motion (node dependent)."""
    d = np.asarray(x_n, float) - g["x"]
    eqs = []
    sk = skew_terms(d)
    for i in range(3):
        terms = [(n, i + 1, 1.0), (g["r"], i + 1, -1.0)]
        terms += [(g["q"], j + 1, -c) for ii, j, c in sk if ii == i]
        eqs.append(terms)
    return eqs


def normal_equation(n: int, x_n, nrm, g: dict) -> list[tuple[int, int, float]]:
    """A node kept on a rigid face along its normal only: n . (u_n - u_r - theta x d) = 0, with
    n . (theta x d) = theta . (d x n). The dependent is the node's DOF along the normal's largest component."""
    nrm = np.asarray(nrm, float) / np.linalg.norm(nrm)
    d = np.asarray(x_n, float) - g["x"]
    d0 = int(np.argmax(np.abs(nrm)))
    terms = [(n, d0 + 1, float(nrm[d0]))]
    terms += [(n, k + 1, float(nrm[k])) for k in range(3) if k != d0 and abs(nrm[k]) > 1e-9]
    terms += [(g["r"], k + 1, -float(nrm[k])) for k in range(3) if abs(nrm[k]) > 1e-9]
    dxn = np.cross(d, nrm)
    terms += [(g["q"], k + 1, -float(dxn[k])) for k in range(3) if abs(dxn[k]) > 1e-12]
    return terms


def point_rows(points, dofs, x_r) -> list[np.ndarray]:
    """Rows over [t, theta] holding the given translations at each point: e_d . (t + theta x (p - x_r)) = 0, the
    theta part being (p - x_r) x e_d."""
    rows = []
    for p in points:
        d = np.asarray(p, float) - x_r
        for dof in dofs:
            e = np.eye(3)[dof - 1]
            rows.append(np.concatenate([e, np.cross(d, e)]))
    return rows


def reduce_holds(rows: list[np.ndarray], tol: float = 1e-9) -> dict:
    """{"spc": [column], "equations": [[(column, coef), ...]]} of independent hold rows over the six columns
    (0-2 translations, 3-5 turns): reduced row echelon form, each row's leading column appearing in no other row."""
    if not rows:
        return {"spc": [], "equations": []}
    A = np.array(rows, float)
    # scale the turn columns to the rows' lever lengths, so that the rank test does not depend on the units
    _, sv, vt = np.linalg.svd(A, full_matrices=False)
    basis = vt[:int(np.sum(sv > tol * max(sv[0], 1e-300)))]
    R = basis.copy()
    piv: list[int] = []
    for r in range(len(R)):
        cands = [c for c in range(6) if c not in piv]
        c = max(cands, key=lambda c: abs(R[r, c]))
        R[r] /= R[r, c]
        for q in range(len(R)):
            if q != r:
                R[q] -= R[q, c] * R[r]
        piv.append(c)
    spc, eqs = [], []
    for r, c in enumerate(piv):
        big = np.abs(R[r]).max()
        others = [(d, float(R[r, d])) for d in range(6) if d != c and abs(R[r, d]) > 1e-12 * big]
        if not others:
            spc.append(c)
        else:
            eqs.append([(c, 1.0)] + others)
    return {"spc": sorted(spc), "equations": eqs}


def free_motions(rows: list[np.ndarray]) -> np.ndarray:
    """(6, k) basis of the motions [t, theta] a group's holds leave free (all six with no hold)."""
    if not rows:
        return np.eye(6)
    A = np.array(rows, float)
    _, sv, vt = np.linalg.svd(A, full_matrices=True)
    rank = int(np.sum(sv > 1e-9 * max(sv[0], 1e-300)))
    return vt[rank:].T


def column_dof(g: dict, col: int) -> tuple[int, int]:
    """(node, dof) of a group's motion column: 0-2 the reference node's translations, 3-5 the rotation node's."""
    return (g["r"], col + 1) if col < 3 else (g["q"], col - 2)


def resultant(g: dict, forces) -> tuple[np.ndarray, np.ndarray]:
    """(force, moment about x_r) of point forces [(point, force)]."""
    F, M = np.zeros(3), np.zeros(3)
    for p, f in forces:
        f = np.asarray(f, float)
        F += f
        M += np.cross(np.asarray(p, float) - g["x"], f)
    return F, M


def motion_at(t: np.ndarray, th: np.ndarray, x_r: np.ndarray, X: np.ndarray) -> np.ndarray:
    """Displacements of points X under the small rigid motion (t, theta) about x_r."""
    return np.asarray(t, float)[None, :] + np.cross(np.asarray(th, float)[None, :], np.asarray(X, float) - x_r)
