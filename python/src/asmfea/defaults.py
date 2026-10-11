"""Every numeric default in asmfea, with where its value came from.

Change a value here, never inline. "Plan" means PLAN.md; "M0" means the milestone 0
calibration (spikes/m0/RESULTS.md). Values marked "guess" must be replaced by a
measurement when a milestone can produce one.
"""

# --- Matching STEP bodies and faces to the model (Neutral model section) -------------
BODY_CENTROID_TOL = 0.05       # mm, guess
BODY_VOLUME_WARN = 0.01        # a matched body whose STEP volume differs from SolidWorks' by more than 1 % is a warning; measured: a 297-body weldment from SolidWorks 2026 read by OpenCASCADE was 0.1 % off typically, 0.6 % at the 95th percentile, 61 % worst, every bounding box within 0.001 mm
FACE_AREA_RTOL = 1e-3          # guess
FACE_CENTROID_TOL = 0.05       # mm, guess: the floor of the centroid tolerance (small faces)
FACE_CENTROID_RTOL = 2e-3      # and the tolerance grows with the face: this times sqrt(area). The add-in's centroid comes from
                               # SolidWorks' display tessellation, whose chords round off holes and fillets: a 20 100 mm2 sheet
                               # metal cheek with holes was 0.073 mm off (exact area), and 0.05 mm flat refused it as
                               # "matches no face" (2026-10-09). 2e-3 gives it 0.28 mm; area, type and normal still match tightly
FACE_NORMAL_COS = 0.999        # guess (~2.5 degrees)
FACE_POINT_TOL = 0.05          # mm, guess: a face_ref point (D53) this close to a face lies on it

# --- Resolver (Connection resolver section) -----------------------------------------
GAP_TOL = 0.5                  # mm, plan: start value, calibrate on reference assemblies
MIN_INTERFACE_AREA = 1.0       # mm^2, guess: slivers below this are dropped
OPPOSING_NORMAL_COS = -0.99    # face pairs must face each other within ~8 degrees, guess

# --- Meshing (Meshing section) -------------------------------------------------------
# Global element size for solids when the study gives none: a fraction of the part's
# smallest bounding-box dimension, clamped. Guess until M4 measures larger models.
SIZE_FRACTION_OF_MIN_DIM = 0.25
SIZE_MIN = 1.0                 # mm
SIZE_MAX = 25.0                # mm
FIDELITY_FACTOR = {"coarse": 1.6, "standard": 1.0, "fine": 0.6}
# Fast run (ADDENDUM-FAST-RUN-MODE, D40): element sizes x standard, span layers and section triangles x wall.
# Speed investigation F1 on the 11-member truss: 253 k DOF against 1.91 M, member stress rank correlation 0.95,
# governing member and first buckling mode the same, displacement 0.98 +/- 0.005 of full, buckling +2 %.
FAST_FIDELITY_FACTOR = 2.5
FIDELITY_FACTOR["fast"] = FAST_FIDELITY_FACTOR
FAST_FALLBACK_FACTOR = 1.6        # a part that will not mesh at the fast size is meshed at the coarse size (angled
                                  # SHS ends failed at 2, 3 and 4 x standard but meshed at 1.6 and 2.5: investigation F1)
FAST_SWEEP_LAYER_T = 40.0         # span layers in a fast run (exact on clean spans: investigation avenue 2)
FAST_SWEEP_SECTION_T = 3.0        # span section triangles in a fast run
REFINE_RATIO = 0.6             # plan: refined re-solve uses 0.6 x the base size
REFINE_FULL_MAX_DOF = 10_000_000  # a refined model larger than this (estimated from the base mesh) is refined only on
                               # its most stressed parts (D73; the user's rigs: 55 M DOF refined), guess
REFINE_EXTRA_MAX_DOF = 10_000_000  # ... the unknowns the selective refinement may add (D80: a cap on the refined model's
                               # total chose one part when the base model alone was 17 M DOF, the cradle), guess
REFINE_SELECT_FRACTION = 0.5   # ... those with an assessed stress of at least half the model's highest (raised until it fits)
REFINE_GROWTH = (1 / 0.6) ** 2.5  # refined / base unknowns, for the estimate (swept walls grow as ^2, tets as ^3)
LOCAL_REFINE_RATIO = 0.6       # the local pass (D65): parts that did not converge again at 0.6 x their refined size

# --- Solve ---------------------------------------------------------------------------
SOLVER_THREADS = 8             # D3: 8 physical cores
HYBRID_DEGREE = 4              # D49: polynomial degree of a joint's modes in the hybrid solver, used when CalculiX
                               # would not fit in memory; 24-part welded chassis -3 %, Bench truss S -0.6 % against CalculiX
# D60 run planner: what each "favour" choice means, and the memory it leaves for everything else
PLAN_FAVOUR = {
    # mesh level, refined convergence pass, hybrid exact-joint caps per part and in all (0: smooth joints only).
    # Totals: m4_frame's 96 welds need 39 600 (D59); the tank's 387 joints at 232 000 ran out of memory
    # refine: the third, local pass where the two solves disagree (D65; the user's choice: on for accuracy)
    "speed": {"fidelity": "coarse", "convergence": False, "refine": False, "exact_part_unknowns": 0,
              "exact_total_unknowns": 0},
    "balanced": {"fidelity": "standard", "convergence": True, "refine": False, "exact_part_unknowns": 10000,
                 "exact_total_unknowns": 60000},
    "accuracy": {"fidelity": "fine", "convergence": True, "refine": True, "exact_part_unknowns": 15000,
                 "exact_total_unknowns": 120000},
}
PLAN_RESERVE_GB = 3.0          # memory left free for other programs to grow into, GB. The free memory measured already
                               # excludes what SolidWorks and the rest use now, and the memory-pressure watch covers
                               # them growing, so the solve takes as much as it can (the user's choice, 2026-10-09:
                               # 6 GB / 15 % left 10 GB of 45 GB free unused)
PLAN_RESERVE_FRACTION = 0.04   # ... or this share of the computer's RAM, whichever is larger
PLAN_SHORT_GB = 4.0            # less than this to spare after the reserve: the plan warns before the run starts
PLAN_CCX_MARGIN = 1.3         # CalculiX is chosen only if its estimate x this fits the budget (the estimate is a
                               # fit to measured peaks with about 25 % scatter: BYTES_PER_DOF)
PLAN_ACCURACY_CAP_MIN_GB = 24.0  # "accuracy" uses its larger joint cap only with at least this budget, GB
ACCURACY_HIGH_DISP = 0.02      # D60 accuracy grade "High": displacements changed at most this between the meshes
ACCURACY_HIGH_STRESS = 0.05    # ... and stresses away from joints at most this (beyond CONVERGENCE_TOL: Indicative)
PRESSURE_TICKS = 5            # memory pressure: this many heartbeats in a row with free memory below the reserve
                               # (or the solver's memory being paged out) before the warning (a few seconds)
PRESSURE_DROP_GB = 3.0        # memory pressure: other programs took more than this since the run started (the run's own
                               # growth not counted). The user's rule (2026-10-09): warn only when the free memory falls
                               # by more than the 3 GB margin during a run, never for what was already so at the start.
                               # A test of the solver's working set against its committed memory (0.6) was dropped: the
                               # solver library commits far more than it touches, and warned with 44 GB free
HYBRID_EXACT_TOTAL_UNKNOWNS = 60000  # D60: ... and while the model's exact joint unknowns stay within this (the global
                                     # system grows with them; see PLAN_FAVOUR)
HYBRID_EXACT_JOINT_NODES = 3000  # D59: a joint with at most this many master nodes is exact (node by node, as
                                 # CalculiX's tie): m4_frame's welds by smooth fields +9.5 %, exact -0.05 %
HYBRID_EXACT_PART_UNKNOWNS = 10000  # D59: ... while no part condenses onto more exact joint unknowns than this.
                                    # m4_frame's busiest chord needs 9894 (every weld exact); at 15000 a tank
                                    # panel took 14 751 and the fast tank, 11 minutes before, was stopped unfinished after 57. A
                                    # dense block of 8 x 10000^2 bytes = 0.8 GB (every tie exact: 119 000, 105 GB)
MEMORY_WARN_FRACTION = 0.7     # plan: warn when estimate exceeds this share of RAM, guess
# PARDISO memory per DOF for quadratic tets. M0: 1.94 M DOF solved; estimate refined
# from logged runs once milestone 4 records them.
BYTES_PER_DOF = 8000           # measured 2026-10-03: >= 62 GB at 8.1 M DOF (24-part welded chassis, quadratic tets), D30
MEMORY_BLOCK_FRACTION = 0.9    # estimate above this share of RAM: not solved (62 GB on 64 GB swapped the PC), D30

# --- Checks (Results, checks and report section) -------------------------------------
REACTION_BALANCE_TOL = 0.01    # plan: within 1 %
CONVERGENCE_TOL = 0.10
JOINT_BAND_T = 1.5             # the joint band: 1.5 x the wall thickness round a joint's edge (D32; hot-spot read-out practice); since D-joint-edge only a part wholly inside is unassessed
JOINT_BAND_MIN = 3.0           # mm: ... and at least this distance (solids, unknown thickness), D32
HOT_SPOT_NEAR_T = 0.4          # thin parts' joint edges: surface stress read from 0.4 t ... (IIW type "a" coarse read-out points 0.5 t / 1.5 t, widened for a fit over nodes; D-joint-edge)
HOT_SPOT_FAR_T = 1.6           # ... to 1.6 t from the edge, extrapolated in a straight line back to it
HOT_SPOT_SPACING_T = 0.5       # one hot-spot read-out per half wall thickness of joint edge, guess
JOINT_EDGE_FILL_NEAREST = 6    # plots: a joint-edge point drawn with the inverse-distance mean of its part's 6 nearest assessed points, guess
SCL_PER_JOINT_CELL = 8        # membrane plus bending in the joint bands: at most 8 sections per cell 3 walls wide, so every joint gets some (D-joint-edge), guess
JOINT_PATCH_T = 3.0            # joint structural stress read over windows 3 wall thicknesses long (D33), guess
JOINT_WINDOW_ELEMENTS = 1.0    # window half-length >= 1 element: windows span >= 2 elements along the joint (D33)
REPORT_JOINT_ROWS = 12          # bonded joints listed per load case in the report, highest stress first
RECEIVING_WALLS = False        # Battelle on the receiving wall (D34): passes the flat-wall hand check but varied 0.6-3.2x
                               # between meshes on tube joints with small, wrapping footprints: not used in checks yet
SCL_CANDIDATES = 200           # most stressed surface nodes per part linearised for membrane plus bending (D34)
SCL_POINTS = 21                # points along a stress classification line (D34)
SCL_INTERP_DEPTHS = 5          # fewer distinct depths along an SCL: fitted (straight / quadratic), not interpolated (D34)
SCL_EDGE_T = 2.0               # thin parts: face points within 2 x the wall of an edge are left to the edge's in-plane SCL (D34), guess
WALL_SAMPLE_FACETS = 200       # largest outer faces a part's wall thickness is measured through (D34)
WALL_THIN_FRACTION = 0.3       # ... and it is a wall when thinner than 0.3 x the part's middle dimension, guess
SCL_LATERAL = 0.25             # nodes within 0.25 x the SCL length of the line give its stress profile (D34), guess
SCL_LIGAMENT_T = 20.0          # thin parts: an in-plane SCL is cut at 20 x the wall; a longer one is a free end (a tube's cut end ran 5.9 m), not a notch ligament; beyond it the stress is nominal, so the cut is on the safe side (found on C4)
SINGULAR_RISE = 0.15           # a peak rising > 15 % on the finer mesh while deflection settles: a singular point (D32), guess         # plan: within 10 %

# --- Jobs (Interfaces section) --------------------------------------------------------
CANCEL_GRACE_S = 10.0          # Cancel: seconds a job has to stop on request before its process tree is ended
HEARTBEAT_S = 1.0              # plan: heartbeat every second or two
STALE_HEARTBEAT_S = 30.0       # heartbeat older than this with no live worker = stalled

# --- Sanity of solved results (Diagnostics section, "results that solved but look wrong")
LARGE_DISP_FRACTION = 0.02     # warn above 2 % of the model diagonal, guess
SINGULAR_DISP_FRACTION = 1.0   # M1: PARDISO returns ~1e9 mm on a mechanism; anything this big is singular
SINGULAR_IMBALANCE = 0.10      # M1: the mechanism case showed 103 % imbalance; 10 % is far outside FE error

# --- Resolver, milestone 3 -------------------------------------------------------------
NEAR_MISS_FACTOR = 5.0         # face pairs within 5 x GAP_TOL but beyond it are near misses, guess
OVERLAP_TOL = 0.05             # mm: faces this far into each other still count as touching, guess
OVERLAP_VOL_TOL = 1.0          # mm^3: solids overlapping more than this are interference, guess
OVERLAP_FUZZY_MM = 1e-3        # the interference check's intersections treat sub-shapes this close as coincident: a bush
                               # against its link took 268 s plain and 0.06 s fuzzy, every overlap volume of that model the
                               # same at 0.001 and 0.01 mm (measured 2026-10-09)
TIE_AREA_RATIO = 4.0           # tie sides differing this much in area (an edge on a face): the small one is the slave (D30), guess
COAX_TOL = 0.1                 # mm: axis offset for coaxial cylinders, guess
# Hydrostatic loads (D54)
WATER_DENSITY = 1000.0         # kg/m^3, fresh water (the user's default)
ATMOSPHERE_MPA = 0.101325      # MPa, one standard atmosphere: the default surface pressure, and taken to act outside
GRAVITY_MM_S2 = 9810.0         # mm/s^2: as the task pane's gravity default (standard gravity is 9806.65)
HYDRO_DOWN = (0.0, -1.0, 0.0)  # the fluid's down direction when a load gives none: SolidWorks' up is +Y
# A hinge from one chosen face (D50): faces found on its axis must be within this radius of it (a pin in a
# clearance hole), and continue it along the axis without a longer gap than max(radius, HINGE_FIND_GAP). Guesses:
# loose enough for an H11 clearance hole, tight enough to leave a gear's rim off its bore.
HINGE_FIND_RADIUS = 1.0        # mm
HINGE_FIND_RADIUS_REL = 0.15   # of the radius
HINGE_FIND_GAP = 5.0           # mm
BOND_FIRST_MIN_PARTS = 50       # D81: models with more parts use bond-first connections (the user's choice, 2026-10-10)
SMALL_PART_VOLUME_FRACTION = 0.001  # ... and leave out parts under 0.1 % of the total volume that carry nothing (the user)
FLOATING_GAP_CANDIDATES = 8     # a floating part's gap is measured exactly to this many nearest parts (by bounding box)
NEAR_COAX_MM = 0.5             # hinges / pins within 0.5 mm of one line along their whole length share it exactly (2026-10-10), guess
SPIN_CENTRE_FRACTION = 0.15    # a part spins on its axis (pin, boss, bush) when its box centre is within 0.15 x its box diagonal of the axis; further off it swings (2026-10-10), guess
LINE_MIN_LENGTH = 0.01         # mm: an edge contact shorter than this is a point contact, not bonded along a line (2026-10-10)
LINE_BOND_WIDTH = 2.0          # mm: strip bonded along an edge contact inside a weldment (D29), guess
BUTT_ADJUST = False            # True closes a butt gap by moving nodes (*TIE ADJUST=YES); measured worse on stress (+24 %) than rigid links (-6 %), D31
BUTT_COVERED_FRACTION = 0.97   # a weldment butting face covered less than this is bonded whole (D29), guess
HOLE_CLEARANCE_MAX = 1.5       # hole diameter up to 1.5 x fastener diameter counts as its hole, guess

# --- Mesh backends (Change 001) -----------------------------------------------------------
MESH_TIMEOUT_S = 600.0         # per part per backend; a timeout moves on to the next backend, guess
MESH_CURVATURE_POINTS = 12     # gmsh Mesh.MeshSizeFromCurvature: elements per 2*pi of curvature
# Curvature refinement may shrink elements to this fraction of the target size, no further.
# M0 rerun (Change 001): with size/10 the 2.5 mm inner corner radii of an SHS 100x5 tripled
# the tet-region node count (143k -> 490k) for no gain in the measured response.
MESH_RETRY_SIZE_FACTORS = (0.5, 0.25)  # a part no mesher can mesh is tried again at these fractions of its element size; measured: a 297-body weldment's failing bodies (surface mesh crossing itself, or still inverted after optimising) all meshed at 0.5
MESH_SIZE_MIN_FRACTION = 0.5
# A tet with less quality (mesh.backends.tet_quality: 1 regular, 0 flat) is refused like an inverted one (D86).
# Measured: gmsh left three flat tets (corners on one plane through an 8 mm wall, quality 2e-15) in a pinned cradle frame's
# tube ends; each gave stiffness entries of 1e20 against 1e7 for the rest, whose round-off (1e4 N/mm) put a 2.5 %
# sideways reaction on its pins. Sound gmsh/netgen meshes here stay above 1e-2; 1e-6 is far below any usable tet.
TET_FLAT_QUALITY = 1e-6

# --- Swept meshing, milestone 4 (D6 option A) -----------------------------------------------
# Member clean spans: the section is meshed in 2D at SWEEP_SECTION_T x wall thickness (one
# quadratic element through the wall, M0 section 3: within 1-2 % in wall bending) and swept
# along the axis in layers of SWEEP_LAYER_T x wall thickness (M0 section 5: a = 5 t default,
# up to 10 t fine for global response).
SWEEP_SECTION_T = 1.0
SWEEP_LAYER_T = 5.0
# Constant-thickness walls (sheet, member end regions): one skin is meshed in 2D at
# SKIN_SIZE_T x thickness and swept through the thickness, one quadratic wedge through the
# wall. Calibrated on the M0 local wall-bending case (spikes/m0/m0_local_skin.py): 2 t, 3 t
# and 4 t all give 0.989-0.990 of the hex reference (tets at h = t: 0.27 on flat walls);
# 3 t keeps some resolution of stress gradients for 78k DOF on that case.
SKIN_SIZE_T = 3.0
# A solid part shaped like a plate or a bar (a box dimension at least SOLID_SKIN_ASPECT x the next smaller one)
# is swept through its thickness like sheet (D56: a 10 x 20 x 993 foam strip took 427 000 tet nodes; swept, about
# 1 300). Its in-plane size is also held to its length / SOLID_SKIN_SPAN_DIV, so a short bar is not left with a
# handful of elements along it. Guesses, checked on the m1 blocks and the verification cases.
SOLID_SKIN_ASPECT = 4.0
SOLID_SKIN_SPAN_DIV = 20.0
SOLID_SKIN_FILL = 0.6          # ... and only a solid that fills most of its box: a plate or bar, not a frame of tubes
                               # (a fused tee of two SHS passed the aspect test and was skin-swept through its walls)
SKIN_FALLBACK_SIZE_T = 1.0     # a sheet tet-meshed instead (sweep refused): tet size = thickness (D30), guess
SKIN_THICKNESS_RTOL = 0.03     # skin pairs must be this close to one thickness, guess
SKIN_MIN_ASPECT = 4.0          # a skin-swept wall: largest face at least 4 x the thickness across (sqrt area), guess (D30)
SKIN_SIDE_FACTOR = 1.5         # a face whose inward ray runs further than 1.5 t is a side face, guess
SWEEP_SURFACE_TOL_T = 0.01     # tagging tolerance for swept meshes, x thickness (analytic offsets are exact)

# --- Plots and report (milestone 5) ----------------------------------------------------------
PLOT_SIZE = (1400, 1000)       # px
CLASSIFY_WORKERS = 6           # parts classified at once (membrane plus bending) in worker processes ...
CLASSIFY_POOL_MIN_NODES = 500000  # ... on a model of at least this many nodes (a worker loads the model in a few
                                  # seconds: small models stay in one process)
PLOT_DEFORM_FRACTION = 0.05    # deformed shape scaled so the largest motion is 5 % of the model size
PLOT_LEADER_DASH = 0.008       # report images: leader-line dashes 0.8 % of the model's size long, guess
PLOT_LABEL_OFFSET = 0.12       # min / max labels set off the model by 12 % of its size, with a leader line
PLOT_FOS_CAP = 10.0            # factor-of-safety plots saturate above this
PLOT_BUCKLING_MODES = 3        # mode-shape images per buckling case
PLOT_MODE_FRACTION = 0.08      # mode shapes drawn with the largest motion at 8 % of the model size

# --- Contact and preloaded bolts (milestone 10) ----------------------------------------------
CONTACT_PENALTY_FACTOR = 10.0  # face-to-face penalty slope K = factor x E / slave face size (MPa/mm), guess
CONTACT_STICK_FRACTION = 0.1   # friction stick slope = fraction of K, guess
CONTACT_INC_INITIAL = 0.1      # nonlinear incrementation: initial increment of the unit step period
CONTACT_INC_MIN = 1e-5         # smallest increment before CalculiX gives up (-> CONTACT_NOT_CONVERGED)
CONTACT_INC_MAX = 0.5
STAB_SPRING_FRACTION = 1e-6    # stabilising springs: total stiffness = fraction x E x part size per direction, guess
STAB_SPRING_WARN = 0.01        # springs carrying more than 1 % of the applied load: warning
STAB_SPRING_BLOCK = 0.10       # ... more than 10 %: the part is not really supported (blocking)
CONTACT_OPEN_TOL = 1e-3       # mm: a contact node further apart than this counts as open (reporting only), guess
BOLT_BEARING_RADIUS = 0.8      # bearing annulus outer radius = factor x bolt diameter (head ~1.6 d), guess
DEFAULT_BOLT_FRICTION = 0.2    # clamped faces of preloaded bolts when no friction is given, guess

# --- Pass/fail criteria a new study starts with (task pane, D28) -------------------------------
# Written into the study by the add-in (AsmFea.AddIn TaskPane, DefaultMinFos etc.; keep both in step),
# shown on its Study tab and used in the reports; the user may change or blank them.
PANE_MIN_FOS = 1.5             # yield; a common static design factor for general machinery, judgement
PANE_MIN_BUCKLING_FACTOR = 3.0 # linear (eigenvalue) buckling over-predicts real collapse loads; ~3 is usual
PANE_DEFLECTION_RATIO = 250    # max deflection = largest assembly dimension / 250 (span/250 serviceability)
