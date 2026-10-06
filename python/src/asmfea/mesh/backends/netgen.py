"""netgen: second tet mesher in the fallback chain (milestone 4).

Netgen is LGPL-2.1 (THIRD_PARTY.md). It will run as a separate process. Until its
command-line route is built and checked, the backend reports itself unavailable and the
chain moves on.
"""
from __future__ import annotations

from typing import Callable, Optional

from . import MeshRequest, MeshResult


class NetgenBackend:
    name = "netgen"

    def __init__(self, exe: Optional[str]):
        self.exe = exe

    def available(self) -> bool:
        return False

    def version(self) -> str:
        return "unavailable"

    def mesh(self, req: MeshRequest, on_line: Callable[[str], None] = lambda s: None) -> MeshResult:
        return MeshResult(False, self.name, error="netgen backend not built yet (milestone 4)")
