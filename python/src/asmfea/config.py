"""User configuration: where the external binaries are, thread count.

Searched in order: $ASMFEA_CONFIG, ~/.asmfea.toml, %APPDATA%/asmfea/asmfea.toml, then asmfea.toml
in the install folder (Setup.exe writes it there: the bundled Python's parent folder).
Example:

    [solver]
    ccx = 'C:\\Tools\\PrePoMax v2.6.0\\Solver\\ccx_dynamic.exe'
    threads = 8

    [prepomax]
    exe = 'C:\\Tools\\PrePoMax v2.6.0\\PrePoMax.exe'

    [mesh]
    gmsh = 'C:\\Tools\\gmsh-4.15.2-Windows64\\gmsh.exe'
    netgen = 'C:\\Tools\\PrePoMax v2.6.0\\NetGen\\NetGenMesher.exe'
    ftetwild = ''
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import defaults


def config_paths() -> list[Path]:
    out = []
    if os.environ.get("ASMFEA_CONFIG"):
        out.append(Path(os.environ["ASMFEA_CONFIG"]))
    out.append(Path.home() / ".asmfea.toml")
    if os.environ.get("APPDATA"):
        out.append(Path(os.environ["APPDATA"]) / "asmfea" / "asmfea.toml")
    # an installed copy (Setup.exe): the bundled Python is <app>/python, its config <app>/asmfea.toml
    out.append(Path(sys.prefix).parent / "asmfea.toml")
    return out


@dataclass
class Config:
    ccx: Optional[str] = None
    threads: int = defaults.SOLVER_THREADS
    prepomax: Optional[str] = None
    gmsh: Optional[str] = None
    netgen: Optional[str] = None
    ftetwild: Optional[str] = None
    source: Optional[str] = None          # which file was read, for status()
    raw: dict = field(default_factory=dict)


def _merge(base: dict, over: dict) -> dict:
    """`over` wins key by key; an empty string in `over` does not hide a value in `base`."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v != "":
            out[k] = v
    return out


def load_config() -> Config:
    """Every config file found, combined: an earlier file in config_paths() wins key by key and a
    later one fills the gaps. So an installed copy's <app>/asmfea.toml (solver, mesher) and the
    user's own file (e.g. only [prepomax], written by set_prepomax) work together."""
    found = [p for p in config_paths() if p.is_file()]
    if not found:
        return Config()
    d: dict = {}
    for p in reversed(found):
        d = _merge(d, tomllib.loads(p.read_text(encoding="utf-8-sig")))  # tolerate a BOM (PowerShell writes one)
    s, pm, me = d.get("solver", {}), d.get("prepomax", {}), d.get("mesh", {})
    return Config(ccx=s.get("ccx") or None, threads=int(s.get("threads", defaults.SOLVER_THREADS)),
                  prepomax=pm.get("exe") or None, gmsh=me.get("gmsh") or None, netgen=me.get("netgen") or None,
                  ftetwild=me.get("ftetwild") or None, source=str(found[0]), raw=d)


def user_config_path() -> Path:
    """The user's own config file, where the task pane remembers settings (PrePoMax)."""
    if os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "asmfea" / "asmfea.toml"
    return Path.home() / ".asmfea.toml"


def set_prepomax(exe: str | Path) -> Path:
    """Remember where PrePoMax.exe is, in the user's config file. Only the [prepomax] exe line is
    written: the rest of the file (other settings, comments) is kept as it is."""
    import json
    import re
    exe = Path(exe)
    if not exe.is_file() or exe.suffix.lower() != ".exe":
        raise FileNotFoundError(f"not a program: {exe}")
    p = user_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8-sig") if p.exists() else ""
    line = f"exe = {json.dumps(str(exe))}"     # a TOML basic string: backslashes escaped
    sec = re.search(r"(?m)^\[prepomax\][ \t]*\r?\n", text)
    if sec:
        end = re.search(r"(?m)^\[", text[sec.end():])
        stop = sec.end() + end.start() if end else len(text)
        body = text[sec.end():stop]
        body = (re.sub(r"(?m)^exe[ \t]*=.*$", lambda _: line, body, count=1) if re.search(r"(?m)^exe[ \t]*=", body)
                else line + "\n" + body)
        text = text[:sec.end()] + body + text[stop:]
    else:
        text += ("" if not text or text.endswith("\n") else "\n") + ("\n" if text else "") + f"[prepomax]\n{line}\n"
    tomllib.loads(text)                         # never leave a file the pipeline cannot read
    p.write_text(text, encoding="utf-8")
    return p
