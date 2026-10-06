"""Standalone MCP server: python -m asmfea.mcp_server (stdio).

Calls the same registration function the SolidWorks MCP calls when it hosts asmfea, with
its own SolidWorks session (asmfea.sw.ComSession, which attaches to a running SolidWorks
and never launches it). Hosted inside the SolidWorks MCP, use only one of the two servers
so there is exactly one path to SolidWorks.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .mcp_tools import register

INSTRUCTIONS = """asmfea runs assembly FEA (CalculiX) on a study folder exported from SolidWorks.
Workflow: asmfea_status -> (asmfea_extract if SolidWorks is open) -> asmfea_run -> poll asmfea_job
until done/blocked/failed -> asmfea_summary -> asmfea_image -> asmfea_report.
Never choose a study folder yourself: ask the user. When a job is blocked, read its diagnostics
(what, where, likely causes, actions) and asmfea_exceptions; resolve connection questions with
asmfea_decide only after the user agrees, then run again from the resolve stage."""


def build() -> FastMCP:
    import os
    from .sw import ComSession
    mcp = FastMCP("asmfea_mcp", instructions=INSTRUCTIONS)
    # ASMFEA_NO_SOLIDWORKS=1 (tests, headless use): no path to SolidWorks at all.
    session = None if os.environ.get("ASMFEA_NO_SOLIDWORKS") else ComSession()
    register(mcp, "asmfea_", sw_session=session)
    return mcp


def main() -> None:
    build().run()


if __name__ == "__main__":
    main()
