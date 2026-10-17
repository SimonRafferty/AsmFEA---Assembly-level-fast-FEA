# AsmFEA: assembly-level fast FEA for SolidWorks

AsmFEA analyses a whole SolidWorks assembly (welded frames, bolted structures, sheet and machined parts) from a task pane inside SolidWorks. It finds the connections between parts, meshes every part automatically, solves the model and writes a Word/PDF report, with no manual meshing and no re-rigging after a design change.

- **Analyses:** linear static and linear buckling, several load cases per run, optional frictional contact and preloaded bolts, and fatigue.
- **Connections:** found from the geometry, mates and fasteners, each listed and editable: bonded, bolted, sliding, and hinges (from concentric, hinge, screw or profile-center mates, or added on one chosen face) whose parts' other touching faces slide without separating. Gear and rack-and-pinion mates couple the parts' turning and sliding. Sub-assemblies are analysed as if their parts were in the top-level assembly, with their own mates. Large rigs of many parts are bonded by proximity first, with mates applied only where they free a motion that matters; small parts hanging off the structure are left out, and floating parts are listed so the bond tolerance can be raised or the parts left out.
- **Simplified models:** parts whose shape is only approximate can be made rigid (they carry load and weight but are not meshed), two parts that do not touch can be joined, and selecting a connection shows it on the model.
- **Meshing:** members and thin walls are meshed with elements swept along the section or through the wall, so a model is many times smaller than a conventional solid mesh; along a member the mesh is fine round its joints and long between them; chunky parts get quadratic tetrahedra.
- **Results:** stresses, displacements, factors of safety and buckling factors, judged as static design codes do: on the membrane-plus-bending stress through the wall, round joints as well as away from them (at a thin wall's joint edge, the surface stress read as welded-joint practice reads it), not on the spikes at sharp modelled edges or at the edges of loaded faces, which have no finite value; where the stress beside a joint keeps rising with the mesh, the joint is listed for a weld or fatigue check. Plots are scaled to that stress, with local peaks beyond it in their own colour. The factor of safety is given on yield and, for information, on ultimate strength. Every full run is solved at two mesh sizes and the change is reported; with Optimise for Accuracy, parts that have not settled are solved a third time on a finer mesh and the converged value is extrapolated.
- **Fatigue:** from the static results, with no new solve: a loading spectrum of load cases cycling between two levels, damage by Miner's rule and a fatigue factor on stress. Welds are assessed by the structural stress at the weld line (or nominal stress with a detail category), the walls under square hollow section T-joints by published stress concentration formulas, parent material by the material's S-N data (the SolidWorks material's own curve, published data, or an estimate marked as such), and preloaded bolts on their stress area. Standards: BS 7608, PD 5500 Annex C, EN 1993-1-9 and EN 13001-3-1. Fatigue has its own summary, report and data report.
- **Speed:** re-runs redo only what changed; a fast run gives an approximate answer for design iterations; models too large for CalculiX's memory, including pinned and hinged rigs, are solved by AsmFEA's own hybrid solver, part by part. On a 174-part welded frame a full run takes about 40 minutes on an 8-core i9 with 64 GB.
- Runs in SolidWorks 2018 to 2026, including Standard and Professional seats.

## Download and install

**[Download the installer: AsmFEA-Setup-0.10.0.exe](https://github.com/SimonRafferty/AsmFEA---Assembly-level-fast-FEA/releases/download/v0.10.0/AsmFEA-Setup-0.10.0.exe)** (Windows 10/11, 64-bit; all releases on the [Releases page](https://github.com/SimonRafferty/AsmFEA---Assembly-level-fast-FEA/releases)).

The installer contains AsmFEA, the SolidWorks add-in and a private Python runtime. During installation it downloads the solver and mesher from their official sources (about 310 MB), each checked against a pinned SHA-256 hash:

| Program | Version | Downloaded from |
|---|---|---|
| CalculiX (solver, `ccx`) | 2.22 for Windows | <http://www.dhondt.de/calculix_2.22_4win.zip> (the author's own Windows build; [CalculiX home page](https://www.calculix.de)) |
| Intel oneMKL runtime (PARDISO solver library for CalculiX) | 2022.2.1 | <https://files.pythonhosted.org/packages/8e/80/ad5fb0f2c9880c3d2b8df3ac508021cf5eb3a6909b250250430e66557bbd/mkl-2022.2.1-py2.py3-none-win_amd64.whl> ([PyPI: mkl](https://pypi.org/project/mkl/2022.2.1/)) |
| Intel OpenMP runtime | 2022.2.1 | <https://files.pythonhosted.org/packages/a0/39/9be1803f45c779ef2df43920fe24140915b992a1e42bf9a37e3beec1ee48/intel_openmp-2022.2.1-py2.py3-none-win_amd64.whl> ([PyPI: intel-openmp](https://pypi.org/project/intel-openmp/2022.2.1/)) |
| Gmsh (mesher) | 4.15.2 | <https://gmsh.info/bin/Windows/gmsh-4.15.2-Windows64.zip> ([Gmsh home page](https://gmsh.info)) |

The bundled Python runtime is the official embeddable package, <https://www.python.org/ftp/python/3.13.5/python-3.13.5-embed-amd64.zip>, with AsmFEA's Python libraries installed into it (listed under Credits).

[PrePoMax](https://prepomax.fs.um.si/downloads/) is optional: AsmFEA can open a run in it for a closer look at the results. It is not downloaded by the installer.

## Documents

- **[Technology overview (PDF)](AsmFEA%20Technology%20Overview.pdf):** what AsmFEA does and how, in plain language: how it reads an assembly, the decision path it follows for every model (matching geometry, finding and classifying connections, checking the model is held, choosing how to mesh each part, planning the run for the computer, solving, checking and assessing the results), the part-by-part solver for large assemblies, fatigue, how each result's accuracy is graded, and the verification evidence.
- **[Quick start guide (PDF)](AsmFEA%20Quick%20Start%20Guide.pdf):** installing, setting up a study, running it and reading the results. The installer also puts it, and a web version, in the Start menu.
- **[Verification report (PDF)](AsmFEA%20Verification%20Report.pdf):** AsmFEA's results against classical hand calculations on four cases (a tube cantilever, a bar under its own weight, a cantilever built from two bonded members, and a buckling column), the CalculiX solver against its own test examples and the NAFEMS LE1 and LE10 benchmarks, and the hybrid solver against CalculiX. Every comparison is within its acceptance criterion. It is a comparison of methods, not a sign-off: check AsmFEA against your own known cases before relying on it.

The SolidWorks models of the verification cases are in [`cases/verification`](cases/verification): each case's `sw` folder holds its assembly and parts, with the study stored in the assembly. They use the SHS 100×100×5 weldment profile in `_profiles` and the material "S355 verification" from `_materials/AsmFEA Verification.sldmat` (copy it into SolidWorks' Custom Materials folder).

## How it works

1. **The SolidWorks add-in** (`addin/`, C#, .NET Framework 4.8) extracts the model: geometry as one STEP file per part, materials, members, mates, fasteners, loads and fixtures. It does no analysis and never modifies your documents.
2. **A neutral model** (`model.json` plus the STEP files, defined by `schema/model.schema.json`) is the only thing passed from SolidWorks to the analysis. Other programs can write it too: a face may be given by a point on it, a part's solid taken from its own STEP file, and a study may set an allowable stress.
3. **The Python pipeline** (`python/src/asmfea`) resolves the connections, meshes each part (Gmsh and AsmFEA's own swept meshers), writes the CalculiX input, solves it, post-processes the results and writes the report. It runs as a background job that the task pane follows.

CalculiX and Gmsh run as separate programs that exchange files with AsmFEA; no GPL code is linked into AsmFEA.

## Building from source

- **Python package:** Python 3.11 or later. From `python/`: `pip install -e ".[report,fast]"`.
- **Add-in:** from the repository root, `powershell -ExecutionPolicy Bypass -File addin\build.ps1 -NoTests` (uses the C# compiler of Visual Studio 2019 Build Tools and the SolidWorks interop assemblies of an installed SolidWorks).
- **Installer:** `python installer\build.py` (needs Inno Setup 6 and Microsoft Edge for the PDF guide). The verification report must exist first (`asmfea verify cases/verification`).
- AsmFEA finds CalculiX, Gmsh and PrePoMax through `%APPDATA%\asmfea\asmfea.toml`, which the installer writes.

## Credits and references

**Programs AsmFEA runs (separate executables):**
- **CalculiX** by Guido Dhondt (solver) and Klaus Wittig. GPL-2.0. <https://www.calculix.de>. G. Dhondt, *The Finite Element Method for Three-Dimensional Thermomechanical Applications*, Wiley, 2004.
- **Gmsh** by Christophe Geuzaine and Jean-François Remacle. GPL-2.0-or-later. <https://gmsh.info>. C. Geuzaine and J.-F. Remacle, "Gmsh: a three-dimensional finite element mesh generator with built-in pre- and post-processing facilities", *International Journal for Numerical Methods in Engineering* 79(11), 2009.
- **Intel oneMKL (PARDISO)** and **Intel OpenMP** runtimes. Intel Simplified Software License. <https://www.intel.com/content/www/us/en/developer/tools/oneapi/onemkl.html>
- **PrePoMax** (optional viewer) by Matej Borovinšek, University of Maribor. GPL-3.0. <https://prepomax.fs.um.si>

**Libraries in AsmFEA's Python runtime:**
- [Python](https://www.python.org) 3.13 (PSF licence)
- [NumPy](https://numpy.org) and [SciPy](https://scipy.org) (BSD-3-Clause)
- [cadquery-ocp](https://github.com/CadQuery/OCP) (Apache-2.0), the Python binding to [Open CASCADE Technology](https://dev.opencascade.org) (LGPL-2.1 with the Open CASCADE exception): STEP reading and geometry. This software uses Open CASCADE Technology.
- [PyVista](https://pyvista.org) (MIT) and [VTK](https://vtk.org) (BSD-3-Clause): plots and the 3-D results window
- [Matplotlib](https://matplotlib.org) (Matplotlib licence): colour maps and charts
- [python-docx](https://github.com/python-openxml/python-docx) (MIT): the Word report
- [pydantic](https://docs.pydantic.dev) and [jsonschema](https://github.com/python-jsonschema/jsonschema) (MIT): the neutral model
- [psutil](https://github.com/giampaolo/psutil) (BSD-3-Clause): job monitoring
- [pypardiso](https://github.com/haasad/PyPardisoProject) (BSD-3-Clause): PARDISO for the hybrid solver
- [numba](https://numba.pydata.org) and [llvmlite](https://github.com/numba/llvmlite) (BSD-2-Clause; LLVM under Apache-2.0 with LLVM exception): compiled element calculations in the hybrid solver

**Build tools:** [Inno Setup](https://jrsoftware.org/isinfo.php) (installer).

**Methods and benchmarks:**
- NAFEMS benchmarks LE1 and LE10 (NAFEMS TNSB Rev. 3, 1990), via [bConverged](http://www.bconverged.com/benchmarks/le1.php).
- Structural stress at welded joints: P. Dong, "A structural stress definition and numerical implementation for fatigue analysis of welded joints", *International Journal of Fatigue* 23, 2001.
- Fatigue design curves: BS 7608:2014+A1:2015; PD 5500 Annex C; EN 1993-1-9:2005; EN 13001-3-1. Hot-spot stress: A. Hobbacher, *Recommendations for Fatigue Design of Welded Joints and Components* (IIW).
- Stress concentration factors of SHS T-joints: A. M. van Wingerde, "The fatigue behaviour of T- and X-joints made of square hollow sections", *HERON* 37(2), 1992; CIDECT Design Guide 8.
- S-N data: MIL-HDBK-5J (aluminium alloys); estimates by the Uniform Material Law (M. Bäumel and T. Seeger, 1990) and the FKM guideline.
- Membrane-plus-bending stress classification: ASME Boiler and Pressure Vessel Code, Section VIII Division 2, Part 5.
- Classical beam formulae: W. C. Young and R. G. Budynas, *Roark's Formulas for Stress and Strain*, 8th ed.; S. Timoshenko and J. Gere, *Theory of Elastic Stability*.

Full licence details: [THIRD_PARTY.md](THIRD_PARTY.md) and `installer/licenses/THIRD-PARTY.txt`.
