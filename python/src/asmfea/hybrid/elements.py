"""Element routines for the hybrid solver: stiffness, consistent body-force loads and nodal stresses of the
three element types AsmFEA meshes with, in CalculiX's node order and with CalculiX's integration rules
(C3D10: 4 points; C3D15: 9 points; C3D20R: 2 x 2 x 2), so a part assembled here has the same stiffness as
CalculiX gives it.

Everything is batched over elements of one type: X (n_el, n, 3) node coordinates, U (n_el, n, 3)
displacements. Strains and stresses are in CalculiX's order: xx, yy, zz, xy, yz, zx (engineering shears in
the strains)."""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from ..mesh.elements import MIDSIDE, _wedge15_N


# ---------------------------------------------------------------- shape functions (natural coordinates)

def _tet10_N(r: float, s: float, t: float) -> np.ndarray:
    L = (1 - r - s - t, r, s, t)
    N = [L[i] * (2 * L[i] - 1) for i in range(4)]
    N += [4 * L[i] * L[j] for i, j in MIDSIDE["C3D10"]]
    return np.array(N)


_HEX_CORNERS = np.array([[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
                         [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], float)
_HEX_POINTS = np.vstack([_HEX_CORNERS, [(_HEX_CORNERS[a] + _HEX_CORNERS[b]) / 2 for a, b in MIDSIDE["C3D20R"]]])


def _hex20_N(r: float, s: float, t: float) -> np.ndarray:
    x = np.array([r, s, t])
    out = np.zeros(20, dtype=x.dtype)                 # complex for the complex-step derivatives
    for i, p in enumerate(_HEX_POINTS):
        if i < 8:
            out[i] = 0.125 * np.prod(1 + x * p) * (x @ p - 2)
        else:
            k = int(np.nonzero(p == 0)[0][0])
            f = 0.25 * (1 - x[k] ** 2)
            for j in range(3):
                if j != k:
                    f *= 1 + x[j] * p[j]
            out[i] = f
    return out


SHAPE = {"C3D10": _tet10_N, "C3D15": lambda r, s, t: _wedge15_N(r, s, t), "C3D20R": _hex20_N}
N_NODES = {"C3D10": 10, "C3D15": 15, "C3D20R": 20}

# natural coordinates of each element's nodes (for stresses evaluated at the nodes)
_NODE_NAT = {
    "C3D10": np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1],
                       [.5, 0, 0], [.5, .5, 0], [0, .5, 0], [0, 0, .5], [.5, 0, .5], [0, .5, .5]], float),
    "C3D15": np.array([[0, 0, -1], [1, 0, -1], [0, 1, -1], [0, 0, 1], [1, 0, 1], [0, 1, 1],
                       [.5, 0, -1], [.5, .5, -1], [0, .5, -1], [.5, 0, 1], [.5, .5, 1], [0, .5, 1],
                       [0, 0, 0], [1, 0, 0], [0, 1, 0]], float),
    "C3D20R": _HEX_POINTS,
}


def _gauss_points(etype: str) -> list[tuple[np.ndarray, float]]:
    if etype == "C3D10":
        a, b = 0.5854101966249685, 0.1381966011250105
        return [(np.array(p), 1 / 24) for p in ([b, b, b], [a, b, b], [b, a, b], [b, b, a])]
    if etype == "C3D15":
        tri = [((1 / 6, 1 / 6), 1 / 6), ((2 / 3, 1 / 6), 1 / 6), ((1 / 6, 2 / 3), 1 / 6)]
        g = np.sqrt(0.6)
        line = [(-g, 5 / 9), (0.0, 8 / 9), (g, 5 / 9)]
        return [(np.array([r, s, t]), wt * wl) for (r, s), wt in tri for t, wl in line]
    g = 1 / np.sqrt(3)
    return [(np.array([a, b, c]), 1.0) for a in (-g, g) for b in (-g, g) for c in (-g, g)]


def _dN(etype: str, p: np.ndarray, h: float = 1e-30) -> np.ndarray:
    """(n, 3) derivatives at natural point p by complex steps: exact to machine precision for these polynomial
    shape functions (central differences lost about 10 digits to round-off)."""
    f = SHAPE[etype]
    cols = []
    for k in range(3):
        z = np.array(p, dtype=complex)
        z[k] += 1j * h
        cols.append(np.imag(f(*z)) / h)
    return np.stack(cols, axis=1)


@lru_cache(maxsize=None)
def rule(etype: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(weights (g,), N (g, n), dN (g, n, 3)) of the element's integration rule."""
    pts = _gauss_points(etype)
    w = np.array([wt for _, wt in pts])
    N = np.array([SHAPE[etype](*p) for p, _ in pts])
    dN = np.array([_dN(etype, p) for p, _ in pts])
    return w, N, dN


# ---------------------------------------------------------------- material and kinematics

def elasticity(E: float, nu: float) -> np.ndarray:
    lam, mu = E * nu / ((1 + nu) * (1 - 2 * nu)), E / (2 * (1 + nu))
    D = np.zeros((6, 6))
    D[:3, :3] = lam
    D[np.arange(3), np.arange(3)] += 2 * mu
    D[3:, 3:] = np.eye(3) * mu
    return D


def _B(dNdx: np.ndarray) -> np.ndarray:
    """(n_el, 6, 3n) strain-displacement matrices from (n_el, n, 3) global gradients; dofs ordered node by node."""
    ne, n, _ = dNdx.shape
    B = np.zeros((ne, 6, 3 * n))
    x, y, z = dNdx[:, :, 0], dNdx[:, :, 1], dNdx[:, :, 2]
    B[:, 0, 0::3] = x
    B[:, 1, 1::3] = y
    B[:, 2, 2::3] = z
    B[:, 3, 0::3] = y
    B[:, 3, 1::3] = x
    B[:, 4, 1::3] = z
    B[:, 4, 2::3] = y
    B[:, 5, 0::3] = z
    B[:, 5, 2::3] = x
    return B


def _grad(dN: np.ndarray, X: np.ndarray):
    """(dN/dx (n_el, n, 3), det J (n_el,)) at one natural point; dN (n, 3) natural derivatives."""
    J = np.einsum("ai,eaj->eij", dN, X)              # J[e, i, j] = d x_j / d xi_i
    det = np.linalg.det(J)
    inv = np.linalg.inv(J)
    return np.einsum("ai,eji->eaj", dN, inv), det


def _make_kernel():
    """The element stiffness loop compiled by numba, or None without it. For an isotropic material B'DB is, per
    pair of nodes a, b and directions i, j:  lam dNa_i dNb_j + mu dNa_j dNb_i + mu (i == j) dNa . dNb,  which
    this computes straight from the shape-function gradients, element by element across the cores. The NumPy
    version below (batched B'DB) was a quarter of the hybrid solver's busy time on a 4.5 M-DOF frame."""
    try:
        import numba
    except ImportError:
        return None

    # one thread per call, the GIL released: the hybrid solver calls this from several part threads at once, and
    # numba's own parallel loop is not safe to start from several threads (its default threading layer)
    @numba.njit(nogil=True, cache=True)
    def kernel(X, w, dN, lam, mu):
        ne, n, _ = X.shape
        ng = w.shape[0]
        K = np.zeros((ne, 3 * n, 3 * n))
        for e in range(ne):
            G = np.empty((n, 3))
            for g in range(ng):
                J = np.zeros((3, 3))                       # J[i, j] = sum_a dN[a, i] X[a, j]
                for a in range(n):
                    for i in range(3):
                        for j in range(3):
                            J[i, j] += dN[g, a, i] * X[e, a, j]
                det = (J[0, 0] * (J[1, 1] * J[2, 2] - J[1, 2] * J[2, 1])
                       - J[0, 1] * (J[1, 0] * J[2, 2] - J[1, 2] * J[2, 0])
                       + J[0, 2] * (J[1, 0] * J[2, 1] - J[1, 1] * J[2, 0]))
                inv = np.empty((3, 3))
                inv[0, 0] = (J[1, 1] * J[2, 2] - J[1, 2] * J[2, 1]) / det
                inv[0, 1] = (J[0, 2] * J[2, 1] - J[0, 1] * J[2, 2]) / det
                inv[0, 2] = (J[0, 1] * J[1, 2] - J[0, 2] * J[1, 1]) / det
                inv[1, 0] = (J[1, 2] * J[2, 0] - J[1, 0] * J[2, 2]) / det
                inv[1, 1] = (J[0, 0] * J[2, 2] - J[0, 2] * J[2, 0]) / det
                inv[1, 2] = (J[0, 2] * J[1, 0] - J[0, 0] * J[1, 2]) / det
                inv[2, 0] = (J[1, 0] * J[2, 1] - J[1, 1] * J[2, 0]) / det
                inv[2, 1] = (J[0, 1] * J[2, 0] - J[0, 0] * J[2, 1]) / det
                inv[2, 2] = (J[0, 0] * J[1, 1] - J[0, 1] * J[1, 0]) / det
                for a in range(n):                         # global gradients: G[a, j] = sum_i dN[a, i] inv[j, i]
                    for j in range(3):
                        s = 0.0
                        for i in range(3):
                            s += dN[g, a, i] * inv[j, i]
                        G[a, j] = s
                f = w[g] * abs(det)
                for a in range(n):
                    for b in range(n):
                        dot = G[a, 0] * G[b, 0] + G[a, 1] * G[b, 1] + G[a, 2] * G[b, 2]
                        for i in range(3):
                            for j in range(3):
                                v = lam * G[a, i] * G[b, j] + mu * G[a, j] * G[b, i]
                                if i == j:
                                    v += mu * dot
                                K[e, 3 * a + i, 3 * b + j] += f * v
        return K

    return kernel


_KERNEL = "unset"


def stiffness(etype: str, X: np.ndarray, E: float, nu: float) -> np.ndarray:
    """Element stiffness matrices (n_el, 3n, 3n): compiled (numba) when available, else NumPy."""
    global _KERNEL
    if _KERNEL == "unset":
        _KERNEL = _make_kernel()
    if _KERNEL is not None and len(X):
        w, _, dN = rule(etype)
        lam, mu = E * nu / ((1 + nu) * (1 - 2 * nu)), E / (2 * (1 + nu))
        return _KERNEL(np.ascontiguousarray(X, dtype=np.float64), np.ascontiguousarray(w, dtype=np.float64),
                       np.ascontiguousarray(dN, dtype=np.float64), float(lam), float(mu))
    return stiffness_numpy(etype, X, E, nu)


def stiffness_numpy(etype: str, X: np.ndarray, E: float, nu: float) -> np.ndarray:
    """Element stiffness matrices (n_el, 3n, 3n), batched B'DB in NumPy (the reference the kernel is tested
    against, and the fallback without numba)."""
    w, _, dN = rule(etype)
    D = elasticity(E, nu)
    n = X.shape[1]
    K = np.zeros((len(X), 3 * n, 3 * n))
    for g in range(len(w)):
        dNdx, det = _grad(dN[g], X)
        B = _B(dNdx)
        K += (w[g] * np.abs(det))[:, None, None] * (B.transpose(0, 2, 1) @ (D @ B))   # batched BLAS
    return K


def body_force(etype: str, X: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Consistent nodal loads (n_el, n, 3) of a uniform body force b (force per volume, (3,) or (n_el, 3))."""
    w, N, dN = rule(etype)
    b = np.broadcast_to(np.asarray(b, float), (len(X), 3))
    F = np.zeros(X.shape)
    for g in range(len(w)):
        _, det = _grad(dN[g], X)
        F += (w[g] * np.abs(det))[:, None, None] * N[g][None, :, None] * b[:, None, :]
    return F


def volumes(etype: str, X: np.ndarray) -> np.ndarray:
    w, _, dN = rule(etype)
    return sum(w[g] * np.abs(_grad(dN[g], X)[1]) for g in range(len(w)))


_CORNERS = {"C3D10": 4, "C3D15": 6, "C3D20R": 8}


def _extrap_basis(etype: str, P: np.ndarray) -> np.ndarray:
    r, s, t = P[:, 0], P[:, 1], P[:, 2]
    o = np.ones_like(r)
    if etype == "C3D10":
        return np.stack([o, r, s, t], 1)
    if etype == "C3D20R":
        return np.stack([o, r, s, t, r * s, s * t, t * r, r * s * t], 1)
    return np.stack([a * b for a in (o, r, s) for b in (o, t)], 1)          # C3D15: linear in-plane x linear


@lru_cache(maxsize=None)
def extrapolation(etype: str) -> np.ndarray:
    """(n_nodes, n_points): integration-point values to nodal values as CalculiX does it. Corner values come
    from a (least-squares) linear, trilinear or linear-by-linear fit through the integration points; a
    midside node takes the mean of its edge's corners. Checked on a 24-part welded chassis against CalculiX's
    own nodal stresses from the same displacements: within 0.8 % of the peak at every node, where stresses
    evaluated directly at the nodes were up to 91 % off at distorted wedges."""
    pts = np.array([p for p, _ in _gauss_points(etype)])
    nat = _NODE_NAT[etype]
    nc = _CORNERS[etype]
    E = np.zeros((len(nat), len(pts)))
    E[:nc] = _extrap_basis(etype, nat[:nc]) @ np.linalg.pinv(_extrap_basis(etype, pts))
    for k in range(nc, len(nat)):
        a, b = next((a, b) for a in range(nc) for b in range(a + 1, nc)
                    if np.allclose((nat[a] + nat[b]) / 2, nat[k]))
        E[k] = (E[a] + E[b]) / 2
    return E


def _make_stress_kernel():
    """Stress at the integration points (n_el, n_points, 6), compiled by numba (None without it): the displacement
    gradient H[i, j] = sum_a u[a, i] dNa_j gives strain and, isotropic, stress. The NumPy version below (batched
    B matrices) took 10 s of a 1.5 M-node frame's output. Single-threaded, the GIL released (see _make_kernel)."""
    try:
        import numba
    except ImportError:
        return None

    @numba.njit(nogil=True, cache=True)
    def kernel(X, U, dN, lam, mu):
        ne, n, _ = X.shape
        ng = dN.shape[0]
        Sg = np.zeros((ne, ng, 6))
        for e in range(ne):
            for g in range(ng):
                J = np.zeros((3, 3))
                for a in range(n):
                    for i in range(3):
                        for j in range(3):
                            J[i, j] += dN[g, a, i] * X[e, a, j]
                det = (J[0, 0] * (J[1, 1] * J[2, 2] - J[1, 2] * J[2, 1])
                       - J[0, 1] * (J[1, 0] * J[2, 2] - J[1, 2] * J[2, 0])
                       + J[0, 2] * (J[1, 0] * J[2, 1] - J[1, 1] * J[2, 0]))
                inv = np.empty((3, 3))
                inv[0, 0] = (J[1, 1] * J[2, 2] - J[1, 2] * J[2, 1]) / det
                inv[0, 1] = (J[0, 2] * J[2, 1] - J[0, 1] * J[2, 2]) / det
                inv[0, 2] = (J[0, 1] * J[1, 2] - J[0, 2] * J[1, 1]) / det
                inv[1, 0] = (J[1, 2] * J[2, 0] - J[1, 0] * J[2, 2]) / det
                inv[1, 1] = (J[0, 0] * J[2, 2] - J[0, 2] * J[2, 0]) / det
                inv[1, 2] = (J[0, 2] * J[1, 0] - J[0, 0] * J[1, 2]) / det
                inv[2, 0] = (J[1, 0] * J[2, 1] - J[1, 1] * J[2, 0]) / det
                inv[2, 1] = (J[0, 1] * J[2, 0] - J[0, 0] * J[2, 1]) / det
                inv[2, 2] = (J[0, 0] * J[1, 1] - J[0, 1] * J[1, 0]) / det
                H = np.zeros((3, 3))                           # H[i, j] = d u_i / d x_j
                for a in range(n):
                    for j in range(3):
                        gj = dN[g, a, 0] * inv[j, 0] + dN[g, a, 1] * inv[j, 1] + dN[g, a, 2] * inv[j, 2]
                        for i in range(3):
                            H[i, j] += U[e, a, i] * gj
                tr = H[0, 0] + H[1, 1] + H[2, 2]
                Sg[e, g, 0] = lam * tr + 2 * mu * H[0, 0]
                Sg[e, g, 1] = lam * tr + 2 * mu * H[1, 1]
                Sg[e, g, 2] = lam * tr + 2 * mu * H[2, 2]
                Sg[e, g, 3] = mu * (H[0, 1] + H[1, 0])
                Sg[e, g, 4] = mu * (H[1, 2] + H[2, 1])
                Sg[e, g, 5] = mu * (H[0, 2] + H[2, 0])
        return Sg

    return kernel


_STRESS_KERNEL = "unset"


def nodal_stress(etype: str, X: np.ndarray, U: np.ndarray, E: float, nu: float) -> np.ndarray:
    """Stress (n_el, n, 6) at each element's nodes, extrapolated from its integration points (to be averaged
    over the elements sharing a node, as CalculiX does): compiled (numba) when available, else NumPy."""
    global _STRESS_KERNEL
    if _STRESS_KERNEL == "unset":
        _STRESS_KERNEL = _make_stress_kernel()
    if _STRESS_KERNEL is None or not len(X):
        return nodal_stress_numpy(etype, X, U, E, nu)
    _, _, dN = rule(etype)
    lam, mu = E * nu / ((1 + nu) * (1 - 2 * nu)), E / (2 * (1 + nu))
    Sg = _STRESS_KERNEL(np.ascontiguousarray(X, dtype=np.float64), np.ascontiguousarray(U, dtype=np.float64),
                        np.ascontiguousarray(dN, dtype=np.float64), float(lam), float(mu))
    return np.einsum("ng,egc->enc", extrapolation(etype), Sg)


def nodal_stress_numpy(etype: str, X: np.ndarray, U: np.ndarray, E: float, nu: float) -> np.ndarray:
    """nodal_stress in NumPy (batched B matrices): the reference the kernel is tested against, and the fallback."""
    D = elasticity(E, nu)
    _, _, dN = rule(etype)
    u = U.reshape(len(U), -1)
    Sg = np.zeros((len(X), len(dN), 6))
    for g in range(len(dN)):
        dNdx, _ = _grad(dN[g], X)
        Sg[:, g] = np.einsum("eij,ej->ei", _B(dNdx), u) @ D.T
    return np.einsum("ng,egc->enc", extrapolation(etype), Sg)
