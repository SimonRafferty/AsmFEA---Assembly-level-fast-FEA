"""Typed view of the neutral model (model.json + study.json).

`schema/model.schema.json` is the definition; these pydantic classes mirror it so the
pipeline gets attribute access and defaults. `load_model` validates against the JSON
Schema first, so the two can never silently disagree on what a file may contain.
"""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Literal, Optional

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

SCHEMA_MAJOR = 1

Vec3 = tuple[float, float, float]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(_M):
    assembly: str
    configuration: Optional[str] = None
    sw_version: Optional[str] = None
    addin_version: Optional[str] = None
    timestamp: Optional[str] = None
    view: Optional["View"] = None


class View(_M):
    """The user's view in SolidWorks at export: the report's main plot orientation."""
    toward_viewer: Vec3
    up: Vec3


class Material(_M):
    name: str
    E: float
    nu: float
    density: float
    yield_: float = Field(alias="yield")
    ultimate: Optional[float] = None


class Body(_M):
    volume: float
    centroid: Vec3
    bbox_min: Vec3
    bbox_max: Vec3


class CleanSpan(_M):
    s0: float
    s1: float


class MemberData(_M):
    profile: dict
    path: dict
    clean_spans: list[CleanSpan]


class SheetData(_M):
    thickness: float
    flat: bool


class FastenerData(_M):
    type: str
    d: float
    axis: Vec3


class Part(_M):
    id: str
    kind: Literal["member", "sheet", "solid", "fastener", "weld_bead"]
    material: str
    step: str
    body: Body
    geometry_hash: str
    transform: list[float]
    fixed: bool = False
    subassembly: Optional[str] = None      # top-level sub-assembly (rigid, like one multi-body part)
    member: Optional[MemberData] = None
    sheet: Optional[SheetData] = None
    fastener: Optional[FastenerData] = None


class Intermittent(_M):
    length: float = 0.0
    gap: float = 0.0
    pitch: float = 0.0
    staggered: bool = False


class Weld(_M):
    """A weld bead: its path (assembly-frame polylines, mm) and size (fillet leg, mm)."""
    id: str
    size: float
    side: Literal["selection", "both_sides", "all_around"] = "selection"
    parts: list[str]
    path: list[list[Vec3]]
    intermittent: Optional[Intermittent] = None
    partial: Optional[dict] = None


class Mate(_M):
    id: str
    type: str
    parts: list[str]
    entities: list[str] = []
    geometry: Optional[str] = None
    point: Optional[Vec3] = None
    direction: Optional[Vec3] = None
    radius: Optional[float] = None
    lock_rotation: bool = False
    profile: Optional[str] = None
    ratio: Optional[list[float]] = None
    distance_per_rev: Optional[float] = None
    reverse: bool = False


class Fingerprint(_M):
    surface: Literal["plane", "cylinder", "cone", "sphere", "torus", "bspline", "other"]
    area: float
    centroid: Vec3
    normal: Optional[Vec3] = None
    axis: Optional[Vec3] = None
    radius: Optional[float] = None


class FaceRef(_M):
    id: str
    part: str
    persistent_ref: Optional[str] = None
    feature: Optional[str] = None
    fingerprint: Fingerprint


class Load(_M):
    id: str
    load_case: str
    type: Literal["force", "pressure", "gravity", "remote_force"]
    faces: list[str] = []
    magnitude: float = 0.0
    direction: Optional[Vec3] = None
    point: Optional[Vec3] = None
    offset: Optional[Vec3] = None       # remote_force: from the area-weighted centroid of its faces, mm
    scale_in_buckling: Optional[bool] = None


class Fixture(_M):
    id: str
    type: Literal["fixed", "displacement", "pinned"]
    faces: list[str]
    dofs: list[int] = [1, 2, 3]
    hold_rotations: list[int] = []


class Override(_M):
    id: str
    parts: list[str]
    interface: Optional[str] = None
    connection: Literal["tie", "contact", "none", "revolute", "hinge", "slide", "sliding", "bolted", "screw"]
    friction: Optional[float] = None
    params: dict = {}
    note: Optional[str] = None


class Hinge(_M):
    """A hinge the user added: concentric cylindrical faces in two or more parts (D50)."""
    id: str
    faces: list[str]
    lock_rotation: bool = False
    note: Optional[str] = None


class MeshPrefs(_M):
    fidelity: Literal["coarse", "standard", "fine"] = "standard"
    size: Optional[float] = None
    convergence_check: bool = True


class Limits(_M):
    max_deflection: Optional[float] = None
    min_fos: Optional[float] = None
    min_buckling_factor: Optional[float] = None


class Study(_M):
    name: str
    analyses: list[Literal["static", "buckling"]] = ["static"]
    load_cases: list[str]
    buckling_cases: list[str] = []
    buckling_modes: int = 5
    bond_tolerance: Optional[float] = None   # mm, gap or overlap still bonded; None = defaults.GAP_TOL
    load_superposition: bool = False         # solve each load once, cases are sums (superpose.py, D39)
    mesh: MeshPrefs = MeshPrefs()
    limits: Limits = Limits()


class IssueWhere(_M):
    parts: list[str] = []
    faces: list[str] = []


class Issue(_M):
    """A problem the add-in found while extracting (reported as a diagnostic at load)."""
    code: str
    where: IssueWhere = IssueWhere()
    params: dict = {}


class Model(_M):
    schema_version: str
    source: Source
    materials: list[Material]
    parts: list[Part]
    mates: list[Mate] = []
    welds: list[Weld] = []
    face_refs: list[FaceRef] = []
    loads: list[Load] = []
    fixtures: list[Fixture] = []
    overrides: list[Override] = []
    hinges: list[Hinge] = []
    study: Optional[Study] = None
    issues: list[Issue] = []

    def material(self, name: str) -> Optional[Material]:
        return next((m for m in self.materials if m.name == name), None)

    def part(self, pid: str) -> Optional[Part]:
        return next((p for p in self.parts if p.id == pid), None)

    def face_ref(self, fid: str) -> Optional[FaceRef]:
        return next((f for f in self.face_refs if f.id == fid), None)


# --------------------------------------------------------------------------- #

def schema() -> dict:
    return json.loads(resources.files("asmfea.schema").joinpath("model.schema.json").read_text(encoding="utf-8"))


class SchemaError(ValueError):
    """The file does not satisfy the schema; message names the offending path."""


def check_version(doc: dict, what: str) -> None:
    v = str(doc.get("schema_version", ""))
    try:
        major = int(v.split(".")[0])
    except ValueError:
        raise SchemaError(f"{what}: missing or malformed schema_version {v!r}")
    if major != SCHEMA_MAJOR:
        raise SchemaError(f"{what}: schema_version {v} is not supported (this asmfea reads {SCHEMA_MAJOR}.x)")


# Keys a study.json may carry; list-valued ones are appended, `study` replaces.
_MERGE_LISTS = ("face_refs", "loads", "fixtures", "overrides", "hinges", "materials", "mates")


def merge_study(model: dict, study: dict) -> dict:
    out = dict(model)
    for k, v in study.items():
        if k == "schema_version":
            continue
        if k in _MERGE_LISTS:
            seen = {item.get("id", item.get("name")) for item in v}
            out[k] = [i for i in out.get(k, []) if i.get("id", i.get("name")) not in seen] + list(v)
        elif k == "study":
            out[k] = v
        else:
            raise SchemaError(f"study.json: key {k!r} cannot be supplied by study.json")
    return out


def load_model(folder: str | Path) -> Model:
    """Read model.json, merge study.json if present, validate, return the typed model."""
    folder = Path(folder)
    doc = json.loads((folder / "model.json").read_text(encoding="utf-8-sig"))
    check_version(doc, "model.json")
    sj = folder / "study.json"
    if sj.exists():
        sdoc = json.loads(sj.read_text(encoding="utf-8-sig"))
        check_version(sdoc, "study.json")
        doc = merge_study(doc, sdoc)
    # Decisions made while SolidWorks was unreachable (api.decide); the add-in folds
    # them into the assembly at the next extract, the pipeline honours them meanwhile.
    po = folder / "pending_overrides.json"
    if po.exists():
        pdoc = json.loads(po.read_text(encoding="utf-8-sig"))
        doc = merge_study(doc, {"overrides": pdoc.get("overrides", [])})
    try:
        jsonschema.validate(doc, schema())
    except jsonschema.ValidationError as e:
        path = "/".join(str(p) for p in e.absolute_path) or "(root)"
        raise SchemaError(f"model: {path}: {e.message}") from None
    return Model.model_validate(doc)
