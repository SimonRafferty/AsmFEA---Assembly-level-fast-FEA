"""Every numeric default in asmfea, with where its value came from.

Change a value here, never inline. "Plan" means PLAN.md; "M0" means the milestone 0
calibration (spikes/m0/RESULTS.md). Values marked "guess" must be replaced by a
measurement when a milestone can produce one.
"""

# --- Matching STEP bodies and faces to the model (Neutral model section) -------------
BODY_CENTROID_TOL = 0.05       # mm, guess
BODY_VOLUME_WARN = 0.01        # a matched body whose STEP volume differs from SolidWorks' by more than 1 % is a warning; measured: a 297-body weldment from SolidWorks 2026 read by OpenCASCADE was 0.1 % off typically, 0.6 % at the 95th percentile, 61 % worst, every bounding box within 0.001 mm
FACE_AREA_RTOL = 1e-3          # guess
FACE_CENTROID_TOL = 0.05       # mm, guess
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

# --- Solve ---------------------------------------------------------------------------
SOLVER_THREADS = 8             # D3: 8 physical cores
HYBRID_DEGREE = 4              # D49: polynomial degree of a joint's modes in the hybrid solver, used when CalculiX
                               # would not fit in memory; 24-part welded chassis -3 %, Bench truss S -0.6 % against CalculiX
MEMORY_WARN_FRACTION = 0.7     # plan: warn when estimate exceeds this share of RAM, guess
# PARDISO memory per DOF for quadratic tets. M0: 1.94 M DOF solved; estimate refined
# from logged runs once milestone 4 records them.
BYTES_PER_DOF = 8000           # measured 2026-10-03: >= 62 GB at 8.1 M DOF (24-part welded chassis, quadratic tets), D30
MEMORY_BLOCK_FRACTION = 0.9    # estimate above this share of RAM: not solved (62 GB on 64 GB swapped the PC), D30

# --- Checks (Results, checks and report section) -------------------------------------
REACTION_BALANCE_TOL = 0.01    # plan: within 1 %
CONVERGENCE_TOL = 0.10
JOINT_BAND_T = 1.5             # stresses within 1.5 x the wall thickness of a bonded joint are not assessed (D32; hot-spot read-out practice)
JOINT_BAND_MIN = 3.0           # mm: ... and within at least this distance (solids, unknown thickness), D32
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
TIE_AREA_RATIO = 4.0           # tie sides differing this much in area (an edge on a face): the small one is the slave (D30), guess
COAX_TOL = 0.1                 # mm: axis offset for coaxial cylinders, guess
# A hinge from one chosen face (D50): faces found on its axis must be within this radius of it (a pin in a
# clearance hole), and continue it along the axis without a longer gap than max(radius, HINGE_FIND_GAP). Guesses:
# loose enough for an H11 clearance hole, tight enough to leave a gear's rim off its bore.
HINGE_FIND_RADIUS = 1.0        # mm
HINGE_FIND_RADIUS_REL = 0.15   # of the radius
HINGE_FIND_GAP = 5.0           # mm
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
SKIN_FALLBACK_SIZE_T = 1.0     # a sheet tet-meshed instead (sweep refused): tet size = thickness (D30), guess
SKIN_THICKNESS_RTOL = 0.03     # skin pairs must be this close to one thickness, guess
SKIN_MIN_ASPECT = 4.0          # a skin-swept wall: largest face at least 4 x the thickness across (sqrt area), guess (D30)
SKIN_SIDE_FACTOR = 1.5         # a face whose inward ray runs further than 1.5 t is a side face, guess
SWEEP_SURFACE_TOL_T = 0.01     # tagging tolerance for swept meshes, x thickness (analytic offsets are exact)

# --- Plots and report (milestone 5) ----------------------------------------------------------
PLOT_SIZE = (1400, 1000)       # px
PLOT_DEFORM_FRACTION = 0.05    # deformed shape scaled so the largest motion is 5 % of the model size
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
