"""ftetwild: last-resort tet mesher on a tessellation of the part (milestone 4).

fTetWild is MPL-2.0 and runs as a separate executable (THIRD_PARTY.md). Its nodes lie on
the tessellation, not the exact CAD surface, so its MeshResult carries a looser surface
tolerance for face tagging. Not installed yet: reports itself unavailable.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from . import MeshRequest, MeshResult


class FTetWildBackend:
    name = "ftetwild"

    def __init__(self, exe: Optional[str]):
        self.exe = exe

    def available(self) -> bool:
        return bool(self.exe and Path(self.exe).is_file()) and False  # route not built yet

    def version(self) -> str:
        return "unavailable"

    def mesh(self, req: MeshRequest, on_line: Callable[[str], None] = lambda s: None) -> MeshResult:
        return MeshResult(False, self.name, error="ftetwild backend not built yet (milestone 4)")
