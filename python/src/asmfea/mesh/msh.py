"""Reader for Gmsh MSH 2.2 ASCII files (our own code; no Gmsh library involved).

Format (Gmsh reference manual, "MSH file format version 2"):
  $MeshFormat / 2.2 0 8 / $EndMeshFormat
  $Nodes / n / id x y z ... / $EndNodes
  $Elements / n / id type ntags tag... node... / $EndElements
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

# nodes per element for the gmsh element types we may meet
NODES_PER_TYPE = {1: 2, 2: 3, 3: 4, 4: 4, 5: 8, 6: 6, 7: 5, 8: 3, 9: 6, 10: 9, 11: 10,
                  12: 27, 13: 18, 14: 14, 15: 1, 16: 8, 17: 20, 18: 15, 19: 13}


def read_msh22(path: Path) -> tuple[np.ndarray, np.ndarray, dict[int, np.ndarray]]:
    """(node ids (n,), coords (n, 3), {gmsh element type: connectivity (m, k) of node ids})."""
    with open(path, "r") as f:
        lines = iter(f.read().splitlines())
    ids = coords = None
    elems: dict[int, list] = {}
    for line in lines:
        tag = line.strip()
        if tag == "$MeshFormat":
            ver = next(lines).split()
            if not ver[0].startswith("2") or ver[1] != "0":
                raise ValueError(f"{path}: expected MSH 2.x ASCII, got {' '.join(ver)}")
        elif tag == "$Nodes":
            n = int(next(lines))
            arr = np.loadtxt([next(lines) for _ in range(n)], ndmin=2)
            ids, coords = arr[:, 0].astype(np.int64), arr[:, 1:4]
        elif tag == "$Elements":
            n = int(next(lines))
            for _ in range(n):
                v = next(lines).split()
                ty, ntags = int(v[1]), int(v[2])
                elems.setdefault(ty, []).append([int(x) for x in v[3 + ntags:]])
    if ids is None:
        raise ValueError(f"{path}: no $Nodes section")
    return ids, coords, {t: np.asarray(c, dtype=np.int64) for t, c in elems.items()}
