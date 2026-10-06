"""Structured diagnostics: one shape for exceptions.json, job.json and results.json.

Every problem asmfea reports is a Diagnostic built from the catalogue
(catalogue.toml), so wording and advice live in one reviewable file.
"""
from __future__ import annotations

import tomllib
from functools import lru_cache
from importlib import resources
from typing import Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "blocking"]


class Where(BaseModel):
    parts: list[str] = []
    faces: list[str] = []
    connections: list[str] = []
    loads: list[str] = []
    fixtures: list[str] = []


class Diagnostic(BaseModel):
    id: str = ""                 # stable within a study: code + sorted where ids
    code: str
    severity: Severity
    stage: str
    what: str
    where: Where = Field(default_factory=Where)
    evidence: dict = {}
    likely_causes: list[str] = []
    actions: list[str] = []
    explain: str = ""            # plain-English paragraph (catalogue), for help windows
    raw: Optional[str] = None    # log excerpt or a pointer to one


@lru_cache(maxsize=1)
def catalogue() -> dict:
    text = resources.files("asmfea.diagnostics").joinpath("catalogue.toml").read_text(encoding="utf-8-sig")
    return tomllib.loads(text)


def make(code: str, *, where: Optional[dict] = None, evidence: Optional[dict] = None,
         raw: Optional[str] = None, **params) -> Diagnostic:
    """Build a diagnostic from its catalogue entry; `params` fill the `what` template."""
    entry = catalogue()[code]
    w = Where(**(where or {}))
    ids = sorted(w.parts + w.faces + w.connections + w.loads + w.fixtures)
    return Diagnostic(
        id=code + (":" + ",".join(ids) if ids else ""),
        code=code,
        severity=entry["severity"],
        stage=entry["stage"],
        what=entry["what"].format(**params),
        where=w,
        evidence=evidence or {},
        likely_causes=list(entry.get("likely_causes", [])),
        actions=list(entry.get("actions", [])),
        explain=entry.get("explain", ""),
        raw=raw,
    )


class Blocked(Exception):
    """Raised by a stage that cannot continue; carries the blocking diagnostics."""

    def __init__(self, diags: list[Diagnostic]):
        self.diags = diags
        super().__init__("; ".join(d.what for d in diags))


def raise_if_blocking(diags: list[Diagnostic]) -> None:
    blocking = [d for d in diags if d.severity == "blocking"]
    if blocking:
        raise Blocked(blocking)
