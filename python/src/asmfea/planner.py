"""The run planner (D60): from the study's "favour" choice (speed, balanced, accuracy), the computer's free memory and
cores, and the model, decide how to solve: the mesh level, the refined convergence pass, CalculiX or the hybrid
solver, the hybrid's exact-joint cap and memory budget, and the threads. The plan always leaves a reserve for
SolidWorks and other programs, and steps down rather than refusing to run: a solve that fails after a long wait is
the outcome the user most wants to avoid.

Two steps: ``plan_study`` before meshing (mesh level, convergence pass, threads, budget), and ``choose_solver``
once the deck's size is known. Advanced overrides in the study (mesh.fidelity, mesh.convergence_check,
mesh.solver) win over the planner and are reported as such."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

import psutil

from . import defaults


@dataclass
class Machine:
    ram_total_gb: float
    ram_available_gb: float
    cores: int                     # physical
    threads: int                   # logical

    def as_dict(self) -> dict:
        return asdict(self)


def machine() -> Machine:
    vm = psutil.virtual_memory()
    return Machine(round(vm.total / 2 ** 30, 1), round(vm.available / 2 ** 30, 1),
                   psutil.cpu_count(logical=False) or 1, psutil.cpu_count(logical=True) or 1)


LEGACY_FAVOUR = {"coarse": "speed", "standard": "balanced", "fine": "accuracy"}   # studies saved before D60


def reserve_gb(m: Machine) -> float:
    """Memory always left for everything else."""
    return max(defaults.PLAN_RESERVE_GB, defaults.PLAN_RESERVE_FRACTION * m.ram_total_gb)


@dataclass
class Plan:
    favour: str
    fidelity: str                  # mesh level
    convergence_check: bool        # refined pass
    solver_pref: str               # auto / calculix / hybrid (an override, or auto)
    exact_part_unknowns: int       # hybrid: exact-joint cap per part (0: smooth joints only)
    exact_total_unknowns: int      # hybrid: exact-joint cap in all (0: no cap)
    budget_gb: float               # memory the solve may use
    reserve_gb: float              # memory left for everything else
    threads: int
    machine: dict
    overrides: list = field(default_factory=list)   # settings forced by the study, not the planner
    notes: list = field(default_factory=list)       # plain-language lines for the plan display
    solver: Optional[str] = None   # chosen once the deck's size is known
    dof: Optional[int] = None
    measured: list = field(default_factory=list)    # [[DOF, peak GB]] of this run's CalculiX solves (D131)
    refine_unconverged: bool = False   # a third pass, finer again, on the parts that did not converge (D65)

    def as_dict(self) -> dict:
        return asdict(self)


def plan_study(study, m: Optional[Machine] = None, fast: bool = False) -> Plan:
    """The plan before meshing. ``study``: the model's Study (or None: defaults). ``fast``: a Fast run (the
    coarsest mesh, no refined pass, smooth joints), which the Fast run button asks for whatever the favour."""
    m = m or machine()
    mesh = study.mesh if study is not None else None
    favour = (mesh.favour if mesh is not None else "balanced") or "balanced"
    legacy = mesh is not None and "favour" not in mesh.model_fields_set and mesh.fidelity is not None
    if legacy:
        # a study saved before the planner always held fidelity and convergence_check: its favour follows from the
        # fidelity, and the old settings are overrides only where they differ from that favour's own
        favour = LEGACY_FAVOUR[mesh.fidelity]
    table = defaults.PLAN_FAVOUR[favour]
    res = reserve_gb(m)
    budget = max(m.ram_available_gb - res, 1.0)
    overrides, notes = [], []
    fidelity = "fast" if fast else table["fidelity"]
    if mesh is not None and mesh.fidelity and not fast and not legacy:
        fidelity = mesh.fidelity
        overrides.append(f"mesh level {mesh.fidelity}")
    convergence = False if fast else table["convergence"]
    given = mesh.convergence_check if mesh is not None else None
    if legacy and given is None:
        given = True                                   # before the planner, a missing convergence_check meant on
    if given is not None and not fast and not (legacy and bool(given) == table["convergence"]):
        convergence = bool(given)
        overrides.append("convergence check " + ("on" if convergence else "off"))
    # the local pass needs the convergence check's two solves to find the parts to refine
    refine = bool(table.get("refine")) and convergence and not fast
    given_r = mesh.refine_unconverged if mesh is not None else None
    if given_r is not None and not fast:
        refine = bool(given_r) and convergence
        overrides.append("refine where not converged " + ("on" if given_r else "off")
                         + ("" if convergence or not given_r else " (needs the convergence check)"))
    solver_pref = (mesh.solver if mesh is not None else "auto") or "auto"
    if solver_pref != "auto":
        overrides.append(f"solver {solver_pref}")
    cap = 0 if fast else table["exact_part_unknowns"]
    total = 0 if fast else table["exact_total_unknowns"]
    if favour == "accuracy" and budget < defaults.PLAN_ACCURACY_CAP_MIN_GB:
        cap = defaults.PLAN_FAVOUR["balanced"]["exact_part_unknowns"]
        total = defaults.PLAN_FAVOUR["balanced"]["exact_total_unknowns"]
        notes.append(f"Only {budget:.0f} GB to spare: joints are joined exactly as for Balanced.")
    notes.insert(0, f"optimised for {favour.capitalize()} on this computer: {m.cores} cores, {m.ram_available_gb:.0f} GB free of "
                    f"{m.ram_total_gb:.0f} GB; {res:.0f} GB kept for other programs, {budget:.0f} GB for the solve.")
    if m.ram_available_gb - res < defaults.PLAN_SHORT_GB:
        # little to spare now: the run still goes ahead (it is never refused for memory), but say so first
        notes.insert(1, f"Memory is short right now (only {m.ram_available_gb:.0f} GB free). The run will go ahead "
                        "but may be slow: close programs you do not need (browsers, other SolidWorks documents, "
                        "another analysis) before starting it.")
    if refine:
        notes.append("Parts whose results change by more than the limit between the two meshes are meshed finer "
                     "again and solved a third time.")
    return Plan(favour, fidelity, convergence, solver_pref, cap, total, round(budget, 1), round(res, 1), m.cores,
                m.as_dict(), overrides, notes, refine_unconverged=refine)


def ccx_estimate_gb(dof: int) -> float:
    return dof * defaults.BYTES_PER_DOF / 1e9                       # as solve.estimate_memory_gb


def ccx_need_gb(plan: Plan, dof: int) -> tuple[float, str]:
    """CalculiX's memory for a deck of ``dof`` unknowns, and where the figure comes from. From this run's own solves
    when it has any (D131): the peak of the largest, scaled by the DOF to a power fitted from the two largest
    (clamped), or CCX_MEMORY_EXPONENT from one. The generic BYTES_PER_DOF (solid tets) read 39 GB for a sheet
    assembly's 4.9 M DOF whose own first two solves (3.8 GB at 0.79 M, 9.7 GB at 1.9 M) scale to about 26 GB."""
    pts = sorted({int(d): float(g) for d, g in plan.measured if d and g and g > 0}.items())
    if not pts:
        return ccx_estimate_gb(dof), "estimate"
    d1, g1 = pts[-1]
    e = defaults.CCX_MEMORY_EXPONENT
    if len(pts) >= 2:
        d0, g0 = pts[-2]
        if d1 > d0 * 1.2 and g1 > g0:
            import math
            e = min(max(math.log(g1 / g0) / math.log(d1 / d0), 1.0), defaults.CCX_MEMORY_EXPONENT_MAX)
    return g1 * (dof / d1) ** e, f"scaled from this run's {d1:,}-unknown solve ({g1:.1f} GB)"


def choose_solver(plan: Plan, dof: int, kind: str = "linear", largest_part_dof: Optional[int] = None
                  ) -> tuple[str, list[str]]:
    """CalculiX when it fits the budget with a margin (it is exact and usually quicker), the hybrid solver
    otherwise (it needs about the largest part's memory). Only linear static decks can go to the hybrid. An
    override wins, except that a forced hybrid on a deck it cannot take falls back to CalculiX.
    largest_part_dof: when one part holds most of the model, the hybrid saves little memory and is slow (one solve
    per joint unknown of that part: 13 500 on a 3 M-DOF sheet part ran over 14 minutes, the user 2026-10-18), so CalculiX is
    kept while it fits the computer's RAM (MEMORY_WARN_FRACTION) even beyond the budget's margin (D131)."""
    need, source = ccx_need_gb(plan, dof)
    fits = need * defaults.PLAN_CCX_MARGIN <= plan.budget_gb
    ram = float((plan.machine or {}).get("ram_total_gb") or 0.0)
    dominant = (not fits and kind == "linear" and plan.solver_pref == "auto" and largest_part_dof is not None
                and dof > 0 and largest_part_dof / dof >= defaults.HYBRID_MIN_SPLIT
                and need <= min(plan.budget_gb, defaults.MEMORY_WARN_FRACTION * ram))   # free now, and the RAM
    notes = []
    if plan.solver_pref == "calculix":
        solver = "calculix"
        if not fits:
            notes.append(f"CalculiX was chosen in Advanced, but needs about {need:.0f} GB of the {plan.budget_gb:.0f} GB "
                         "to spare: the run may be very slow (paging) or fail.")
    elif plan.solver_pref == "hybrid" and kind == "linear":
        solver = "hybrid"
    elif dominant:
        solver = "calculix"
        notes.append(f"{dof:,} unknowns need about {need:.0f} GB in CalculiX ({source}); one part holds "
                     f"{largest_part_dof / dof:.0%} of them, so the hybrid solver would save little and take much "
                     "longer: CalculiX, within the computer's memory.")
    elif fits or kind != "linear":
        solver = "calculix"
        if not fits:
            notes.append(f"CalculiX needs about {need:.0f} GB, more than the {plan.budget_gb:.0f} GB to spare, and this "
                         "analysis (contact or buckling) has no alternative solver: expect it to be slow.")
    else:
        solver = "hybrid"
        notes.append(f"{dof:,} unknowns would need about {need:.0f} GB in CalculiX ({source}); the hybrid solver uses about the "
                     f"largest part's memory, within the {plan.budget_gb:.0f} GB budget.")
    plan.solver, plan.dof = solver, dof
    plan.notes += notes
    return solver, notes
