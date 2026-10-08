"""Every sentence of the report that states an assumption or a limitation lives here,
so it can be reviewed in one place (PLAN.md, Results, checks and report).

Each function takes the model facts it depends on and returns plain sentences; the
report only lists what applies to this study.
"""
from __future__ import annotations

ALWAYS = [
    "Material behaviour is linear elastic. Stresses above yield are reported but not redistributed.",
    "Displacements are assumed small (linear geometry). A displacement warning in the check table means this "
    "assumption may not hold.",
    "Parts are meshed one by one with quadratic solid elements: thin walls and member spans with wedges swept "
    "through the thickness or along the member (one element through a wall), other regions with tetrahedra. "
    "Regions and parts are joined by tied constraints; no conformal mesh is made across them.",

    "Peak stresses at sharp re-entrant corners, at fixture edges and at the edges of tied interfaces are "
    "singular and do not converge with mesh refinement. Each part therefore reports two peaks: the raw peak, and "
    "the peak excluding nodes within one element of a tie boundary. The factor of safety uses the second. "
    "Neither figure excludes singular points at fixture edges or sharp corners.",
    "Fixtures are rigid: a fixed face cannot move at all, which stiffens the structure next to it compared with "
    "a real support.",
]


def mesh_levels(two: bool) -> list[str]:
    if two:
        return ["The reported results come from the refined mesh. The base mesh is solved as well, and the change "
                "between the two is the convergence figure."]
    return ["Only one mesh was solved (convergence check switched off), so the mesh sensitivity of the results is "
            "not measured."]


def ties(n_ties: int) -> list[str]:
    if not n_ties:
        return []
    return [f"{n_ties} interface(s) are bonded (tied). A tie bonds the full touching area, not a weld throat or a "
            "bolt pattern, and transfers tension as well as compression. Weld sizing remains a hand calculation."]


def removed(removed_parts: dict[str, str]) -> list[str]:
    out = []
    beads = [p for p, why in removed_parts.items() if "weld bead" in why]
    if beads:
        out.append(f"Weld bead bodies ({', '.join(beads)}) are removed and the parts they join are tied directly.")
    loose = [p for p, why in removed_parts.items() if why == "disconnected"]
    if loose:
        out.append(f"Parts with no connection to a fixture and no load ({', '.join(loose)}) are left out of the "
                   "analysis; their weight is not included.")
    return out


def bolted(joints: list[dict]) -> list[str]:
    out = []
    rigid = [j for j in joints if "rigid" in j.get("mode", "")]
    if rigid:
        names = ", ".join(f"{j['fastener']} ({' / '.join(j['parts'])})" for j in rigid)
        out.append(f"Bolted joints in the default mode: {names}. The fastener solids are removed and the clamped "
                   "faces are tied, so the joints are assumed not to slip or separate, and bolt forces are not "
                   "reported. Use contact with preload where bolt forces matter.")
    pre = [j for j in joints if j.get("preload")]
    if pre:
        names = ", ".join(f"{j['fastener']} ({j['preload']:g} N)" for j in pre)
        out.append(f"Preloaded bolts: {names}. Each bolt is an axial spring of its own stiffness (E x nominal area / "
                   "grip length) between the centres of the bearing faces under head and nut, with the preload as a "
                   "constant force pair; the bearing faces are joined to the bolt over an annulus of 0.8 x the bolt "
                   "diameter. Bending of the bolt and thread flexibility are not modelled. The clamped faces are in "
                   "frictional contact, so the joint can open, and the bolt force is reported per load case.")
    return out


def contact(pairs: list[str], stabilised: list[str] = (), buckling: bool = False) -> list[str]:
    if not pairs:
        return []
    out = [f"Separating contact (penalty, face to face, with friction where given) is used on: {', '.join(pairs)}. "
           "Load cases with contact are solved one by one and cannot be superposed."]
    if stabilised:
        out.append(f"Parts held only by contact ({', '.join(stabilised)}) carry weak springs to ground so the solve "
                   "can start before the contact closes; the share of load they carry is checked and reported.")
    if buckling:
        out.append("Buckling is solved on a linear stand-in of the model in which every contact pair is tied and "
                   "preloaded bolts are bonded.")
    return out


def slides(n: int) -> list[str]:
    """Faces that slide but do not separate (the touching faces of hinged parts)."""
    if not n:
        return []
    return [f"{n} interface(s) slide without separating: the faces stay in contact along their normal, without "
            "friction, and move freely in their plane. This is linear: a face that would really lift off is "
            "still held, so check the reaction there is compressive."]


def couplings(gears: int, screws: int) -> list[str]:
    """Gear, rack and pinion couplings and screw joints (D51)."""
    out = []
    if gears:
        out.append(f"{gears} gear or rack and pinion coupling(s) link the parts' turning (or sliding) by the mate's "
                   "ratio, as if the teeth meshed rigidly and without friction. The tooth contact itself is not "
                   "modelled: the teeth carry no stress, and the separating force of the tooth flanks is left out.")
    if screws:
        out.append(f"{screws} screw joint(s) turn and advance together by the mate's lead, without friction, so a "
                   "screw that would self-lock in reality is free to turn back here; the thread itself is not modelled.")
    return out


def joints(revolute: int, sliding: int) -> list[str]:
    out = []
    if revolute:
        out.append(f"{revolute} revolute joint(s) are linear idealisations: rigid couplings that leave rotation about "
                   "the joint axis free, with no friction or clearance.")
    if sliding:
        out.append(f"{sliding} sliding joint(s) are linear idealisations that leave translation along the slide free.")
    return out


def buckling(has_buckling: bool) -> list[str]:
    if not has_buckling:
        return []
    return ["Linear buckling ignores imperfections, residual stresses and yielding, so its load factors are upper "
            "bounds. A factor above 1 is not proof of stability; the study limit should be well above 1.",
            "Thin walls are one element thick in swept regions, so local wall buckling modes depend on the mesh."]


def welds(strips: list[str], whole: list[str]) -> list[str]:
    """strips: weld beads bonding a strip of their interface; whole: welds bonding all of it."""
    out = []
    if strips:
        out.append(f"Weld beads {', '.join(strips)} bond the touching faces within the bead size of the weld path "
                   "(mesh nodes within that distance are tied); the rest of each welded interface keeps its own "
                   "connection, so it can open or slide. The weld throat is not modelled: stresses next to a weld "
                   "are approximate and weld sizing remains a hand calculation.")
    if whole:
        out.append(f"Weld beads on both sides or all around ({', '.join(whole)}) bond the whole touching interface.")
    return out


def subassemblies(groups: list[str]) -> list[str]:
    if not groups:
        return []
    return [f"The parts of sub-assemblies ({', '.join(groups)}) are analysed as if they were in the top-level "
            "assembly: their own mates are read, and their touching faces are connected like any other's. A rigid or "
            "flexible setting in SolidWorks makes no difference to the analysis."]


def hydrostatic(load_ids: list[str]) -> list[str]:
    """Hydrostatic loads (D54)."""
    if not load_ids:
        return []
    return [f"Hydrostatic loads ({', '.join(load_ids)}): the pressure on each element face is the pressure over the "
            "fluid less 1 atmosphere, plus ρ·g·depth below the top of the fluid (g = 9810 mm/s²), taken at the face's "
            "centre. 1 atmosphere is taken to act on the outside, so an open tank's faces carry ρ·g·depth alone. "
            "Faces above the top of the fluid carry only the gas pressure over it. The fluid's own mass is not added "
            "to the model: it acts only through this pressure."]


def remote(load_ids: list[str]) -> list[str]:
    if not load_ids:
        return []
    return [f"Remote forces ({', '.join(load_ids)}) act at a point off their faces. Each is carried to its faces "
            "as nodal forces: the force spread by area, and its moment about the faces' centroid spread as a "
            "rigid-body pattern (as a distributing coupling does). The faces are not stiffened by it."]


def joint_band(bonded: bool) -> list[str]:
    """How stresses at bonded joints are assessed (D32, D33)."""
    if not bonded:
        return []
    from .. import defaults
    return [f"Bonded joints are assessed by their structural stress: the membrane plus bending stress of the member's "
            f"section at the joint, from the forces the joint carries (Dong's mesh-insensitive method, Battelle). The "
            f"stress right at a sharp bonded edge is singular (it grows as the mesh is refined) and is not used; member "
            f"peaks are read at least {defaults.JOINT_BAND_T:g} x the wall thickness (and {defaults.JOINT_BAND_MIN:g} mm) "
            f"from a bond. The raw peak is shown in brackets. Weld throats and the wall of the part a member is bonded "
            f"onto are not assessed by this method.",
            "Member stresses are classified as in ASME VIII Div. 2 Part 5: the factor of safety uses the membrane plus "
            "bending stress, linearised along a line through the material at the most stressed points (across the "
            "ligament at a notch in a sheet's outline, through the wall elsewhere). Local notch peaks above it are "
            "relevant to fatigue, not static strength, and are shown for reference only."]


def weldment(butts: int, edges: int, tolerance: float | None) -> list[str]:
    """Weldment bonds (D29) and the bond tolerance."""
    out = []
    if butts:
        out.append(f"{butts} butt joint(s) inside weldment parts touch only between the corner radii of the member "
                   "they meet. They are taken as welded: the whole end face is bonded across the radii, which "
                   "treats the weld filling the V as rigid (slightly stiffer than real weld metal).")
    if edges:
        out.append(f"{edges} edge contact(s) between bodies of one weldment part are taken as welded: a narrow strip "
                   "along the contact line is bonded. Stresses right at such a line are not reliable.")
    if tolerance is not None:
        out.append(f"Faces up to {tolerance:g} mm apart, or overlapping by up to {tolerance:g} mm, are treated as "
                   "touching and bonded (the study's bond tolerance).")
    return out


def overrides(n: int) -> list[str]:
    if not n:
        return []
    return [f"{n} connection(s) were decided by the user rather than detected; they are marked in the connection "
            "table."]


def superposition(used: bool) -> list[str]:
    """Load cases as sums of one solve per load (superpose.py, D39)."""
    if not used:
        return []
    return ["Each load was solved on its own and every load case is the sum of its loads, scaled to their sizes "
            "(superposition). For this linear static model that is exact: the displacements, stresses and reactions "
            "are linear in the loads, and von Mises stress is computed from the summed stress tensor."]


def hybrid(used: bool) -> list[str]:
    """Solved by the hybrid solver because CalculiX would not fit in memory (D49)."""
    if not used:
        return []
    return ["This model was too large for CalculiX on this computer, so it was solved with AsmFEA's hybrid solver: "
            "each part is solved on its own and the parts are joined through their interfaces, where each joint "
            "moves as a smooth field (polynomials of degree up to 4 over the joint) rather than node by node. "
            "Against CalculiX on models both can solve it agreed within about 1-3 % on displacement and on stresses "
            "away from joints, with exact reactions; peak stresses right at a joint can differ more."]
