"""Mesher backends (Change 001). Every mesher implements the same small interface, and no
code outside this package knows which mesher produced a mesh.

A backend turns a MeshRequest into a neutral mesh file (`.npz`, see `save_neutral`):
nodes in the part frame and element blocks in CalculiX node order. Face tagging,
caching and the fallback chain live in asmfea.mesh and work the same for every backend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

import numpy as np

from ... import defaults
from ..elements import FLIP, corner_volume_sign, inverted_count


@dataclass
class MeshRequest:
    part: str                      # part id (for logs only)
    geometry_file: Path            # single-body STEP in the part frame
    body_selector: int = 0         # index of the solid in geometry_file
    element: str = "tet"           # "tet" | "swept" (native_sweep)
    order: int = 2
    size_max: float = 5.0
    size_min: Optional[float] = None
    out_dir: Path = Path(".")
    timeout_s: float = 600.0
    threads: int = 1
    extra: dict = field(default_factory=dict)  # backend-specific (e.g. sweep data)


@dataclass
class MeshResult:
    ok: bool
    backend: str
    mesh_file: Optional[Path] = None   # neutral .npz
    nodes: int = 0
    elements: int = 0
    quality: dict = field(default_factory=dict)
    log_path: Optional[Path] = None
    error: Optional[str] = None        # first line of the failure, for the diagnostic
    timed_out: bool = False
    # How closely boundary nodes follow the CAD surfaces; face tagging uses it as
    # its distance tolerance (mm).
    surface_tolerance: float = 1e-3


class Backend(Protocol):
    name: str

    def version(self) -> str: ...

    def available(self) -> bool: ...

    def mesh(self, req: MeshRequest, on_line: Callable[[str], None] = lambda s: None) -> MeshResult: ...


def save_neutral(path: Path, nodes: np.ndarray, blocks: list[tuple[str, np.ndarray]]) -> None:
    """Neutral mesh: nodes (N, 3) with local ids 1..N, blocks of (CalculiX type, conn)."""
    arrays = {f"conn_{i}": c for i, (_, c) in enumerate(blocks)}
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, nodes=nodes, etypes=np.array([t for t, _ in blocks]), **arrays)
    tmp.replace(path)


def load_neutral(path: Path) -> tuple[np.ndarray, list[tuple[str, np.ndarray]]]:
    z = np.load(path, allow_pickle=False)
    etypes = [str(s) for s in z["etypes"]]
    return z["nodes"], [(t, z[f"conn_{i}"]) for i, t in enumerate(etypes)]


def finish_blocks(X: np.ndarray, blocks: list[tuple[str, np.ndarray]]) -> tuple[list, dict]:
    """Fix element orientation, refuse inverted elements, return quality statistics. A flat tet (quality below
    TET_FLAT_QUALITY) counts as inverted: its Jacobian is only round-off from zero, so the sign test passes it, but
    its stiffness is 1e13 times its neighbours' and swamps the part's precision (D86)."""
    out, inverted, flat, worst = [], 0, 0, 1.0
    for etype, conn in blocks:
        neg = corner_volume_sign(etype, X[conn]) < 0
        if neg.any():
            conn = conn.copy()
            conn[neg] = conn[neg][:, FLIP[etype]]
        inverted += inverted_count(etype, X[conn])
        if etype == "C3D10":
            qual = tet_quality(X[conn[:, :4]])
            worst = min(worst, float(qual.min()) if len(qual) else 1.0)
            flat += int(np.sum(qual < defaults.TET_FLAT_QUALITY))
        out.append((etype, conn))
    return out, {"inverted": inverted + flat, "flat": flat, "min_tet_quality": worst}


def tet_quality(P: np.ndarray) -> np.ndarray:
    """Normalised volume-to-edge measure: 1 for a regular tet, 0 for a flat one."""
    e = [P[:, j] - P[:, i] for i, j in ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))]
    lrms = np.sqrt(sum((v ** 2).sum(axis=1) for v in e) / 6.0)
    vol = np.abs(np.einsum("ij,ij->i", e[0], np.cross(e[1], e[2]))) / 6.0
    return 6 * np.sqrt(2) * vol / lrms ** 3


def registry(cfg) -> dict[str, "Backend"]:
    from .ftetwild import FTetWildBackend
    from .gmsh_cli import GmshCliBackend
    from .native_sweep import NativeSweepBackend
    from .netgen import NetgenBackend
    return {b.name: b for b in (GmshCliBackend(cfg.gmsh), NetgenBackend(cfg.netgen),
                                FTetWildBackend(cfg.ftetwild), NativeSweepBackend(cfg.gmsh))}


# Fallback chains per region kind (PLAN.md Meshing: Gmsh, then Netgen, then fTetWild). A swept
# region falls back to tets when the sweep refuses it.
CHAINS = {"tet": ["gmsh_cli", "netgen", "ftetwild"],
          "swept": ["native_sweep", "gmsh_cli", "netgen", "ftetwild"]}
