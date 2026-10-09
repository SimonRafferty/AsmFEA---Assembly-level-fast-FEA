"""The hybrid solver's core: parts assembled one at a time, condensed onto smooth modes on the faces where they
join other parts, joined through a small global system, then recovered.

- A part (one instance of the deck) is assembled with its own internal ties (span to end region) applied
  exactly, as CalculiX applies them: each slave node follows the master face at its projection.
- A tie between two parts becomes an interface patch. Both sides' face nodes follow one smooth field over
  the patch: a polynomial of degree s in the patch's plane, 3 (s+1)(s+2)/2 modes (18 at s = 2). The
  speed investigation measured about 1 % on displacement and 1 to 4 % on joint stresses at s = 2.
- Each part's interior is condensed onto its patches' modes exactly; the global system has only the modes,
  so memory is set by the largest part, not the model.
- ``exact=True`` joins every tie exactly instead (the whole model in one system): a check against CalculiX
  on small models, not a way to solve large ones.
"""
from __future__ import annotations

import math
import os
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from pathlib import Path
from dataclasses import dataclass, field

import numpy as np
import psutil
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from scipy.spatial import cKDTree

from . import elements as el
from .deckread import Deck

PIN_FIXED_FRACTION = 0.5         # a joint with this share of one side fixed is pinned there (see joint_terms)
GLOBAL_RESIDUAL_MAX = 0.1          # relative residual of the global joint system above which it fell apart:
                                   # 15-21 on srleg with parts joined to nothing; a sound solve gives about
                                   # 1e-7 x PENALTY_SCALE, round-off of the stiff agreement rows
PENALTY_SCALE = 1e3                # agreement rows between joint fields, as a penalty (see solve): rows held
                                   # to 3e-8 on srleg, residual 2e-4 (1e4: 7e-9 but residual 2e-3)
GLOBAL_REFINE_STEPS = 2            # refinement of the scaled joint solve (Factor): the water tank's residual went
                                   # 1.0 -> 1.7e-5 -> 8e-6, flat after
MEMORY_RESERVE_FRACTION = 0.25    # of the RAM available at the start, left free for everything else
GLOBAL_BYTES_PER_ENTRY = 50        # the global joint system's memory per condensed entry, its factor included:
                                   # measured 42 on the frame with exact chord welds (412 M entries, peak 18 GB,
                                   # D62). Was 100 while its preparation made several copies (Bench truss L: 60
                                   # then left its solve 5 GB over the budget); at 100 no factor could be kept
                                   # for recovery on the frame
DISK_RESERVE = 5 * 2 ** 30         # disk left free when interior responses wait on disk
XT_SPILL_MAX = 2 * 2 ** 30         # a part's interior response larger than this is not kept (not even on disk):
                                   # recovery factorises the part again, one factorisation and one solve against
                                   # writing and reading it back (found: a 1 m tank panel's was 14 GB, and its
                                   # disk writes held the solve to one core for hours)
RHS_BLOCK = 256                    # joint modes solved for at once when condensing a part
SCHUR_MIN_MODES = 200              # a part with more joint modes is condensed by PARDISO's Schur complement
SCHUR_MAX_BDOF = 30000             # ... when its joint DOF fit a dense block (30000: 7.2 GB). Bench truss L's
                                   # chord (20k joint DOF, 6570 modes) took 890 s by right-hand sides
SCHUR_PARTIAL_MAX_BDOF = 7000      # up to this many joint DOF, and at most this share of the part's DOF, a
SCHUR_PARTIAL_MAX_FRACTION = 0.03  # part's Schur factor is kept for partial solves (SchurFactor, D62): PARDISO
                                   # then factorises the dense Schur block too, ns^3/3 flops on the part's
                                   # thread(s), worth it only beside a much larger part. The frame's rungs (1.4 %)
                                   # and chords at 6840 (2.3 %) gained, chords at 13 680 (4.7 %) lost 5 s each; the
                                   # tank's panels (28 800) took minutes, and its small parts (boundary up to 26 %
                                   # of the part) left the tank 12 % slower than before D62
MEMORY_RESERVE_MIN = 2 * 2 ** 30   # and never less than this
PART_BYTES_PER_DOF = 4000          # a part's stiffness copies and temporaries; its factorisation reserves
                                   # PARDISO's own figure from the analysis (a guess per DOF for the whole
                                   # part was out by 2-3x: srleg 36 GB on 31, Bench truss L 52 on 33)
ELEMENT_CHUNK = 4000              # elements per assembly batch: bounds the memory of the element matrices


@dataclass
class Settings:
    degree: int = 2               # polynomial degree of a patch's modes (s)
    patch_size: float = 0.0       # mm; > 0 splits patches larger than this into a grid of sub-patches
    exact: bool = False
    nodes_per_mode: float = 1.0   # each side of a patch needs this many nodes per polynomial (0: no cap)
    split_dof: int = 0              # a part with more DOF is cut into slabs joined exactly (0: never). Off
                                    # until a case exercises it: no large part without internal ties yet
    exact_max_nodes: int = 10 ** 9  # a joint whose footprint has at most this many master nodes is joined
                                    # exactly (its master nodes are global unknowns); larger ones by modes
    exact_part_unknowns: int = 0    # and while no part takes more exact joint unknowns than this (0: no cap)
    exact_total_unknowns: int = 0   # and while the whole model's exact joint unknowns stay within this (0: no cap)
    workers: int = 0                # parts solved at once; 0: as many as memory and cores allow
    memory_bytes: int = 0           # memory budget; 0: the RAM available at the start less a reserve
    reserve_bytes: int = 0          # free memory kept for other programs: below it, parts start one at a time (D60)


@dataclass
class PartData:
    name: str
    nodes: np.ndarray                     # global node ids (sorted)
    blocks: list                          # [(etype, element ids, conn global ids, material name)]
    internal_ties: list = field(default_factory=list)
    shared: frozenset = frozenset()       # nodes this piece shares with other pieces of a split part
    loads_here: bool = True               # False: point loads on shared nodes go to another piece


def split_part(deck: Deck, part: PartData, max_dof: int) -> list[PartData]:
    """A part too large to condense in one piece, cut into slabs of elements along its longest axis. Nodes on
    a cut belong to both slabs and become exact global unknowns, so the split is exact (substructuring),
    not an approximation. Found: Bench truss S's chords (about a million DOF each, dozens of joints) made
    the condensation 20 GB per chord and slower than CalculiX. A part with internal ties is kept whole (a tie
    must stay within one piece)."""
    k = int(np.ceil(3 * len(part.nodes) / max_dof))
    if k <= 1 or part.internal_ties:
        return [part]
    X = np.array([deck.nodes[int(n)] for n in part.nodes])
    c = X.mean(axis=0)
    axis = np.linalg.svd(X - c, full_matrices=False)[2][0]
    pos = {int(n): float((x - c) @ axis) for n, x in zip(part.nodes, X)}
    cents = []
    for etype, eids, conn, mname in part.blocks:
        cents.append(np.array([np.mean([pos[int(v)] for v in row]) for row in conn]))
    allc = np.concatenate(cents)
    edges = np.quantile(allc, np.linspace(0, 1, k + 1)[1:-1])
    pieces_blocks = [[] for _ in range(k)]
    for (etype, eids, conn, mname), cc in zip(part.blocks, cents):
        which = np.searchsorted(edges, cc)
        for j in range(k):
            sel = which == j
            if sel.any():
                pieces_blocks[j].append((etype, eids[sel], conn[sel], mname))
    node_sets = [np.unique(np.concatenate([b[2].ravel() for b in bl])) for bl in pieces_blocks if bl]
    count = {}
    for ns in node_sets:
        for n in ns.tolist():
            count[n] = count.get(n, 0) + 1
    shared = frozenset(n for n, m in count.items() if m > 1)
    out = []
    for j, (bl, ns) in enumerate(zip([b for b in pieces_blocks if b], node_sets)):
        out.append(PartData(f"{part.name} [{j + 1}/{len(node_sets)}]", ns.astype(np.int64), bl, [],
                            shared=frozenset(n for n in ns.tolist() if n in shared), loads_here=(j == 0)))
    return out


# --------------------------------------------------------------------------- ties as constraints

def _shapes(n: int, xi: np.ndarray, eta: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised joints._shape: N (m, n) and dN (m, n, 2) at m local points of a 6-node triangle or an
    8-node quad."""
    m = len(xi)
    if n == 6:
        L1, L2, L3 = 1 - xi - eta, xi, eta
        N = np.stack([L1 * (2 * L1 - 1), L2 * (2 * L2 - 1), L3 * (2 * L3 - 1),
                      4 * L1 * L2, 4 * L2 * L3, 4 * L3 * L1], axis=1)
        dL = np.array([[-1, -1], [1, 0], [0, 1]], float)
        a, b, c = (4 * L1 - 1)[:, None], (4 * L2 - 1)[:, None], (4 * L3 - 1)[:, None]
        dN = np.stack([a * dL[0], b * dL[1], c * dL[2],
                       4 * (L1[:, None] * dL[1] + L2[:, None] * dL[0]),
                       4 * (L2[:, None] * dL[2] + L3[:, None] * dL[1]),
                       4 * (L3[:, None] * dL[0] + L1[:, None] * dL[2])], axis=1)
        return N, dN
    N, dN = np.zeros((m, 8)), np.zeros((m, 8, 2))
    for i, (a, b) in enumerate([(-1, -1), (1, -1), (1, 1), (-1, 1)]):
        N[:, i] = 0.25 * (1 + a * xi) * (1 + b * eta) * (a * xi + b * eta - 1)
        dN[:, i, 0] = 0.25 * a * (1 + b * eta) * (2 * a * xi + b * eta)
        dN[:, i, 1] = 0.25 * b * (1 + a * xi) * (a * xi + 2 * b * eta)
    for i, (a, b) in enumerate([(0, -1), (1, 0), (0, 1), (-1, 0)], start=4):
        if a == 0:
            N[:, i] = 0.5 * (1 - xi ** 2) * (1 + b * eta)
            dN[:, i, 0], dN[:, i, 1] = -xi * (1 + b * eta), 0.5 * b * (1 - xi ** 2)
        else:
            N[:, i] = 0.5 * (1 + a * xi) * (1 - eta ** 2)
            dN[:, i, 0], dN[:, i, 1] = 0.5 * a * (1 - eta ** 2), -eta * (1 + a * xi)
    return N, dN


def _face_project(P: np.ndarray, faces: list, tol: float):
    """Each point's nearest master-face location: (face index, shape weights over that face's nodes, distance),
    or None when it is not tied. Faces: [(etype, node coords (k, 3))], 6- or 8-noded. All candidate (point,
    face) pairs of one face type are projected together by Gauss-Newton on the face's shape functions,
    clipped to the face.

    A point is tied, as CalculiX ties it, when its distance NORMAL to the nearest face is within ``tol``;
    it may lie beside the face (an edge node of a slave face overhanging the master's edge) by up to the
    face's own size. Found on a 24-part welded chassis: CalculiX tied nodes up to 8.6 mm beside a face with a
    0.55 mm tolerance, and the plain point-to-face distance missed 96 of them."""
    out = [None] * len(P)
    if not faces or not len(P):
        return out
    tree = cKDTree(P)
    lim = max(tol, 1e-6)
    best_d = np.full(len(P), np.inf)
    best_j = np.full(len(P), -1)
    best_N = [None] * len(P)
    for n in (6, 8):
        js = [j for j, (_, F) in enumerate(faces) if len(F) == n]
        if not js:
            continue
        Fs = np.array([faces[j][1] for j in js])                      # (f, n, 3)
        cent = Fs.mean(axis=1)
        rad = np.linalg.norm(Fs - cent[:, None], axis=2).max(axis=1)
        pi, fi = [], []
        for k, hits in enumerate(tree.query_ball_point(cent, 2.2 * rad + lim)):
            pi += hits
            fi += [k] * len(hits)
        if not pi:
            continue
        pi, fi = np.array(pi), np.array(fi)
        F, p = Fs[fi], P[pi]                                          # (m, n, 3), (m, 3)
        xi = np.full((len(pi), 2), 1 / 3 if n == 6 else 0.0)
        for _ in range(12):
            N, dN = _shapes(n, xi[:, 0], xi[:, 1])
            x = np.einsum("mk,mkd->md", N, F)
            J = np.einsum("mka,mkd->mad", dN, F)                      # (m, 2, 3)
            A = J @ J.transpose(0, 2, 1)
            r = np.einsum("mad,md->ma", J, p - x)
            det = A[:, 0, 0] * A[:, 1, 1] - A[:, 0, 1] * A[:, 1, 0]
            det = np.where(np.abs(det) > 1e-300, det, 1e-300)
            step = np.stack([A[:, 1, 1] * r[:, 0] - A[:, 0, 1] * r[:, 1],
                             A[:, 0, 0] * r[:, 1] - A[:, 1, 0] * r[:, 0]], axis=1) / det[:, None]
            xi = xi + step
            if n == 6:
                xi = np.clip(xi, 0.0, 1.0)
                s = xi.sum(axis=1)
                xi = xi / np.maximum(s, 1.0)[:, None]
            else:
                xi = np.clip(xi, -1.0, 1.0)
            if np.abs(step).max() < 1e-10:
                break
        N, dN = _shapes(n, xi[:, 0], xi[:, 1])
        gap = p - np.einsum("mk,mkd->md", N, F)
        dist = np.linalg.norm(gap, axis=1)
        J = np.einsum("mka,mkd->mad", dN, F)
        nrm = np.cross(J[:, 0], J[:, 1])
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1), 1e-300)[:, None]
        dn = np.abs(np.einsum("md,md->m", gap, nrm))
        side = np.sqrt(np.maximum(dist ** 2 - dn ** 2, 0.0))
        ok = (dn <= lim * (1 + 1e-9)) & (side <= 2 * rad[fi] + 1e-9)
        order = np.lexsort((dist, pi))                                # nearest face first for each point
        first = np.ones(len(order), bool)
        first[1:] = pi[order][1:] != pi[order][:-1]
        for m in order[first]:
            i = pi[m]
            if dist[m] < best_d[i]:
                best_d[i], best_j[i], best_N[i] = dist[m], (js[fi[m]] if ok[m] else -1), N[m]
    for i in range(len(P)):
        if best_j[i] >= 0:
            out[i] = (int(best_j[i]), best_N[i], float(best_d[i]))
    return out


def tie_footprint(deck: Deck, tie, coords) -> tuple[set, set, dict]:
    """(slave nodes, master nodes) of the tie's real interface: the slave nodes that land on the master
    surface within the position tolerance, and the nodes of the master faces they land on. AsmFEA's master
    surface is the receiving part's whole CAD face, which several ties share; the joint is only the part
    under each tie's slave side."""
    name, slave, master, tol = tie
    snodes = deck.surface_nodes(slave)
    faces = deck.surface_faces(master)
    fx = [(et, np.array([coords[int(n)] for n in ids])) for et, ids in faces]
    hits = _face_project(np.array([coords[int(s)] for s in snodes]), fx, tol) if len(snodes) else []
    s_out, m_out, weights = set(), set(), {}
    for s, h in zip(snodes, hits):
        if h is None:
            continue
        s_out.add(int(s))
        m_out.update(int(n) for n in faces[h[0]][1])
        weights[int(s)] = [(int(m), float(w)) for m, w in zip(faces[h[0]][1], h[1]) if abs(w) > 1e-14]
    return s_out, m_out - s_out, weights


def tie_constraints(deck: Deck, tie, coords) -> dict:
    """{slave node: [(master node, weight), ...]} for one tie (nodes found within its position tolerance)."""
    name, slave, master, tol = tie
    snodes = deck.surface_nodes(slave)
    faces = deck.surface_faces(master)
    fx = [(et, np.array([coords[int(n)] for n in ids])) for et, ids in faces]
    master_nodes = {int(n) for _, ids in faces for n in ids}
    snodes = np.array([s for s in snodes if int(s) not in master_nodes])      # a node on both sides stays free
    hits = _face_project(np.array([coords[int(s)] for s in snodes]), fx, tol) if len(snodes) else []
    out = {}
    for s, h in zip(snodes, hits):
        if h is None:
            continue
        j, N, _ = h
        out[int(s)] = [(int(m), float(w)) for m, w in zip(faces[j][1], N) if abs(w) > 1e-14]
    return out


def collect_ties(deck: Deck, ties: list, held) -> dict:
    """{slave: [(master, weight)]} of several ties, taken in deck order as CalculiX takes them: a node
    already slaved keeps its first tie, a fixed node (``held``) is never a slave, and a slave whose master
    nodes already depend on it (directly or through other ties) is left untied rather than closing a loop.
    On a 24-part welded chassis the last rule picked out exactly the 21 loop nodes CalculiX left untied."""
    rank = {tt[0]: k for k, tt in enumerate(deck.ties)}
    out: dict = {}

    def depends(a, target, seen):
        if a == target:
            return True
        if a not in out or a in seen:
            return False
        seen.add(a)
        return any(depends(m, target, seen) for m, _ in out[a])
    for tie in sorted(ties, key=lambda tt: rank.get(tt[0], len(rank))):
        new = {}
        for s, w in tie_constraints(deck, tie, deck.nodes).items():
            if s in held or s in out:
                continue
            if any(depends(m, s, set()) for m, _ in w):
                continue
            new[s] = w
        out.update(new)
    return out


# --------------------------------------------------------------------------- linear solves

class Singular(RuntimeError):
    """The model is not held together: some parts move freely (a mechanism)."""


_PARDISO: list = []                    # [pypardiso module or None], found once


def _pardiso():
    """pypardiso, or None when it is not installed. Each new pypardiso solver searches the Python tree for
    MKL's runtime library (0.3 s a time on this machine), so the first one's library is handed to the rest
    through pypardiso's own PYPARDISO_MKL_RT setting."""
    if not _PARDISO:
        try:
            import pypardiso
            if "PYPARDISO_MKL_RT" not in os.environ:
                # the installed AsmFEA ships pypardiso without MKL: it uses the MKL runtime the installer puts
                # beside CalculiX (mkl_rt.2.dll), which PARDISO in CalculiX already runs on
                try:
                    from ..config import load_config
                    ccx = load_config().ccx
                    beside = Path(ccx).parent / "mkl_rt.2.dll" if ccx else None
                except Exception:                             # noqa: BLE001 - no configuration: search instead
                    beside = None
                if beside is not None and beside.exists():
                    os.environ["PYPARDISO_MKL_RT"] = str(beside)
                else:
                    os.environ["PYPARDISO_MKL_RT"] = pypardiso.PyPardisoSolver().libmkl._name
        except (ImportError, OSError, AttributeError):
            pypardiso = None
        _PARDISO.append(pypardiso)
    return _PARDISO[0]


def _remove_stale_spills() -> None:
    """Spill folders of solves whose process is gone (a cancelled job's process is ended, so its temporary folder
    is never removed: 24 GB was left behind on a water tank). The folder name carries its process number."""
    import re
    for d in Path(tempfile.gettempdir()).glob("asmfea_hybrid_*"):
        m = re.match(r"asmfea_hybrid_(\d+)_", d.name)
        if m is None or not psutil.pid_exists(int(m.group(1))):
            shutil.rmtree(d, ignore_errors=True)


def _mkl_threads_local(n: int) -> int:
    """MKL threads for the calling thread only, so parts solved side by side share the cores. Returns the
    thread's previous setting (0: none of its own), to put back after a borrowed setting."""
    if _pardiso() is None:
        return 0
    try:
        import ctypes
        fn = ctypes.CDLL(os.environ["PYPARDISO_MKL_RT"]).MKL_Set_Num_Threads_Local
        fn.restype = ctypes.c_int
        return int(fn(ctypes.c_int(n)))
    except (OSError, KeyError, AttributeError):
        return 0


class _Threads:
    """MKL threads set for a block and put back after: a kept factor is solved with the threads it was analysed
    with, whichever thread solves it (PARDISO sizes its per-thread work at analysis; changing the threads
    between its phases crashed it)."""

    def __init__(self, n: int):
        self.n = n

    def __enter__(self):
        self.prev = _mkl_threads_local(self.n) if self.n else None

    def __exit__(self, *exc):
        if self.prev is not None:
            _mkl_threads_local(self.prev)


def upper_triangle(A, scale: np.ndarray | None = None) -> sp.csr_matrix:
    """The upper triangle of the square matrix A as sorted CSR, every diagonal entry stored (PARDISO's input for a
    symmetric matrix), optionally scaled to s_i a_ij s_j. Taken straight from A's rows: through sp.triu, COO and
    sorting, the 400 M-entry joint system of a frame with exact chord welds spent 20 s here before PARDISO began."""
    A = sp.csr_matrix(A)
    if not A.has_canonical_format:
        A = A.copy()
        A.sum_duplicates()
    n = A.shape[0]
    rows = np.repeat(np.arange(n, dtype=np.int32), np.diff(A.indptr))
    keep = A.indices >= rows
    r, c, v = rows[keep], A.indices[keep], A.data[keep]
    del rows, keep
    if scale is not None:
        v = scale[r] * v * scale[c]                      # the rounding of S @ A @ S
    diag = np.zeros(n, bool)
    diag[r[r == c]] = True
    if diag.all():
        indptr = np.zeros(n + 1, dtype=A.indptr.dtype)
        np.cumsum(np.bincount(r, minlength=n), out=indptr[1:])
        U = sp.csr_matrix((v, c, indptr), shape=(n, n))
        U.has_sorted_indices = True                      # A's rows were sorted, and a row's tail stays so
        return U
    miss = np.nonzero(~diag)[0]                           # a zero diagonal entry still has to be stored
    U = sp.coo_matrix((np.concatenate([v, np.zeros(len(miss))]),
                       (np.concatenate([r, miss]), np.concatenate([c, miss]))), shape=(n, n)).tocsr()
    U.sort_indices()
    return U


class SchurFactor:
    """MKL PARDISO's partial factorisation of the symmetric A = [A_II A_IB; A_BI A_BB] (A_II definite, A itself only
    semi-definite for a part held by its joints alone) with the dense Schur complement S onto its last ns rows and
    columns (iparm[35] = 2), kept for partial solves (phases
    331-333): ``interior`` gives A_II^-1 (b_I - A_IB u_B) for any b_I and u_B. So one factorisation gives a part's
    condensed matrix, its response to its loads and, when the factor is kept, its recovery; a part condensed by
    its Schur complement was factorised three times before (D62). Measured on Bench truss S's chord (152k
    interior DOF, 1530 joint modes): the Schur complement in 3.5 s against 56 s for solving the modes as
    right-hand sides, the same matrix to 1e-13. Raises RuntimeError on a PARDISO error."""

    def __init__(self, A, ns: int, budget: "MemoryBudget | None" = None, partial: bool = True):
        """``partial=False``: only S, as before D62 (Cholesky, iparm[35] = 1, the Schur block not factorised, the
        factor released at once; ``interior`` is then unavailable). For a wide joint boundary, where factorising
        the dense Schur block costs more than factorising the part again (SCHUR_PARTIAL_MAX_BDOF)."""
        import ctypes
        self._ct = ctypes
        self._lib = ctypes.CDLL(os.environ["PYPARDISO_MKL_RT"])
        self.n, self.ns = A.shape[0], ns
        U = upper_triangle(A)
        self._ia = (U.indptr + 1).astype(np.int32)
        self._ja = (U.indices + 1).astype(np.int32)
        self._a = np.ascontiguousarray(U.data, dtype=np.float64)
        del U
        self._pt = np.zeros(64, dtype=np.int64)
        self._iparm = np.zeros(64, dtype=np.int32)
        # own settings, METIS, perturbation 1e-8, Schur complement with the factor kept for partial solves.
        # Symmetric INDEFINITE (mtype -2): kept for partial solves, PARDISO factorises the Schur block too, and a
        # part held only by its joints has a singular one (its rigid-body motions): Cholesky (mtype 2) stopped on
        # the frame's first rung with a zero pivot (error -4). The indefinite mode perturbs that pivot, which
        # touches only the Schur block's own factor: S is returned before it, and ``interior`` overwrites that
        # block's part of the solve with u_B
        self._mtype = -2 if partial else 2
        self._iparm[0], self._iparm[1], self._iparm[9], self._iparm[35] = 1, 2, 8, 2 if partial else 1
        self._perm = np.zeros(self.n, dtype=np.int32)
        self._perm[self.n - ns:] = 1
        self._budget, self._held = budget, 0
        self.threads = budget.threads() if budget is not None else 0
        self._live = True
        x = np.zeros(ns * ns)
        with _Threads(self.threads):
            # the threads are fixed before analysis (see _Threads)
            if budget is not None:
                self._call(11, np.zeros(self.n), x, 1)          # analysis: the factor's memory, then reserve it
                self._held = 1024 * max(int(self._iparm[14]), int(self._iparm[15]) + int(self._iparm[16]))
                budget.acquire(self._held + 8 * ns * ns, holding=True)
                try:
                    self._call(22, np.zeros(self.n), x, 1)
                finally:
                    budget.release(8 * ns * ns, last=False)    # S is the caller's, inside its own estimate
            else:
                self._call(12, np.zeros(self.n), x, 1)
        self.S = x.reshape(ns, ns)
        if not partial:
            S = self.S
            self.free()                                         # nothing to solve with later
            self.S = S

    @property
    def nbytes(self) -> int:
        """The factor's memory as PARDISO's analysis gave it (0 when no budget was asked)."""
        return self._held

    def _call(self, phase: int, b: np.ndarray, x: np.ndarray, nrhs: int) -> None:
        ct = self._ct
        i32p, f64p = ct.POINTER(ct.c_int32), ct.POINTER(ct.c_double)
        err = ct.c_int32(0)
        self._lib.pardiso(self._pt.ctypes.data_as(ct.POINTER(ct.c_int64)), ct.byref(ct.c_int32(1)),
                          ct.byref(ct.c_int32(1)), ct.byref(ct.c_int32(self._mtype)), ct.byref(ct.c_int32(phase)),
                          ct.byref(ct.c_int32(self.n)), self._a.ctypes.data_as(f64p), self._ia.ctypes.data_as(i32p),
                          self._ja.ctypes.data_as(i32p), self._perm.ctypes.data_as(i32p), ct.byref(ct.c_int32(nrhs)),
                          self._iparm.ctypes.data_as(i32p), ct.byref(ct.c_int32(0)), b.ctypes.data_as(f64p),
                          x.ctypes.data_as(f64p), ct.byref(err))
        if err.value:
            if phase != -1:
                self.free()
            # worded as pypardiso words its errors, so the out-of-memory retry (solve.run_hybrid) knows -2
            raise RuntimeError(f"PARDISO Schur complement failed (phase {phase}, error code {err.value})")

    def interior(self, bI: np.ndarray, uB: np.ndarray) -> np.ndarray:
        """A_II^-1 (b_I - A_IB u_B), (n - ns, k) for k columns of b_I (n - ns, k) and u_B (ns, k). Forward
        substitution (331) leaves the reduced load on B, the diagonal phase (332) solves the Schur block there;
        that part is replaced by u_B and backward substitution (333) gives the interior. Checked against SciPy to
        1e-15, one or several columns (tests/test_hybrid_global.py)."""
        ni = self.n - self.ns
        k = bI.shape[1]
        b = np.zeros((self.n, k), order="F")
        b[:ni] = bI
        y = np.zeros((self.n, k), order="F")
        with _Threads(self.threads):
            self._call(331, b, y, k)
            z = np.zeros((self.n, k), order="F")
            self._call(332, y, z, k)
            z[ni:] = uB
            self._call(333, z, y, k)
        return np.array(y[:ni])

    def release_to_kept(self) -> int:
        """The factor's reservation ends (the caller keeps its bytes in the budget's kept count): its size."""
        n, self._held = self._held, 0
        if n:
            self._budget.release(n, last=False)
        return n

    def free(self) -> None:
        if self._live:
            self._live = False
            self.S = None
            dummy = np.zeros(1)
            with _Threads(self.threads):
                self._call(-1, dummy, dummy, 1)
        if self._held:
            self._budget.release(self._held, last=False)
            self._held = 0


class MemoryBudget:
    """Bytes that parts may hold at once. A part reserves its estimate before its heavy work and gives it
    back after; while nothing else is held a part may start whatever its size, so one large part never
    waits forever. ``try_keep`` takes bytes until recovery (results kept in memory) only when they fit
    with room left for the largest part."""

    def __init__(self, total: int, cores: int = 1, reserve: int = 0):
        self.total = max(int(total), 1)
        self.cores = max(int(cores), 1)
        self.reserve = int(reserve)       # free memory the computer keeps: below it, parts wait their turn
        self.held = 0
        self.kept = 0
        self.peak = 0
        self.holders = 0                  # threads holding memory
        self.topping_up = 0               # of them, waiting for more
        self._cv = threading.Condition()

    def acquire(self, n: int, holding: bool = False) -> None:
        """Reserve n bytes. ``holding``: the caller already holds a reservation and is topping it up (a
        factorisation sized by PARDISO's analysis). It waits only while another holder is still working,
        so parts waiting on each other's memory cannot all wait for ever: the last one goes ahead."""
        with self._cv:
            if holding:
                self.topping_up += 1
                while self.kept + self.held + n > self.total and self.topping_up < self.holders:
                    self._cv.wait()
                self.topping_up -= 1
            else:
                # also wait while another part is working and other programs have taken the computer's free
                # memory below the reserve: the solve slows instead of pushing everything into the page file
                # (D60: a run is never stopped for memory, only slowed, and the user is told)
                while self.held > 0 and (self.kept + self.held + n > self.total or self._pressed()):
                    self._cv.wait(timeout=2.0)
                self.holders += 1
            self.held += n
            self.peak = max(self.peak, self.kept + self.held)

    def _pressed(self) -> bool:
        return bool(self.reserve) and psutil.virtual_memory().available < self.reserve

    def threads(self) -> int:
        """MKL threads for a part's next factorisation (set before its analysis and kept until it is freed): the
        cores shared by the parts working now (holding memory and not waiting for more). Fixed per part when the solve started, they left a part that memory let
        run alone on one thread while the other cores sat idle (a water tank's Schur complements, D57)."""
        with self._cv:
            return max(1, self.cores // max(1, self.holders - self.topping_up))

    def release(self, n: int, last: bool = True) -> None:
        """Give back n bytes; ``last``: the caller's whole reservation ends here (False for a top-up)."""
        with self._cv:
            self.held -= n
            if last:
                self.holders -= 1
            self._cv.notify_all()

    def try_keep(self, n: int, headroom: int) -> bool:
        with self._cv:
            if self.kept + self.held + n + headroom > self.total:
                return False
            self.kept += n
            self.peak = max(self.peak, self.kept + self.held)
            return True

    def unkeep(self, n: int) -> None:
        with self._cv:
            self.kept -= n
            self._cv.notify_all()


def memory_budget(settings) -> int:
    """The bytes the solve may use: as set, or the RAM available now less a reserve for everything else."""
    if settings.memory_bytes:
        return int(settings.memory_bytes)
    avail = psutil.virtual_memory().available
    return int(avail - max(MEMORY_RESERVE_FRACTION * avail, MEMORY_RESERVE_MIN))


class Factor:
    """A factorised symmetric matrix: Intel MKL PARDISO (multi-threaded, as CalculiX uses here) through
    pypardiso when it is installed, else SciPy's SuperLU. ``definite=False`` is for the global system of
    joint modes, which is only semi-definite when a patch's modes are redundant (a footprint nearly a
    line): PARDISO's symmetric indefinite mode perturbs those zero pivots, and as redundant modes move no
    node, their values do not matter. ``solve`` takes one or many right-hand sides; ``free`` releases the
    factor at once rather than at garbage collection."""

    def __init__(self, A, definite: bool = True, budget: "MemoryBudget | None" = None):
        """``budget``: the caller holds a reservation in it; the factorisation's memory, as PARDISO's analysis
        reports it (iparm[14..16]), is added to that reservation before factorising and given back by
        ``free``. A guess per DOF was out by 2-3x on Bench truss L's chords."""
        A = sp.csr_matrix(A)
        self.n = A.shape[0]
        self._pardiso = None
        self._scale = None
        self._budget, self._held = budget, 0
        self.threads = 0
        pypardiso = _pardiso()
        if pypardiso is not None and self.n:
            if not definite:
                # The joint system is factorised scaled to a unit diagonal, with pivots perturbed at 1e-13 rather
                # than PARDISO's 1e-8 for this matrix type, and solved with refinement (solve). Unscaled, the
                # agreement penalty (PENALTY_SCALE x the stiffest unknown) put entries 1e12 above the softest
                # joint unknowns, the 1e-8 perturbation swamped their pivots and a 13 M DOF water tank's joint
                # system came back with a relative residual of 1.3 (D58). Scaled: 1.7e-5 after one refinement.
                d = A.diagonal()
                self._scale = 1.0 / np.sqrt(np.where(d > 0, d, 1.0))
            s = pypardiso.PyPardisoSolver(mtype=2 if definite else -2)   # real symmetric (indefinite)
            if not definite:
                s.set_iparm(1, 1)                                 # own settings: METIS ordering,
                s.set_iparm(2, 2)
                s.set_iparm(10, 13)                               # pivots perturbed at 1e-13
            # PARDISO reads the upper triangle, with every diagonal entry stored even when zero (the
            # multiplier block of a constrained system)
            self._A = upper_triangle(A, self._scale)
            del A
            s._check_A(self._A)
            b = np.zeros((self.n, 1))
            # the threads are fixed from analysis on (see _Threads): a kept factor may be solved from another thread
            self.threads = budget.threads() if budget is not None else 0
            with _Threads(self.threads):
                s.set_phase(11)                                   # analysis: reports the memory to come
                s._call_pardiso(self._A, b)
                if budget is not None:
                    self._held = 1024 * max(int(s.iparm[14]), int(s.iparm[15]) + int(s.iparm[16]))
                    budget.acquire(self._held, holding=True)
                s.set_phase(22)                                   # numerical factorisation
                s._call_pardiso(self._A, b)
            self._pardiso = s
        else:
            self._lu = spla.splu(A.tocsc()) if self.n else None

    @property
    def nbytes(self) -> int:
        """The factor's memory as PARDISO's analysis gave it (0 when no budget was asked)."""
        return self._held

    def _solve33(self, B: np.ndarray) -> np.ndarray:
        """PARDISO's solve phase on the stored factor. pypardiso's own solve checks that the matrix is the one
        factorised by hashing it (or comparing a copy it keeps) at every call: seconds on a large joint system."""
        s = self._pardiso
        b = s._check_b(self._A, B)
        with _Threads(self.threads):
            s.set_phase(33)
            return s._call_pardiso(self._A, b)

    def solve(self, B: np.ndarray) -> np.ndarray:
        B = np.asarray(B, dtype=float)
        if not self.n:
            return np.zeros_like(B)
        if self._pardiso is not None and self._scale is not None:
            # scaled: solve S A S z = S b and refine on that system, whose full matrix is the stored upper
            # triangle U as U + U' - diag(U), so no second copy is held
            S = self._scale.reshape(-1, *([1] * (B.ndim - 1)))
            bs = S * B
            dU = self._A.diagonal().reshape(S.shape)
            z = self._solve33(bs).reshape(B.shape)
            for _ in range(GLOBAL_REFINE_STEPS):
                r = bs - (self._A @ z + self._A.T @ z - dU * z)
                z = z + self._solve33(r).reshape(B.shape)
            X = S * z
        elif self._pardiso is not None:
            X = self._solve33(B)
        else:
            X = self._lu.solve(B)
        return X.reshape(B.shape)

    def release_to_kept(self) -> int:
        """The factor's reservation ends (the caller keeps its bytes in the budget's kept count): its size."""
        n, self._held = self._held, 0
        if n:
            self._budget.release(n, last=False)
        return n

    def free(self) -> None:
        if self._pardiso is not None:
            with _Threads(self.threads):
                self._pardiso.free_memory(everything=True)
            self._pardiso = None
        self._lu = None
        if self._held:
            self._budget.release(self._held, last=False)
            self._held = 0


# --------------------------------------------------------------------------- assembly

def _material_of(deck: Deck) -> dict:
    mat = {}
    for elset, m in deck.sections:
        for e in deck.element_ids(elset):
            mat[int(e)] = m
    return mat


def assemble(deck: Deck, blocks: list, index: dict, steps_gravity: list):
    """K (n, n) CSR over the dofs of ``index`` (node id -> local node index), and body loads (n, n_steps)."""
    n = 3 * len(index)
    K = sp.csr_matrix((n, n))
    F = np.zeros((n, len(steps_gravity)))
    for etype, eids, conn, matname in blocks:
        m = deck.materials[matname]
        loc = np.vectorize(index.__getitem__)(conn)
        dofs = (3 * loc[:, :, None] + np.arange(3)).reshape(len(conn), -1)
        X = np.array([[deck.nodes[int(v)] for v in row] for row in conn])
        for a in range(0, len(conn), ELEMENT_CHUNK):
            sl = slice(a, a + ELEMENT_CHUNK)
            Ke = el.stiffness(etype, X[sl], m["E"], m["nu"])
            d = dofs[sl]
            r = np.repeat(d, d.shape[1], axis=1).ravel()
            c = np.tile(d, (1, d.shape[1])).ravel()
            K = K + sp.csr_matrix((Ke.ravel(), (r, c)), shape=(n, n))
        for k, grav in enumerate(steps_gravity):
            for elset_ids, mag, direc in grav:
                sel = np.isin(eids, elset_ids)
                if not sel.any() or "rho" not in m:
                    continue
                b = m["rho"] * mag * np.asarray(direc, float)
                fe = el.body_force(etype, X[sel], b)
                np.add.at(F[:, k], dofs[sel].ravel(), fe.reshape(len(fe), -1).ravel())
    return K, F


def constraint_matrix(n_nodes: int, index: dict, ties: dict) -> tuple[sp.csr_matrix, np.ndarray]:
    """C (3 n_nodes x 3 n_free) with u = C u_free, slaves following their master weights; free node mask."""
    ties = {s: t for s, t in ties.items() if s in index}
    # chained ties: a master node that is itself another tie's slave is replaced by its own masters, as
    # CalculiX resolves them (dropping it broke the load path: a welded chassis' reactions came out wrong). A
    # loop (two nodes slaved to each other) is broken by freeing the node where it closes.
    resolved: dict = {}

    def resolve(s, stack):
        if s in resolved:
            return resolved[s]
        stack = stack | {s}
        out: dict = {}
        for m, w in ties[s]:
            if m in ties and m not in stack:
                for mm, ww in resolve(m, stack).items():
                    out[mm] = out.get(mm, 0.0) + w * ww
            elif m in ties:                          # a loop: m is freed below
                out[m] = out.get(m, 0.0) + w
            else:
                out[m] = out.get(m, 0.0) + w
        resolved[s] = out
        return out
    for s in list(ties):
        resolve(s, frozenset())
    loops = {m for s in resolved for m in resolved[s] if m in ties}
    free = np.ones(n_nodes, bool)
    for s in ties:
        if s not in loops:
            free[index[s]] = False
    newidx = -np.ones(n_nodes, np.int64)
    newidx[free] = np.arange(free.sum())
    rows, cols, vals = [], [], []
    for i in np.nonzero(free)[0]:
        for d in range(3):
            rows.append(3 * i + d), cols.append(3 * newidx[i] + d), vals.append(1.0)
    for s, terms in resolved.items():
        if s in loops:
            continue
        i = index[s]
        for m, w in terms.items():
            j = index.get(m)
            if j is None or not free[j]:
                continue
            for d in range(3):
                rows.append(3 * i + d), cols.append(3 * newidx[j] + d), vals.append(w)
    C = sp.csr_matrix((vals, (rows, cols)), shape=(3 * n_nodes, 3 * int(free.sum())))
    return C, free


# --------------------------------------------------------------------------- patches

def monomials(s: int):
    return [(a, d - a) for d in range(s + 1) for a in range(d, -1, -1)]


def _legendre(x: np.ndarray, n: int) -> np.ndarray:
    P = [np.ones_like(x), x]
    for k in range(1, n):
        P.append(((2 * k + 1) * x * P[k] - k * P[k - 1]) / (k + 1))
    return np.stack(P[:n + 1], axis=0)


def patch_basis(P: np.ndarray, s: int, frame=None):
    """Values (k, n_mon) of the patch's polynomials of total degree <= s at points P: products of Legendre
    polynomials in the patch frame (centroid, principal axes, scaled to [-1, 1]), better conditioned than
    plain powers at higher degrees."""
    if frame is None:
        c = P.mean(axis=0)
        _, sv, vt = np.linalg.svd(P - c, full_matrices=False)
        e1, e2 = vt[0], vt[1]
        h1 = max(np.abs((P - c) @ e1).max(), 1e-9)
        h2 = max(np.abs((P - c) @ e2).max(), 1e-9)
        frame = (c, e1, e2, h1, h2)
    c, e1, e2, h1, h2 = frame
    y, z = ((P - c) @ e1) / h1, ((P - c) @ e2) / h2
    Ly, Lz = _legendre(y, s), _legendre(z, s)
    return np.stack([Ly[a] * Lz[b] for a, b in monomials(s)], axis=1), frame


def patch_degree(n_side: int, s_max: int, per: float = 1.0) -> int:
    """The highest degree (<= s_max) a patch's nodes can carry: each side needs ``per`` nodes per polynomial.
    A lower degree makes the joint stiffer, not safer (degree 0 lets it translate only): two nodes per
    polynomial took 45 % off a welded chassis' largest displacement."""
    if per <= 0:
        return s_max
    d = 0
    while d < s_max and (d + 2) * (d + 3) / 2 * per <= n_side:
        d += 1
    return d


# --------------------------------------------------------------------------- the solve

def exact_joints(feet: list, settings: Settings, node_part) -> list[bool]:
    """Which joints are exact: those whose master side has at most ``settings.exact_max_nodes`` nodes, smallest
    first, while every part they touch stays within ``settings.exact_part_unknowns`` exact joint unknowns
    (3 per master node, counted on both the master's and the slave's part: both condense onto them). Joints
    by smooth fields were 8.5-10 % soft at a frame's welds (a tube end on a tube's side, m4_frame), exact ones
    0.05 %; the cap keeps a part from condensing onto a dense block of joint unknowns (a tank panel with every
    tie exact: 119 000 of them, 105 GB)."""
    exact = [False] * len(feet)
    if settings.exact_max_nodes <= 0:
        return exact
    if node_part is None or settings.exact_part_unknowns <= 0:
        return [bool(sn and mn) and len(mn) <= settings.exact_max_nodes for sn, mn, _ in feet]
    used: dict = {}
    total = 0
    order = sorted((k for k, (sn, mn, _) in enumerate(feet) if sn and mn and len(mn) <= settings.exact_max_nodes),
                   key=lambda k: len(feet[k][1]))
    for k in order:
        sn, mn, _ = feet[k]
        cost = 3 * len(mn)
        if settings.exact_total_unknowns and total + cost > settings.exact_total_unknowns:
            # the global joint system grows with every exact joint: a tank with 387 exact joints (232 000 global
            # unknowns) ran out of memory there after 28 minutes; m4_frame's welds need 39 600 in all
            break
        touched = {int(node_part(next(iter(sn)))), int(node_part(next(iter(mn))))}
        if all(used.get(p, 0) + cost <= settings.exact_part_unknowns for p in touched):
            exact[k] = True
            total += cost
            for p in touched:
                used[p] = used.get(p, 0) + cost
    return exact


def joint_terms(deck: Deck, external_ties: list, settings: Settings, held: set, log=print, node_part=None,
                stats: dict | None = None):
    """The joints between parts: (terms, transfer, agree, number of global unknowns). See solve."""
    # the joints between parts. Each maps its nodes to global unknowns: terms[node] = [(base, weight)], the
    # node's displacement component c being sum(weight * q[base + c]). An exact joint's master nodes are
    # global unknowns themselves and its slave nodes follow the master face (as CalculiX's tie); a joint
    # by modes moves both sides with one smooth field over its footprint.
    terms: dict = {}
    gnode: dict = {}                                       # master node of an exact joint -> its base
    transfer: dict = {}                                    # exact-joint slave -> [(master node, weight)]
    offset = 0
    n_exact = n_modes = 0
    by_modes = []                                          # (slave nodes, master nodes) of joints by modes
    agree = []                                  # [(new terms, earlier terms, node)] per shared or fixed node
    feet = [tie_footprint(deck, tie, deck.nodes) for tie in external_ties]
    exact = exact_joints(feet, settings, node_part)
    for tie, (snodes, mnodes, weights), is_exact in zip(external_ties, feet, exact):
        if not snodes or not mnodes:
            log(f"tie {tie[0]}: no slave node lands on its master surface; left out")
            continue
        if not is_exact:
            by_modes.append((snodes, mnodes))
            continue
        n_exact += 1
        for m in sorted(mnodes):
            if m not in gnode and m not in terms and m not in held:
                gnode[m] = offset
                terms[m] = [(offset, 1.0)]
                offset += 3
        for s in sorted(snodes):
            if s in held:
                continue
            # Chains, resolved as CalculiX resolves chained ties (D57): a master that an earlier joint already
            # moves (a slave there) brings its own terms; a fixed master moves nothing, so it adds no term but
            # keeps its weight. Dropping such masters and skipping slaves already moved lost 64 000 master
            # weights and 29 000 slave nodes on a sectional tank (foam strips are slave to one panel and master
            # to the next), and the model came apart ("the joints do not hold the model together").
            ws = [(m, w) for m, w in weights[s] if m in terms or m in held]
            tot = sum(w for _, w in ws)
            if not ws or abs(tot) <= 1e-9:
                continue
            acc: dict = {}
            for m, w in ws:
                for base, ww in terms.get(m, ()):
                    acc[base] = acc.get(base, 0.0) + w / tot * ww
            mine = list(acc.items())
            if s in terms:
                # a node already moved by an earlier joint: both joints hold it, so the two must agree there
                # (a penalty row, as where fields share nodes)
                if mine != terms[s]:
                    agree.append((mine, terms[s], s))
                continue
            terms[s] = mine
            transfer[s] = [(m, w / tot) for m, w in ws]
    # each joint by modes moves its footprint with its own smooth field. A node can follow only one field:
    # where joints share nodes (three bodies meeting at a weld) a node keeps the field that took it first,
    # and the later joint's field is made to agree with it there (a constraint row each: Lagrange
    # multipliers in the global system). Left out, a joint whose nodes on one side were all taken joined
    # nothing (the 297-body weldment fell apart: 1e9 mm); merging such joints into one field put a single
    # polynomial round the corner (a welded chassis' worst node 7 % off).
    for sn, mn in by_modes:
        n_modes += 1
        nodes = sorted(sn | mn)
        P = np.array([deck.nodes[n] for n in nodes])
        deg = patch_degree(min(len(sn), len(mn)), settings.degree, settings.nodes_per_mode)
        phi, _ = patch_basis(P, deg)
        # a joint onto a fixture (most of one side fixed) is pinned at its fixed nodes: left free there,
        # parts joined only to fixture plates floated (srleg). Pinned at a lone fixed node of a larger
        # joint, the whole field stiffened (welded chassis: -5.8 % against CalculiX, -3.0 % unpinned).
        pin = max(len(sn & held) / len(sn), len(mn & held) / len(mn)) >= PIN_FIXED_FRACTION
        for r, nd in enumerate(nodes):
            mine = [(offset + 3 * mm, float(phi[r, mm])) for mm in range(phi.shape[1])]
            if pin and nd in held:
                agree.append((mine, [], nd))
            elif nd in terms:
                agree.append((mine, terms[nd], nd))
            else:
                terms[nd] = mine
        offset += 3 * phi.shape[1]
    nq = offset
    log(f"joints: {n_exact} exact, {n_modes} by modes; {nq} global unknowns; {len(agree)} nodes where two joints agree")
    if stats is not None:                                  # for the run's accuracy grade (D60)
        stats.update(joints_exact=n_exact, joints_fields=n_modes)
    return terms, transfer, agree, nq


def solve(deck: Deck, parts: list[PartData], external_ties: list, settings: Settings, log=print):
    """(displacements (top, 3, n_steps), internal nodal forces (top, 3, n_steps), timings), both indexed by
    node id (rows of ids not in the deck stay zero). The nodal forces are what CalculiX prints as RF:
    reactions at fixtures, tie forces at slave nodes."""
    # factors kept from condensing to recovery are freed here whatever happens: a failed solve's error (and its
    # traceback, so its frames) outlives it in the out-of-memory retry, which needs their memory back
    kept_factors: list = []
    try:
        return _solve(deck, parts, external_ties, settings, log, kept_factors)
    finally:
        for f in kept_factors:
            f.free()


def _solve(deck: Deck, parts: list[PartData], external_ties: list, settings: Settings, log, kept_factors: list):
    t0 = time.perf_counter()
    nsteps = len(deck.steps)
    fixed_nodes = {}
    for setname, d0, d1 in deck.boundary:
        for nd in deck.node_ids(setname):
            fixed_nodes.setdefault(int(nd), set()).update(range(d0, d1 + 1))
    gravity = [[(deck.element_ids(es), mag, direc) for es, mag, direc in st.gravity] for st in deck.steps]
    cload = {}
    for k, st in enumerate(deck.steps):
        for nd, d, v in st.cloads:
            cload.setdefault(int(nd), np.zeros((3, nsteps)))[d - 1, k] += v
    timings = {"setup_s": time.perf_counter() - t0}

    if settings.exact:
        allp = PartData("model", np.array(sorted(deck.nodes)), [b for p in parts for b in p.blocks],
                        [t for p in parts for t in p.internal_ties] + list(external_ties))
        parts, external_ties = [allp], []

    held = {nd for nd, d in fixed_nodes.items() if d >= {1, 2, 3}}   # fully fixed: no unknowns of its own
    top = max(int(p.nodes.max()) for p in parts if len(p.nodes)) + 1 if parts else 1
    part_of = np.full(top, -1, dtype=np.int64)
    for k, p in enumerate(parts):
        part_of[p.nodes] = k
    terms, transfer, agree, nq = joint_terms(deck, external_ties, settings, held, log,
                                             node_part=lambda nd: part_of[nd] if nd < top else -1, stats=timings)
    if settings.split_dof and not settings.exact:
        pieces = []
        for part in parts:
            pieces += split_part(deck, part, settings.split_dof)
        if len(pieces) > len(parts):
            log(f"{len(pieces) - len(parts) + sum(1 for p in pieces if p.name.endswith('/1]'))} large part(s) cut into "
                f"slabs: {len(parts)} parts became {len(pieces)} pieces")
        parts = pieces
        # a node shared by slabs is a global unknown of its own, unless a joint field already moves it
        for part in parts:
            for n in sorted(part.shared):
                if n not in terms and n not in held:
                    terms[n] = [(nq, 1.0)]
                    nq += 3

    # The joint terms and the nodal loads as flat arrays, built once here: the part threads look them up with
    # NumPy, which leaves the GIL free (Python loops over them held it, and four part threads shared one core:
    # a 13 M DOF water tank, D57). term_nodes sorted; a node's terms are term_base/term_w[term_ptr[k]:term_ptr[k+1]].
    term_nodes = np.array(sorted(terms), dtype=np.int64)
    term_cnt = np.array([len(terms[int(nd)]) for nd in term_nodes], dtype=np.int64)
    term_ptr = np.concatenate([[0], np.cumsum(term_cnt)]).astype(np.int64)
    term_base = np.fromiter((b for nd in term_nodes for b, _ in terms[int(nd)]), dtype=np.int64, count=int(term_ptr[-1]))
    term_w = np.fromiter((w for nd in term_nodes for _, w in terms[int(nd)]), dtype=float, count=int(term_ptr[-1]))
    load_nodes = np.array(sorted(cload), dtype=np.int64)
    load_vals = np.array([cload[int(nd)] for nd in load_nodes]).reshape(len(load_nodes), 3, nsteps) \
        if len(load_nodes) else np.zeros((0, 3, nsteps))
    fixed_ids = np.array(sorted(fixed_nodes), dtype=np.int64)
    fixed_mask = np.array([[d in fixed_nodes[int(nd)] for d in (1, 2, 3)] for nd in fixed_ids], dtype=bool).reshape(-1, 3)

    def node_terms(nodes):
        """For each node: where its terms are (a row of the term arrays), -1 for a node with none."""
        k = np.searchsorted(term_nodes, nodes)
        k = np.minimum(k, max(len(term_nodes) - 1, 0))
        hit = (term_nodes[k] == nodes) if len(term_nodes) else np.zeros(len(nodes), bool)
        return np.where(hit, k, -1)

    def loads(part, index):
        K, F = assemble(deck, part.blocks, index, gravity)
        if len(load_nodes):
            here = np.isin(load_nodes, part.nodes)
            if not part.loads_here and part.shared:
                here &= ~np.isin(load_nodes, np.fromiter(part.shared, dtype=np.int64))
            if here.any():
                order = np.argsort(part.nodes)
                at = order[np.searchsorted(part.nodes[order], load_nodes[here])]
                F3 = F.reshape(len(part.nodes), 3, -1)          # a view: F is (3 x nodes) x load steps
                np.add.at(F3, at, load_vals[here])
        return K, F

    kr, kc, kv = [], [], []                               # the global system: triplets of the latest parts,
    Kq = sp.csr_matrix((nq, nq))                          # folded into a sparse matrix every few million

    def fold(Kq):
        if kv:
            Kq = Kq + sp.csr_matrix((np.concatenate(kv), (np.concatenate(kr), np.concatenate(kc))), shape=(nq, nq))
            kr.clear(), kc.clear(), kv.clear()
        return Kq
    Fq = np.zeros((nq, nsteps))
    cores = psutil.cpu_count(logical=False) or 1
    budget = MemoryBudget(memory_budget(settings), cores, settings.reserve_bytes)
    _remove_stale_spills()
    spill = tempfile.TemporaryDirectory(prefix=f"asmfea_hybrid_{os.getpid()}_", ignore_cleanup_errors=True)
    _pardiso()                                            # found once, before the threads start

    def prepare(part):
        """The cheap half of a part: its constraints and joint map, and so the size of its heavy half."""
        index = {int(nd): i for i, nd in enumerate(part.nodes)}
        ties = collect_ties(deck, part.internal_ties, fixed_nodes)
        C, free = constraint_matrix(len(part.nodes), index, ties)
        free_nodes = part.nodes[free]
        n = 3 * len(free_nodes)
        fixed = np.zeros((len(free_nodes), 3), bool)
        if len(fixed_ids):
            k = np.minimum(np.searchsorted(fixed_ids, free_nodes), len(fixed_ids) - 1)
            hit = fixed_ids[k] == free_nodes
            fixed[hit] = fixed_mask[k[hit]]
        fixed = fixed.ravel()
        tk = node_terms(free_nodes)
        bdof = np.repeat(tk >= 0, 3) & ~fixed
        idof = ~bdof & ~fixed
        I, B = np.nonzero(idof)[0], np.nonzero(bdof)[0]
        # each boundary DOF's row: its node's terms, the base shifted by its direction (vectorised, see above)
        rk = tk[B // 3]
        cnt = term_cnt[rk]
        if len(B) and cnt.sum():
            start = np.repeat(term_ptr[rk], cnt)
            offs = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
            idx = start + offs
            rows = np.repeat(np.arange(len(B)), cnt)
            cols = term_base[idx] + np.repeat(B % 3, cnt)
            T = sp.csr_matrix((term_w[idx], (rows, cols)), shape=(len(B), nq))
        else:
            T = sp.csr_matrix((len(B), nq))
        used = np.unique(T.indices) if len(B) else np.zeros(0, np.int64)
        return index, C, free, I, B, T[:, used], used, n

    def estimate(n_dof, n_int, n_used, n_bdof=0):
        """A part's memory before its factorisation (which reserves PARDISO's own figure when it starts):
        stiffness copies, interior responses in blocks, the condensed matrix."""
        return int(n_dof * PART_BYTES_PER_DOF + 8 * n_int * (min(n_used, RHS_BLOCK) + nsteps) * 3 + 16 * n_used ** 2
                   + 8 * n_bdof ** 2)

    def reduce(part, threads):
        """One part condensed onto its joint unknowns, within the memory budget. The interior response to the
        joint modes is solved in blocks of RHS_BLOCK columns, so a part with many modes never holds it whole;
        it is kept for recovery only while memory allows, and otherwise recovery factorises the part again
        (found: Bench truss S's chords, 1530 modes on 158k DOF, held 8 GB each and outran everything else)."""
        _mkl_threads_local(threads)
        index, C, free, I, B, Tu, used, n = prepare(part)
        # the dense Schur block only for a part that will build one: a tank panel (more joint DOF than
        # SCHUR_MAX_BDOF) was reserved 29 GB it never used, so parts ran one at a time on two threads
        schur = len(used) > SCHUR_MIN_MODES and len(B) <= SCHUR_MAX_BDOF
        need = estimate(3 * len(part.nodes), len(I), len(used), len(B) if schur else 0)
        budget.acquire(need)
        _mkl_threads_local(budget.threads())
        lu = None
        try:
            K, F = loads(part, index)
            Kf = (C.T @ K @ C).tocsr()
            Ff = C.T @ F
            KIB_T = (Kf[I][:, B] @ Tu).tocsc()                # sparse: only interior nodes next to joints
            XT = None
            Kred = Fpart = None
            if schur and len(I) and _pardiso() is not None:
                # many modes on a modest boundary: the condensed matrix straight from PARDISO's Schur complement.
                # Up to SCHUR_PARTIAL_MAX_BDOF joint DOF the same factor gives the response to the loads (and
                # recovery, see SchurFactor); on a wider boundary the part is factorised once more for that
                order = np.concatenate([I, B])
                partial = len(B) <= min(SCHUR_PARTIAL_MAX_BDOF, SCHUR_PARTIAL_MAX_FRACTION * n)
                sf = SchurFactor(Kf[order][:, order].tocsr(), len(B), budget, partial=partial)
                Kred = np.asarray(Tu.T @ (Tu.T @ sf.S).T)   # Tu' S Tu, S symmetric
                sf.S = None                                 # the dense block is done with (6.6 GB on a tank panel)
                if partial:
                    lu = sf
                    Xf = lu.interior(Ff[I], np.zeros((len(B), nsteps)))
                else:
                    lu = Factor(Kf[I][:, I], budget=budget)
                    Xf = lu.solve(Ff[I]).reshape(len(I), nsteps)
                Fpart = Tu.T @ Ff[B] - KIB_T.T @ Xf
            else:
                lu = Factor(Kf[I][:, I], budget=budget)
                Xf = lu.solve(Ff[I]).reshape(len(I), nsteps) if len(I) else np.zeros((0, nsteps))
            by_rhs = Kred is None and len(used) and len(I)  # condensed below, the joint modes as right-hand sides
            if by_rhs and budget.try_keep(8 * len(I) * len(used), largest):
                XT = np.empty((len(I), len(used)))
            elif by_rhs and 8 * len(I) * len(used) <= XT_SPILL_MAX and \
                    shutil.disk_usage(spill.name).free > 8 * len(I) * len(used) + DISK_RESERVE:
                # no room in memory: the response goes to disk as it is solved, and recovery reads it back
                # (Bench truss L: 292 lacing parts refactorised at recovery took it from 66 s to 279 s)
                XT = np.lib.format.open_memmap(Path(spill.name) / f"{id(part)}.npy", mode="w+",
                                               dtype=np.float64, shape=(len(I), len(used)))
            if Kred is None and len(used):
                Kred = (Tu.T @ Kf[B][:, B] @ Tu).toarray()
                for c0 in range(0, len(used), RHS_BLOCK):
                    c1 = min(c0 + RHS_BLOCK, len(used))
                    rhs = KIB_T[:, c0:c1].toarray()
                    Y = lu.solve(rhs).reshape(rhs.shape) if len(I) else rhs
                    Kred[:, c0:c1] -= KIB_T.T @ Y
                    if XT is not None:
                        XT[:, c0:c1] = Y
                Fpart = Tu.T @ Ff[B] - KIB_T.T @ Xf
            if isinstance(XT, np.memmap):
                XT.flush()
                path = Path(XT.filename)
                del XT
                XT = path
            # no interior response kept: the factor itself waits for recovery while memory allows, so recovery
            # solves instead of factorising the part again (D62: the frame's 53 parts all factorised twice more)
            kept_factor = 0
            if XT is None and len(used) and len(I):
                size = lu.release_to_kept()
                if size and budget.try_keep(size, largest):
                    kept_factor = size
                    kept_factors.append(lu)                 # list.append is atomic: part threads share it
            if not kept_factor:
                lu.free()
                lu = None
            del Kf
            if not budget.try_keep(K.data.nbytes + K.indices.nbytes + K.indptr.nbytes + F.nbytes, largest):
                K = F = None
            return (part, index, C, free, I, B, Tu, used, XT, Xf, n, K, F, KIB_T, lu, kept_factor), Kred, Fpart
        except BaseException:
            if lu is not None:
                lu.free()                                   # idempotent: a kept one is freed again harmlessly
            raise
        finally:
            budget.release(need)

    def gather(k, rec, Kred, Fpart, done):
        nonlocal Kq
        recovery[k] = rec
        used = rec[7]
        if Kred is not None:
            r_, c_ = np.meshgrid(used, used, indexing="ij")
            kr.append(r_.ravel().astype(np.int32)), kc.append(c_.ravel().astype(np.int32)), kv.append(Kred.ravel())
            # folded when the pending triplets outgrow the matrix so far (at least 4 M): each fold costs the
            # whole matrix, so a fixed step made the frame's 400 M-entry joint system quadratic to build
            if sum(len(v) for v in kv) > max(4_000_000, Kq.nnz):
                Kq = fold(Kq)
            Fq[used] += Fpart
        log(f"part {done}/{len(parts)} {parts[k].name}: {len(parts[k].nodes)} nodes, "
            f"{len(rec[5]) // 3} interface nodes, {len(used)} modes")

    t1 = time.perf_counter()
    # a part's work: its DOF times the right-hand sides it solves (its joint modes). A part with more than
    # a core's share of the whole runs alone on all cores first; the rest share the cores one each
    modes_of = []
    for part in parts:
        tk = node_terms(part.nodes)
        tk = tk[tk >= 0]
        cnt = term_cnt[tk]
        if cnt.sum():
            idx = np.repeat(term_ptr[tk], cnt) + np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
            modes_of.append(3 * len(np.unique(term_base[idx])))
        else:
            modes_of.append(0)
    work = [3 * len(p.nodes) * (m + nsteps) for p, m in zip(parts, modes_of)]
    share = sum(work) / cores
    heavy = sorted((k for k in range(len(parts)) if work[k] > share), key=lambda k: -work[k])
    light = sorted((k for k in range(len(parts)) if work[k] <= share), key=lambda k: -work[k])
    big = max((3 * len(parts[k].nodes) for k in light), default=0)
    # what stays free while results are kept for recovery: the largest light part, or the global joint
    # system built after all parts (its triplets, working copies and factor; about 12 bytes an entry, five
    # times over, plus a dense-ish factor). Found: Bench truss L kept 25 GB of results and the global solve
    # (175 M entries from its chords' 6570 modes each) took the process to 46 GB on a 28 GB budget.
    global_est = int(GLOBAL_BYTES_PER_ENTRY * sum(m * m for m in modes_of) + 4 * float(nq) ** 2)
    part_est = estimate(big, big, max((modes_of[k] for k in light), default=0)) if light else 0
    largest = max(part_est, global_est)                  # headroom left free when keeping results
    workers = settings.workers or max(1, min(cores, len(light) or 1, budget.total // max(part_est, 1)))
    threads = max(1, cores // workers)
    timings.update(workers=workers, heavy_parts=len(heavy), memory_budget_gb=budget.total / 2 ** 30)
    log(f"parts: {len(heavy)} heavy one at a time on {cores} threads, then {workers} at once, {threads} thread(s) "
        f"each; memory budget {budget.total / 2 ** 30:.1f} GB")
    top = sorted(range(len(parts)), key=lambda k: -modes_of[k])[:5]
    log("most joint modes: " + ", ".join(f"{parts[k].name} {modes_of[k]} on {3 * len(parts[k].nodes)} DOF "
                                          f"({8 * modes_of[k] ** 2 / 2 ** 30:.1f} GB condensed)" for k in top))
    recovery = [None] * len(parts)
    done = 0
    for k in heavy:
        done += 1
        gather(k, *reduce(parts[k], cores), done)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(reduce, parts[k], threads): k for k in light}
        for fut in as_completed(futures):
            done += 1
            gather(futures[fut], *fut.result(), done)
    timings["parts_s"] = time.perf_counter() - t1
    t2 = time.perf_counter()
    q = np.zeros((nq, nsteps))
    lam = np.zeros((0, 3, nsteps))
    Kq = fold(Kq)
    log(f"joint system: {nq} unknowns, {Kq.nnz / 1e6:.0f} M entries ({Kq.nnz / max(nq, 1) ** 2:.0%} full)")
    if nq and Kq.nnz:
        G = None
        if agree:
            gr, gc, gv = [], [], []
            for k, (mine, earlier, _) in enumerate(agree):
                for c in range(3):
                    row = 3 * k + c
                    for base, w in mine:
                        gr.append(row), gc.append(base + c), gv.append(w)
                    for base, w in earlier:
                        gr.append(row), gc.append(base + c), gv.append(-w)
            G = sp.csr_matrix((gv, (gr, gc)), shape=(3 * len(agree), nq))
        if os.environ.get("ASMFEA_HYBRID_DUMP"):          # investigation: the global system as solved
            d = Path(os.environ["ASMFEA_HYBRID_DUMP"])
            d.mkdir(parents=True, exist_ok=True)
            sp.save_npz(d / "Kq.npz", Kq.tocsr())
            np.save(d / "Fq.npy", Fq)
            if G is not None:
                sp.save_npz(d / "G.npz", G.tocsr())
        live = np.abs(Kq).sum(axis=1).A1 > 0
        if G is not None:
            live |= np.abs(G).sum(axis=0).A1 > 0
        act = np.nonzero(live)[0]
        Ka = Kq if len(act) == nq else Kq[act][:, act]       # no copy when every unknown is live
        del Kq
        alpha = 0.0
        if G is not None:
            # agreement by penalty: alpha G'G added to the joint stiffness. Lagrange multipliers were tried
            # first: redundant rows (several joints agreeing along one weld line) made the system
            # rank-deficient and PARDISO's perturbed pivots left srleg a residual of 1e-5 that refinement
            # drove to 1e-2. With alpha = PENALTY_SCALE x the stiffest joint unknown, a row is broken by
            # about 1/PENALTY_SCALE of the joint's own deflection.
            alpha = PENALTY_SCALE * float(np.abs(Ka.diagonal()).max() if Ka.nnz else 1.0)
            Ga = G[:, act]
            Ka = (Ka + alpha * (Ga.T @ Ga)).tocsc()
        rhs = Fq[act]
        gf = Factor(Ka, definite=False)
        sol = gf.solve(rhs).reshape(len(act), nsteps)
        gf.free()
        q[act] = sol
        if G is not None:
            lam = (alpha * (G @ q)).reshape(-1, 3, nsteps)   # the force each agreement row carries
        timings["agree_rows"] = 3 * len(agree)
        # PARDISO perturbs zero pivots instead of failing, so a model that falls apart (parts joined to
        # nothing) would come back as a solution: the residual tells
        res = np.linalg.norm(Ka @ sol - rhs) / max(np.linalg.norm(rhs), 1e-300)
        timings["global_residual"] = float(res)
        if res > GLOBAL_RESIDUAL_MAX:
            raise Singular(f"the joints do not hold the model together (relative residual {res:.1e})")
    timings["global_s"] = time.perf_counter() - t2
    timings["modes"] = int(nq)
    top = int(max(deck.nodes)) + 1
    U = np.zeros((top, 3, nsteps))                         # by node id
    RF = np.zeros((top, 3, nsteps))
    resid = {}                                             # an exact-joint slave's tie force, K u - F
    lock = threading.Lock()

    def recover(rec, threads):
        _mkl_threads_local(threads)
        part, index, C, free, I, B, Tu, used, XT, Xf, n, K, F, KIB_T, fac, kept = rec
        if isinstance(XT, Path):
            XT = np.load(XT, mmap_mode="r")                # waited on disk
        elif XT is not None:
            kept += XT.nbytes
        if K is not None:
            kept += K.data.nbytes + K.indices.nbytes + K.indptr.nbytes + F.nbytes
        need = 0
        if K is None or (XT is None and fac is None and len(used) and len(I)):
            need = estimate(3 * len(part.nodes), len(I), 0)
            budget.acquire(need)
            _mkl_threads_local(budget.threads())
        if K is None:
            K, F = loads(part, index)                      # assembled again: memory was short
        try:
            uf = np.zeros((n, nsteps))
            if len(B):
                uf[B] = Tu @ q[used]
            if len(I) and len(used):
                if XT is not None:
                    uf[I] = Xf - np.asarray(XT @ q[used])
                elif isinstance(fac, SchurFactor):         # its factor kept: A_II^-1 (0 - A_IB u_B)
                    uf[I] = Xf + fac.interior(np.zeros((len(I), nsteps)), uf[B])
                elif fac is not None:
                    uf[I] = Xf - fac.solve(KIB_T @ q[used]).reshape(len(I), nsteps)
                else:                                      # factorised again: memory was short
                    Kf = (C.T @ K @ C).tocsr()
                    lu = Factor(Kf[I][:, I], budget=budget if need else None)
                    uf[I] = Xf - lu.solve(KIB_T @ q[used]).reshape(len(I), nsteps)
                    lu.free()
                    del Kf
            elif len(I):
                uf[I] = Xf
            XT = None
            u = C @ uf
            # RF as CalculiX prints it: the node's internal force, which at an independent node includes
            # what its tied slave nodes pass on (without it, force tied into a fixed node went missing from
            # the reaction totals); a slave node keeps its own elements' force
            r = K @ u
            rf = (C.T @ r).reshape(-1, 3, nsteps)
            rr = r.reshape(-1, 3, nsteps).copy()
            rr[free] = rf
            res = (C.T @ (r - F)).reshape(-1, 3, nsteps)
            fidx = np.cumsum(free) - 1
            with lock:                                     # slabs of a split part share their cut nodes
                U[part.nodes] = u.reshape(-1, 3, nsteps)
                RF[part.nodes] += rr
            mine = {s: res[fidx[index[s]]] for s in transfer if s in index and free[index[s]]}
            with lock:
                resid.update(mine)
        finally:
            if fac is not None:
                fac.free()
            if need:
                budget.release(need)
            budget.unkeep(kept)

    t3 = time.perf_counter()
    n_kept = sum(1 for rec in recovery if rec is not None and rec[14] is not None)
    log(f"recovering parts: {n_kept} of {len(parts)} kept their factor")
    for k in heavy:                                        # as when condensing: heavy parts alone, all cores
        recover(recovery[k], cores)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(recover, recovery[k], threads) for k in light]):
            fut.result()
    recovery = None
    timings["recovery_s"] = time.perf_counter() - t3
    spill.cleanup()
    timings["memory_peak_gb"] = budget.peak / 2 ** 30
    # a pinned field passes force into its fixed node, which CalculiX would report there as reaction
    for k, (mine, earlier, nd) in enumerate(agree):
        if not earlier and len(lam):
            RF[nd] -= lam[k]                              # the support's force on the field: -alpha x its value
    for s, ms in transfer.items():                          # exact joints between parts, likewise
        for m, w in ms:
            if s in resid:
                RF[m] += w * resid[s]
    timings["total_s"] = time.perf_counter() - t0
    return U, RF, timings
