"""SwSession for the standalone server: the single path to SolidWorks.

Attaches to the running SolidWorks (never launches it), fetches the asmfea add-in's
automation object, and calls one method on it. To keep exactly one client driving
SolidWorks, every call holds the same cross-process lock file the SolidWorks MCP
(layoutcad) uses: %TEMP%\\layoutcad_solidworks.lock.

Needs pywin32 (PSF-2.0, permissive; optional extra asmfea[sw]). Imported lazily, so the
core and the MCP layer load without it.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

ADDIN_PROGID = "AsmFea.AddIn"                       # the add-in's COM ProgID (addin/AsmFea.AddIn/AddIn.cs)
ADDIN_CLSID = "{77cf8ee6-9213-44db-8379-945fd7578fc2}"
LOCK = Path(tempfile.gettempdir()) / "layoutcad_solidworks.lock"
LOCK_WAIT_S = 30.0


class SwUnavailable(RuntimeError):
    pass


def running_solidworks():
    """The running SolidWorks, whatever its version. "SldWorks.Application" names the newest install
    only; an older SolidWorks registers as SldWorks.Application.<major> (33 = 2025, 26 = 2018), so those
    are tried next (D26). None when no SolidWorks is running."""
    import win32com.client
    for progid in ["SldWorks.Application"] + [f"SldWorks.Application.{n}" for n in range(40, 25, -1)]:
        try:
            return win32com.client.GetActiveObject(progid)
        except Exception:
            continue
    return None


class ComSession:
    def __init__(self, progid: str = ADDIN_PROGID):
        self.progid = progid

    def _addin(self):
        try:
            import pythoncom
            import win32com.client
        except ImportError as e:
            raise SwUnavailable("pywin32 is not installed (pip install asmfea[sw])") from e
        pythoncom.CoInitialize()
        sw = running_solidworks()
        if sw is None:
            raise SwUnavailable("SolidWorks is not running: start it with the asmfea add-in loaded")
        # GetAddInObject is documented with a CLSID; the ProgID is tried first (LIVE-T5)
        obj = sw.GetAddInObject(self.progid)
        if obj is None and self.progid == ADDIN_PROGID:
            obj = sw.GetAddInObject(ADDIN_CLSID)
        if obj is None:
            raise SwUnavailable(f"the asmfea add-in ({self.progid}) is not loaded in SolidWorks")
        return obj

    def call(self, method: str, *args: Any) -> Any:
        _acquire()
        try:
            member = getattr(self._addin(), method)
            # late-bound COM: a property (Version) arrives as its value, a method as a callable
            return member(*args) if callable(member) else member
        finally:
            _release()


def _acquire() -> None:
    deadline = time.time() + LOCK_WAIT_S
    while True:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                json.dump({"pid": os.getpid(), "job": None, "action": "asmfea", "program": "asmfea",
                           "started": time.time()}, f)
            return
        except FileExistsError:
            if _stale():
                LOCK.unlink(missing_ok=True)
                continue
            if time.time() > deadline:
                raise SwUnavailable("SolidWorks is busy with another client (lock file held); try again later")
            time.sleep(0.5)


def _stale() -> bool:
    try:
        pid = json.loads(LOCK.read_text()).get("pid")
        import psutil
        return not psutil.pid_exists(int(pid))
    except Exception:
        return False


def _release() -> None:
    try:
        if json.loads(LOCK.read_text()).get("pid") == os.getpid():
            LOCK.unlink(missing_ok=True)
    except Exception:
        pass
