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

    "Peak stresses at sharp re-entrant corners, at fixture edges and at the edges of joints are singular: in a "
    "model with sharp corners they have no finite value and grow with every mesh refinement. Static strength is "
    "therefore judged, as in ASME VIII Div. 2 Part 5, on the membrane plus bending stress: the stress averaged "
    "through the wall plus its linear part, found at the most stressed points of every part, round the joints as "
    "well as away from them. Local peaks above it (notches, the stress next to a joint's edge) matter for fatigue, "
    "not for static strength; the plots show them in magenta beyond the colour key, so they do not hide the rest. "
    "On the joint edges themselves (the elements along the edge of a bond, a slide's footprint or a hinge's face) "
    "the stress is a modelling artefact and is not used; the plots show the stress just beside them there. The "
    "same holds within a band round the edge of a loaded face, where the load's even spread stops abruptly: the "
    "stress there belongs to the idealised load, not the structure, and is not assessed. Where the stress read "
    "beside a joint rose on the finer mesh while the deflection settled (a singular point), the part's factor of "
    "safety uses its peak away from the joint and load edges instead, provided that held steady between the meshes.",
    "The factor of safety is given on yield (no permanent deformation) and on ultimate strength (the margin left "
    "before fracture). The analysis is linear elastic: past yield its stresses are no longer the real ones, so the "
    "factor on ultimate is an index of the reserve, not a predicted failure load; for ductile metals it is on the "
    "safe side. Whether some yielding is acceptable is the engineer's judgement.",
    "Fixtures are rigid: a fixed face cannot move at all, which stiffens the structure next to it compared with "
    "a real support.",
]


def mesh_levels(two: bool) -> list[str]:
    if two:
        return ["The reported results come from the refined mesh. The base mesh is solved as well, and the change "
                "between the two is the convergence figure."]
    return ["Only one mesh was solved (convergence check switched off), so the mesh sensitivity of the results is "
            "not measured."]


def holes(n: int) -> list[str]:
    """Free holes meshed finer round their edges (D101)."""
    if not n:
        return []
    from .. import defaults
    return [f"Holes with no bolt, pin or fixture in them ({n} here) are meshed finer round their edges, about "
            f"{defaults.HOLE_ELEMENTS} elements round the hole, so their notch peaks are resolved (within about 1 % "
            f"of the textbook value on a plate with a hole). Peaks at bolt holes, pins and fixtures depend on how "
            f"those are modelled and are not resolved this way."]


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
    user = [p for p, why in removed_parts.items() if why == "user"]
    if user:
        out.append(f"Parts left out of the analysis by the user ({', '.join(user)}); their stiffness and weight are not "
                   "included. They are not suppressed in SolidWorks.")
    return out


def rigid(groups: list[dict]) -> list[str]:
    """Rigid parts (D96): [{"id", "parts", "mass"}] per group."""
    if not groups:
        return []
    parts = [p for g in groups for p in g["parts"]]
    together = [g for g in groups if len(g["parts"]) > 1]
    out = [f"Parts set as rigid by the user ({', '.join(parts)}) are not meshed and carry no stress: each moves as one "
           "rigid body" + (", and rigid parts bonded to each other move together" if together else "") + ". Their weight "
           "(density from the SolidWorks material times the CAD volume) acts at their centre of mass; loads and fixtures "
           "on their faces act on the rigid body. Their own strength is not checked."]
    out.append("A face of another part bonded to a rigid part cannot deform where it is bonded, which stiffens that part "
               "locally compared with a real, flexible neighbour; the stress at the edge of that bond is a modelling "
               "artefact and is not assessed (as at any bonded joint).")
    return out


def bond_first(method: str, why: str | None, n_bonded: int, small: list[dict], small_fraction: float) -> list[str]:
    """Bond-first connections (D81) and the small parts it left out."""
    if method != "bond_first":
        return []
    out = [f"Connections were found bond-first ({why or 'a model with many parts'}): every pair of parts touching within "
           "the bond tolerance, or overlapping, is bonded, and a mate's joint is kept only where it frees a motion. "
           f"{n_bonded} joint(s) between parts already held together through the structure, or on a pinned face, "
           "were bonded."]
    if small:
        mass = sum(s.get("mass", 0.0) for s in small) * 1000.0
        out.append(f"{len(small)} small part(s), each under {100 * small_fraction:g} % of the model's volume, carrying "
                   f"no load or fixture and not the only link between parts, are left out as negligible "
                   f"(together {mass:.3g} kg, not included in self-weight): "
                   + ", ".join(s["part"] for s in small) + ".")
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
                   "grip length) between the centres of the bearing faces under head and nut. It is first tightened "
                   "to exactly its preload, then its length is locked, so under load its tension is the preload plus "
                   "its stiffness x its stretch since tightening; the bearing faces are joined to the bolt over an annulus of 0.8 x the bolt "
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
        out.append("Parts that lie wholly within a hinge's joint band (bushes, washers) are not assessed: a hinge's "
                   "rigid coupling gives a peak there that grows with mesh refinement. Check the bearing pressure on "
                   "pins and bushes by hand. The parts round a hinge are assessed by their membrane plus bending stress.")
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
            f"stress right at a sharp bonded edge is singular (it grows as the mesh is refined) and is not used. The "
            f"wall a member is bonded onto is assessed by its membrane plus bending stress through the wall at the "
            f"joint. Weld throats are not assessed.",
            "Member stresses are classified as in ASME VIII Div. 2 Part 5: the factor of safety uses the membrane plus "
            "bending stress, linearised along a line through the material at the most stressed points (across the "
            "ligament at a notch in a sheet's outline, through the wall elsewhere), including round the joints. Local "
            "peaks above it are relevant to fatigue, not static strength, and are shown for reference only."]


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


def weld_checks(load_cases: dict, names: bool = True) -> list[str]:
    """Recommended further checks of the joints (the user's request, 2026-10-11): where the stress beside a joint has
    no settled value in the linear model (flagged singular, or set aside for the steady peak, D88/D89), and the
    joints' structural stress (D33, information in the checks). A static factor of safety does not cover a weld's
    strength or fatigue life there. names=False for the summary, whose readers do not know the parts by name."""
    spots: dict[str, list] = {}
    low = None
    for case, lc in load_cases.items():
        for part, p in lc.get("parts", {}).items():
            if p.get("singular") or p.get("singular_fallback"):
                at = p.get("read_out_singular_at") or p.get("membrane_bending_at") or p.get("peak_vm_excl_ties_at")
                spots.setdefault(part, []).append(at)
            jf = p.get("joint_fos")
            if jf and (low is None or jf < low[0]):
                low = (jf, part, p.get("joint_with"))
    out = []
    if spots:
        short = lambda pid: pid.split("/body:")[-1].split("/")[-1]  # noqa: E731
        where = (": " + "; ".join(f"{short(pid)} at ({', '.join(f'{c:.0f}' for c in at[0])}) mm"
                                  if at and at[0] else short(pid) for pid, at in sorted(spots.items()))
                 if names else "")
        out.append(f"Weld or fatigue check recommended at {len(spots)} joint location(s) where the stress beside the "
                   f"joint has no settled value in this linear model (it keeps rising as the mesh is refined){where}. "
                   "The factor of safety above uses the stress just away from them; the joint itself needs a weld "
                   "assessment (weld size and throat, and the structural hot-spot or structural stress against the "
                   "weld's detail category if the loads are repeated).")
    if low is not None:
        joint = ""
        if names and low[1]:
            short = lambda pid: pid.split("/body:")[-1].split("/")[-1]  # noqa: E731
            joint = f" ({short(low[1])}" + (f" at {short(low[2])})" if low[2] else ")")
        out.append(f"The lowest joint structural stress factor is {low[0]:.2f}{joint}: yield over the structural "
                   "stress the joint's forces put into the wall, a measure for the weld rather than the parent "
                   "metal. Check the welds against it with their actual size and type.")
    if out:
        out.append("If the loads are repeated (cyclic), a fatigue assessment of the welded joints is recommended; "
                   "this static analysis does not cover fatigue.")
    return out


def fatigue(doc: dict) -> list[str]:
    """The fatigue report's assumptions (release 2, D105-D107), from results/fatigue.json."""
    out = [
        "Fatigue is assessed on the linear static results of each load case: the stresses scale with the load, and "
        "within a spectrum row the loading is proportional (the stress directions do not turn between its two "
        "states).",
        "Each spectrum row is one cycle between two load states, repeated its number of times. Damage is summed by "
        "Miner's rule (the cycles over the cycles to failure, row by row). Larger cycles that run from one row's "
        "maximum to another row's minimum are not counted unless they are given as a row of their own; a spectrum "
        "from a measured history would need rainflow counting first.",
        f"The stress range at a point is the principal stress of largest magnitude of the change in the stress "
        f"between the row's two states ({'the signed von Mises range' if doc.get('range_measure') == 'signed_von_mises' else 'EN 1993-1-9 Annex B, for nearly proportional loading'}); "
        "the mean stress is the largest principal of the average of the two states.",
        "The fatigue factor is the factor on every stress (range and mean together) at which the damage over the "
        "design life would reach 1.",
        "The governing stress range of each part and joint is required to agree within "
        "5 % between the last two meshes. Life goes roughly as the stress range to the power 3 to 10, so a 5 % "
        "stress difference is 15 % to 60 % in life.",
        "Parent material is read only where a notch peak is finite: the bands round bonded joints, the edges of "
        "loaded faces and fixtures, and sharp inside corners (modelled with no radius) are left out, as their "
        "elastic stress has no finite value. Free holes and fillets are meshed finely enough to resolve their peaks "
        "(within about 1 % of converged on the reference cases).",
    ]
    if doc.get("mean_stress") and doc["mean_stress"] != "none":
        out.append(f"A user's S-N curve is taken as fully reversed (zero mean stress); a cycle with a tensile mean is "
                   f"corrected to an equivalent fully reversed amplitude by the {doc['mean_stress'].title()} rule, "
                   "using the material's ultimate strength. A compressive mean is given no credit. The standards' "
                   "curves already include the effect of residual stress and take no correction.")
    else:
        out.append("No mean-stress correction is made: the S-N data is taken to allow for the mean stress already.")
    if any(p.get("curve_estimated") for p in doc.get("parts", {}).values()):
        out.append("Where no S-N data was given or stored with the SolidWorks material, AsmFEA's built-in data is "
                   "used: measured mean curves (MIL-HDBK-5J) for the alloys it covers, otherwise a curve ESTIMATED from "
                   "the material's tensile strength (the Uniform Material Law for steels and wrought aluminium, the "
                   "FKM fatigue-limit ratio for stainless). Both are reduced to a design curve (x 0.75 on stress) and, "
                   "for steel, by a surface factor for the finish (hot-rolled unless chosen). Estimates are typical "
                   "values for the material class, not data for the actual material: use measured data where the "
                   "result matters.")
    if doc.get("joints"):
        w1 = [j for j in doc["joints"].values() if j["method"].startswith("W1")]
        if w1:
            out.append("Bonded (welded) joints: the structural stress at the weld toe is found from the forces each "
                       "joint carries (Dong's method: the membrane and bending stress of the wall, from equilibrium, "
                       "in windows along the joint), and read on the standard's structural (hot-spot) stress curve. "
                       "At the corners of a tube this stress needs a refined mesh: on a reference T-joint the "
                       "standard mesh read 18 % high and the locally refined mesh 2 % high, against a solid model "
                       "of the same joint.")
        if any(j["method"].startswith("W2") for j in doc["joints"].values()):
            out.append("Joints assessed by their nominal stress use beam theory over the joint's whole section and "
                       "the detail category given for each; the category must suit the joint's real detail.")
        walls = doc.get("walls", {}).values()
        if any("hand formulae" in w["method"] for w in walls):
            out.append("The wall a hollow-section brace is welded onto (the chord's face) is assessed, for square-on "
                       "T-joints inside their range, by the published hand formulae for welded joints (van Wingerde "
                       "1992, the basis of CIDECT design guide 8 and IIW XV-1035-99): the stress concentration factors "
                       "on the chord's hot-spot lines, times the brace's axial and in-plane bending stresses from the "
                       "forces the joint carries, plus the chord's own stress beside the joint, added as absolute "
                       "values. They are for welded joints, so these figures are what a welded joint shows. The "
                       "formulae apply from 4 mm walls; welds started at the brace's corners can be markedly worse.")
        if any("indicative" in w["method"] for w in walls):
            out.append("Where the formulae do not apply (out-of-plane bending of the brace, other joint shapes), the "
                       "wall's hot-spot stress is read from the bonded model at 0.4 and 1.0 wall thicknesses from the "
                       "joint's edge, as an indication only: on a reference joint it read from 40 % low (coarse "
                       "meshes) to 30 % high (refined meshes; the bonded model has no weld bead, which raises it) "
                       "against a solid model and the formulae.")
        out.append("The weld's root and throat are not modelled, so root failure of partial-penetration and fillet "
                   "welds is not covered.")
    if doc.get("bolts"):
        out.append("Preloaded bolts are assessed on the range of their axial tension, from their preloaded state, on "
                   "the tensile stress area. Bolt bending and shear are not modelled.")
    if doc.get("gamma_Ff", 1) != 1 or doc.get("gamma_Mf", 1) != 1:
        out.append(f"Partial factors: {doc['gamma_Ff']:g} on the stress ranges and {doc['gamma_Mf']:g} on the "
                   "fatigue strength.")
    else:
        out.append("No partial factors are applied (gamma_Ff = gamma_Mf = 1): the design curves are the standards' "
                   "(mean minus two standard deviations), or the user's curves as given.")
    return out
