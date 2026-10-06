"""Stage caching (INVESTIGATION-001 Phase 2): a re-run redoes only what its changes require.

A stage that can be skipped has a key: a hash of the CONTENT of everything it reads (never file
times), of this package's source code and of every numeric default. A stage whose key matches a
record of an earlier successful run is skipped; the record names its output files with their
hashes, and a missing or edited output makes the record void. Any doubt means recompute: a wrong
cache hit is far worse than a slow run.

Records live in <study>/cache/<stage>.json. They hold what the stage told the job (RESULT and VERIFY
lines, non-blocking diagnostics) so a skipped stage reports the same as when it ran.
"""
from __future__ import annotations

import hashlib
import json
import time
from functools import lru_cache
from pathlib import Path

from . import defaults

FORMAT = "asmfea-stagecache-1"     # bump when a record's layout changes


def file_digest(path: Path) -> str:
    """SHA-1 of a file's content."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest(obj) -> str:
    """SHA-1 of a JSON-able object, key order ignored."""
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


@lru_cache(maxsize=1)
def code_fingerprint() -> str:
    """Hash of every source, catalogue and schema file of this package: any code change voids every
    stage record (the mesh cache has its own keys)."""
    root = Path(__file__).resolve().parent
    h = hashlib.sha1()
    for p in sorted(root.rglob("*")):
        if p.suffix in (".py", ".toml", ".json") and "__pycache__" not in p.parts:
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def defaults_digest() -> str:
    """Hash of every numeric default as it is NOW (a test or a study that changes one at run time
    must not reuse results made with the old value)."""
    return digest({k: v for k, v in vars(defaults).items() if k.isupper()})


def key(stage: str, inputs: dict) -> str:
    """A stage's key: its inputs, the code and the defaults."""
    return digest({"format": FORMAT, "stage": stage, "code": code_fingerprint(), "defaults": defaults_digest(),
                   "inputs": inputs})


class StageCache:
    def __init__(self, study: Path):
        self.study = study
        self.dir = study / "cache"

    def _path(self, stage: str) -> Path:
        return self.dir / f"{stage}.json"

    def get(self, stage: str, k: str) -> dict | None:
        """The record of an earlier run of this stage with key k whose outputs are all still as it left
        them; else None."""
        try:
            rec = json.loads(self._path(stage).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if rec.get("format") != FORMAT or rec.get("key") != k:
            return None
        for rel, d in rec.get("outputs", {}).items():
            p = self.study / rel
            if not p.is_file() or file_digest(p) != d:
                return None
        return rec

    def put(self, stage: str, k: str, outputs: list[Path], data: dict) -> None:
        """Record a successful run of this stage: its key, its outputs' hashes and what it reported."""
        self.dir.mkdir(exist_ok=True)
        rec = {"format": FORMAT, "key": k, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
               "outputs": {p.relative_to(self.study).as_posix(): file_digest(p) for p in outputs},
               "data": data}
        tmp = self._path(stage).with_suffix(".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        tmp.replace(self._path(stage))

    def drop(self, stage: str) -> None:
        self._path(stage).unlink(missing_ok=True)


class Recorder:
    """Wraps a job so a stage's RESULT and VERIFY lines and diagnostics are both passed on and kept,
    for replay when the stage is next skipped."""

    def __init__(self, js):
        self._js = js
        self.results: list[str] = []
        self.verifies: list[str] = []
        self.diags: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._js, name)

    def result(self, line: str):
        self.results.append(line)
        self._js.result(line)

    def verify(self, line: str):
        self.verifies.append(line)
        self._js.verify(line)

    def add_diags(self, diags):
        self.diags += [d.model_dump() for d in diags]
        self._js.add_diags(diags)

    def data(self) -> dict:
        return {"results": self.results, "verify": self.verifies, "diags": self.diags}


def replay(js, data: dict) -> None:
    """Report a skipped stage's lines and diagnostics again, as when it ran."""
    from .diagnostics import Diagnostic
    for line in data.get("results", []):
        js.result(line)
    for line in data.get("verify", []):
        js.verify(line)
    if data.get("diags"):
        js.add_diags([Diagnostic(**d) for d in data["diags"]])


def force_requested(js) -> bool:
    """A full run (job.json "force": true): every cache is ignored and refilled."""
    try:
        from .jobs import read_json
        return bool(read_json(js.path).get("force"))
    except (OSError, ValueError):
        return False


def note_reuse(js, stage: str, text: str) -> None:
    """What this stage reused: a RESULT line (the run log in the task pane, the MCP tool), job.json
    ("reuse") and, through the pipeline, results/reuse.json (for the report)."""
    js.result(f"{stage}: {text}")
    if hasattr(js, "reuse"):
        js.reuse(stage, text)
