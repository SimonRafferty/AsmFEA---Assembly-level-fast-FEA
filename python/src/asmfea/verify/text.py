"""Wording of the verification report, shared by the Word (report.py) and web (html.py) versions."""

USER_NOTE = ("Users should satisfy themselves of AsmFEA's accuracy for their own work, by running their own tests "
             "against hand calculations or other trusted results, before relying on it.")

PURPOSE = ("This report compares AsmFEA's results with classical hand calculations on a few simple cases, and checks "
           "the CalculiX solver against its own test examples and two standard NAFEMS benchmarks. It is a comparison "
           "between methods, not an approval of any particular analysis.")

NOT_STRESS = ("The add-in does not calculate stress. It reads the geometry, materials, connections and loads from "
              "SolidWorks, builds a finite element model, and passes it to CalculiX. CalculiX solves it. AsmFEA then "
              "reads the results back and reports them. A model too large for CalculiX to hold in the computer's "
              "memory is solved instead by AsmFEA's own hybrid solver (section 3b), which reads the same input file "
              "and writes its results in the same form.")

TWO_LAYERS = ("Two things are therefore shown separately: that the solver solves a correctly posed model accurately "
              "(CalculiX alone), and that AsmFEA poses each model correctly and reports it correctly (the full chain "
              "from SolidWorks).")

SOLVER_INTRO = ("CalculiX is a free, open-source finite element program by Guido Dhondt (solver) and Klaus Wittig "
                "(pre- and post-processor), distributed under the GNU GPL. AsmFEA runs it as a separate program. "
                "CalculiX has no formal certification and no vendor verification manual of the kind published for the "
                "large commercial codes; it is not described here as certified or validated by any body. What exists "
                "is its developer's own test examples and published benchmark comparisons, and both were run on the "
                "exact binary AsmFEA uses.")

METHOD = [
    "Full chain: every case starts as a SolidWorks model and runs through the add-in, the pipeline and the report "
    "generator, with nothing edited by hand in between. Loads and fixtures were set through the add-in on faces "
    "selected in SolidWorks.",
    "Theory first: each classical calculation was written and committed before the analysis for that case was run, "
    "so neither could be adjusted to agree with the other.",
    "Like with like: slender members (length at least 20 times depth); section properties from the modelled section "
    "with its corner radii; shear deflection included; stresses read at least one section depth clear of supports "
    "and load points; nominal stress compared with nominal stress.",
    "Default settings: every case at the default mesh fidelity (standard), which runs a base and a refined mesh and "
    "reports the refined one; C1 also at coarse and fine.",
    "Read-outs: each value is the mean of CalculiX's nodal results over a node set defined by coordinates, stated "
    "with each result; global values (mass, reaction, buckling factor) come from AsmFEA's results.",
    "No tuning: no mesh setting was changed per case to obtain a pass.",
    "SOLIDWORKS Simulation, where supplied, is a second opinion, not an acceptance criterion: two finite element codes "
    "agreeing is weaker evidence than agreement with theory, since they share the same kind of assumptions.",
]

CRITERIA = [("Deflection and rotation", "within 5 % of theory"),
            ("Nominal stress at the stated read-out point", "within 5 %"),
            ("Buckling load", "within 5 %"),
            ("Reactions", "within 1 % of applied load"),
            ("Mass", "within 0.5 % of the SolidWorks mass properties")]

NO_SWS = ("SOLIDWORKS Simulation results for the static cases can be added (SOLIDWORKS Premium, linear static). "
          "SOLIDWORKS Premium has no buckling study (linear buckling needs the separately licensed SOLIDWORKS "
          "Simulation Professional), so C4 is compared with theory alone: AsmFEA can run this analysis and SOLIDWORKS "
          "Premium cannot.")

CASE_NOTES = {
    "C1": "The tip deflection is 0.39 % above beam theory (Euler-Bernoulli plus Timoshenko shear) at the default mesh "
          "and rises slightly with refinement, to 0.61 % on the fine mesh: the solid model represents what beam theory "
          "leaves out, chiefly the restraint and local flexibility at the clamped end face and the real distribution "
          "of shear in the thin walls (the theory uses an approximate shear area). Mid-length stress agrees to 0.001 % "
          "at every mesh. The convergence check is flagged because the peak stress sits at the clamped root, a stress "
          "singularity that grows with refinement; the read-out points are far from it.",
    "C2": "Mass, reaction and bending agree to better than 0.05 %. The total reaction is AsmFEA's support reaction, "
          "which includes the self-weight lumped on the supported faces (found and corrected during this "
          "verification).",
    "C3": "The tie between the two members adds 0.23 % to the C1 tip deflection; stress at z = 1500 agrees to 0.001 %.",
    "C4": "The buckling load is 0.4 % below Euler and 0.2 % below Euler with the shear correction (155.75 kN); the two "
          "lowest modes are equal, as for a square section.",
}

MESH_NOTE = ("The stress at the read-out point is converged to within a few thousandths of a percent at every mesh. "
             "The tip deflection rises slightly with refinement and is still settling, a little above beam theory (see "
             "the C1 comment), well inside the 5 % criterion. The tool's own convergence check compares each run's base "
             "and refined meshes and reports the largest change in peak stress or displacement: it flags the standard "
             "and fine runs, because the peak stress at the clamped root is a singularity that does not converge, and "
             "does not flag the coarse run, where that peak changed by 8.6 %, under its 10 % limit. This shows the "
             "check flags an unconverged peak; it also shows that a peak at a sharp clamp should be read with care "
             "whatever the mesh.")

HYBRID_TITLE = "Large models: the hybrid solver"

HYBRID_INTRO = (
    "CalculiX solves the whole model as one system of equations, and its memory grows with the model: about 8 GB per "
    "million unknowns. A model that would not fit is no longer refused. AsmFEA solves it with its own hybrid "
    "solver: each part is solved on its own and reduced to its joints (static condensation), the joints of all parts "
    "are solved together, and each part's full result is then recovered. Memory is set by the largest part, not the "
    "whole model, and parts are solved side by side within the memory the computer has free. Each joint between parts "
    "moves as a smooth field (polynomials of degree up to 4 over the joint area) rather than node by node, which is "
    "the one approximation it adds. A model CalculiX can hold is still solved by CalculiX, so every case in section 5 "
    "is a CalculiX result; the report of a study solved by the hybrid solver says so in its assumptions.")

HYBRID_HOW = [
    "Each part is assembled and factorised alone (Intel MKL PARDISO, the same direct solver CalculiX uses); a part with many "
    "joints is condensed with PARDISO's Schur complement.",
    "Parts are solved side by side within a memory budget (the free memory less a reserve); results that do not fit "
    "wait on disk.",
    "The solve checks itself: reactions must balance the applied load, and a model that is not held together is "
    "refused (as with CalculiX) rather than returning numbers.",
    "Decks it does not solve (buckling, contact, preloaded bolts, rigid couplings) stay with CalculiX.",
]

# the comparisons made when it was built (D49), with the same input file solved by both where both fit
HYBRID_EVIDENCE = [
    ("Benchmark truss S: 72 welded SHS members, 4.0 M unknowns",
     "Against CalculiX on the same input: largest displacement -0.64 %, worst node within 0.70 % of it, 95th "
     "percentile stress 0.00 %, reactions equal. One peak stress at a joint -12.7 %. "
     "Hybrid 159 s and 15 GB; CalculiX 193 s and 30 GB."),
    ("Welded tube chassis: 24 parts (rectangular tubes, plates and brackets), 90 bonded joints, 0.34 M unknowns",
     "Against CalculiX: largest displacement -3.1 %, 95th percentile stress -3.6 %, reactions equal."),
    ("Benchmark truss L: 296 welded SHS members, 17.2 M unknowns (too large for CalculiX in 64 GB)",
     "Tip deflection under 3 g self-weight 54.0 mm, against a hand-calculated lower bound of 52.4 mm (chord bending "
     "only; the lacing adds shear deflection) and SimSolid's 53 mm at 4 passes. Reactions equal the weight (8113 N). "
     "Hybrid 15 min at 30 GB; SimSolid 52 min."),
]

HYBRID_NOTE = ("Times measured on an 8-core Intel i9 workstation with 64 GB of memory. Peak stresses right at a joint differ more than stresses elsewhere, because the joint's smooth field "
               "spreads the load a little differently from CalculiX's node-by-node tie; the factor of safety in the "
               "report already excludes the band next to a tie (section 5b).")

LIMITATIONS = [
    "Linear elastic material, small displacements, linear buckling (no imperfections, no plasticity).",
    "Not covered by a case: separating contact and friction, preloaded bolts, revolute and sliding joints, weld beads "
    "drawn in SolidWorks, pressure loads, remote torque. The joint and notch methods are checked on hand-written models "
    "(M1-M4), not through SolidWorks.",
    "The wall of the part a member is bonded onto (the receiving side of a joint) is not assessed by the joint method: "
    "the method built for it passes its hand check (M4) but is not yet stable between meshes on tube joints with small "
    "footprints, so it is off in normal runs. Stresses there are read by membrane plus bending outside the joint band.",
    "Weld throats are not assessed; the joint method gives the stress in the members at the joint.",
    "Fatigue and dynamics are not part of AsmFEA's first release and are not verified.",
    "The cases in section 5 are solved by CalculiX. The hybrid solver, used only when CalculiX would not fit in memory, "
    "is checked by the comparisons in section 3b, not by its own full-chain cases.",
    "Four cases cannot cover every geometry; they test the parts of the chain listed under each case.",
    "Neither CalculiX nor AsmFEA is certified or validated by any body. This report compares two methods on a few "
    "cases; it is not an approval of any particular analysis. " + USER_NOTE,
]

FINDINGS = [
    "C2's first run was refused by AsmFEA's mechanism check: two plain pinned ends leave a bar free to spin about its "
    "own axis. The model was corrected (twist held at one end); the classical calculation did not change.",
    "C2 showed that AsmFEA reported the reaction from CalculiX's RF output, which leaves out load applied directly to "
    "held nodes (here 0.10 %, the self-weight on the supported faces). AsmFEA now also reports the support reaction, "
    "the full load the supports carry, and its report's resultant forces use it. The analysis itself was not affected: "
    "its balance check already included that load.",
    "The automation calls used to set up and capture these models arrived on a background thread; the add-in now "
    "hands pane work to its own thread.",
    "On a real assembly (a welded tube chassis) the raw peak stress at bonded junctions grew with each mesh "
    "refinement (726 to 1105 MPa), a singularity. This led to the joint and notch methods below; the checks M1-M4 "
    "were written for them.",
    "On the same assembly, sheet parts with small cut-outs were meshed with fine tetrahedra (8 million unknowns) "
    "because the sweep mistook a narrow strip beside a cut-out for a thinner wall; and a plate meeting tube edges "
    "floated because the wrong side of each tie was chosen. Both were fixed (side walls left out of the thickness "
    "test; the small side of an edge-on-face tie is tied; ties claim only the nodes over the touching area).",
    "A weldment profile the add-in could not read left tubes without a wall thickness; AsmFEA now measures it from "
    "the mesh.",
]

METHODS_INTRO = ("A sharp edge bonded to another part, or a sharp notch, gives a stress that grows without limit as "
                 "the mesh is refined: the peak there is a property of the model, not of the design. AsmFEA therefore "
                 "classifies stresses before checking them against yield, using established methods. Each is listed "
                 "with the situations it is there for and the evidence for it.")

JOINT_METHODS = [
    ("Bonding by proximity", "Faces within the study's bond tolerance (default 0.5 mm), whether a gap or an "
     "overlap, count as touching and are bonded; parts need not be mated.",
     "Parts placed rather than mated; small modelling gaps or overlaps between parts.",
     "Automated tests: a 0.8 mm gap is left unbonded at the default and bonded at 1 mm; a 0.3 mm overlap is bonded "
     "without an interference warning."),
    ("Weldment joints", "Inside one weldment, bodies meeting only along an edge are bonded along a 2 mm strip, and a "
     "member butted against another's side is bonded over its whole end, across the gap at the corner radii, as "
     "it is welded.",
     "Rectangular and square tubes butted against another's side: only the flat between the radii touches, leaving "
     "a V gap a welder fills, which SimSolid and plain contact searches leave unbonded.",
     "M3 below: the bonded joint deflects within 11 % of the same frame with its gaps weld-filled; without the rule "
     "it matches the open gaps."),
    ("Edge on a face", "Where a plate's edge meets a face, the small side is tied onto the large one, and a tie uses "
     "only the nodes over the touching area.",
     "Thin plates welded or bonded edge-on to tubes or plates; coarse meshes on the large face.",
     "Found on the chassis (a plate floated); automated tests and the M1 check use this joint."),
    ("Structural stress at bonded joints", "The joint is read from the forces it carries (Dong's mesh-insensitive "
     "structural stress, Battelle; the method in Verity): the forces at the bonded face of the member are turned "
     "back into a traction field and integrated along the joint into membrane, bending and shear: "
     "sigma = f / t + 6 m / t^2.",
     "Plate edges and tube ends bonded to other parts, where the raw peak is singular.",
     "M1 below: the average joint bending equals the hand answer exactly (statics), the worst point matches the "
     "plate's own stress at its root, and coarse and standard meshes agree to 1 %."),
    ("Membrane plus bending of member stresses", "As in ASME VIII Div. 2 Part 5: at the most stressed points the stress "
     "is linearised along a line through the material; membrane plus bending is checked against yield, the peak "
     "above it is a notch effect for fatigue. In thin parts the line runs across the ligament from a notch's edge.",
     "Holes, notches and internal corners in plates and tubes; local peaks above yield that a ductile material "
     "redistributes.",
     "M2 below: the ligament membrane stress beside a hole is within 6 % of statics; membrane plus bending lies "
     "between it and the notch peak. On the chassis it was stable to 1-8 % between meshes where peaks moved up to "
     "60 %."),
    ("Joint band and singular-peak flag", "Member peaks are read at least 1.5 x the wall thickness (and 3 mm) from a "
     "bond's edge; with the convergence check on, a peak that keeps rising on the finer mesh while the deflection "
     "settles is flagged as a singular point and does not fail the check.",
     "Remaining sharp corners and bond edges not covered above.",
     "Automated tests (the flag's broken case); on the chassis the joint peak rose 52 % between meshes while the "
     "assessed value rose 14 %."),
    ("Wall thickness from the mesh", "Where the model gives none, a part's wall thickness is measured from its mesh "
     "(rays through it from its largest faces).",
     "Weldment profiles the add-in cannot read.",
     "On the chassis: 1.5 mm measured on all 16 tubes (RHS 25 x 25 x 1.5)."),
]
