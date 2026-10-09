# Third-party software

Rule (Change 001): no GPL or AGPL code is imported, linked or copied into the Python package or the add-in. GPL tools run only as separate executables that exchange files. Every dependency is listed here with its licence and how it is used. Add the entry before adding the dependency.

Licences were checked on 2026-10-02 against each project's own repository or distribution, except where noted.

## Linked (imported by the Python package)

| Package | Licence | Checked against | Used for |
|---|---|---|---|
| pydantic | MIT | PyPI metadata | Typed model of the neutral files |
| jsonschema | MIT | PyPI metadata | Validating model.json against the schema |
| numpy | BSD-3-Clause | PyPI metadata | Arrays |
| scipy | BSD-3-Clause | PyPI metadata | KD-trees and sparse algebra (face tagging) |
| psutil | BSD-3-Clause | PyPI metadata | Process monitoring, killing process trees |
| numba 0.68.0 | BSD-2-Clause | PyPI metadata, github.com/numba/numba `LICENSE` | Compiles the hybrid solver's element stiffness loop to machine code (added 2026-10-09). Optional at run time: without it the same matrices come from the NumPy code, more slowly |
| llvmlite 0.50.0 (numba's compiler back end) | BSD-2-Clause; the LLVM it bundles is Apache-2.0 WITH LLVM-exception | PyPI metadata, github.com/numba/llvmlite `LICENSE`, `LICENSE.thirdparty` | Used only through numba |
| cadquery-ocp (OCP) 8.0.1 | Apache-2.0 | github.com/CadQuery/OCP `LICENSE` | Python binding to OpenCascade: STEP reading and writing, face fingerprints, booleans for overlap and interference |
| OpenCascade Technology (OCCT), inside OCP | LGPL-2.1 plus the Open CASCADE exception | github.com/Open-Cascade-SAS/OCCT `LICENSE_LGPL_21.txt`, `OCCT_LGPL_EXCEPTION.txt` | Dynamically linked through OCP. The exception allows distributing object code under other terms, with a prominent notice in the documentation that the software uses Open CASCADE Technology. |

Optional extras (`asmfea[report]`, added in milestone 5):

| Package | Licence | Checked against | Used for |
|---|---|---|---|
| python-docx 1.2.0 | MIT | PyPI metadata | The Word report |
| pyvista 0.49.0 | MIT | PyPI metadata | Off-screen plot images |
| vtk 9.6.2 (pyvista dependency) | BSD-3-Clause | PyPI metadata | Rendering |
| matplotlib 3.11 (pyvista dependency, colour maps) | Matplotlib licence (PSF-based, permissive) | PyPI metadata | Colour maps |

Optional extra `asmfea[fast]` (added 2026-10-06 with the hybrid solver, D49). Without it the hybrid solver falls back to SciPy's SuperLU, single-threaded and about twice as slow on a 24-part welded chassis. The installer bundles pypardiso alone (`--no-deps`, about 30 kB). At run time it uses the MKL 2022.2.1 runtime (`mkl_rt.2.dll`) that the installer already downloads beside CalculiX, so the mkl wheels below are not shipped:

| Package | Licence | Checked against | Used for |
|---|---|---|---|
| pypardiso 0.4.7 | BSD-3-Clause | PyPI metadata | ctypes wrapper calling MKL's PARDISO sparse direct solver |
| mkl 2026.1 (pypardiso dependency), with intel-openmp, tbb, umf, tcmlib, intel-cmplr-lib-ur | Intel Simplified Software License (permissive; redistribution allowed with the notice) | PyPI metadata, `onemkl-license` package | The PARDISO factorisation and solves (the same solver CalculiX uses on this machine) |

`mcp` (MIT; `asmfea[mcp]`) is checked again when milestone 7 adds it.

Development only: pytest (MIT), pypdf (BSD-3-Clause; used once to read the CalculiX manual).

## Linked by the add-in (milestone 2)

The add-in uses no third-party packages: JSON is written by its own code and there is no NuGet.

| Component | Licence | How it is used |
|---|---|---|
| .NET Framework 4.8 | Microsoft, part of Windows | Runtime and base class library |
| SolidWorks interop assemblies (`SolidWorks.Interop.sldworks`, `swconst`, `swpublished`) 34.x | Proprietary (Dassault Systèmes), installed with SolidWorks | Referenced at build time from the SolidWorks install folder. Never copied into the repository. Copied only into the git-ignored `addin\bin` to run the tests. |

## Separate executables (never linked, never bundled; located through asmfea.toml)

| Tool | Licence | Checked against | How it is used |
|---|---|---|---|
| Gmsh 4.15.2 | GPL-2.0-or-later, with an exception for Netgen, METIS, OpenCASCADE and ParaView | `LICENSE.txt` in the official Windows zip | `gmsh.exe` run per part on a generated `.geo` script; the mesh comes back as a `.msh` file read by our own reader |
| CalculiX 2.22 | GPL-2.0 | dhondt.de | `ccx` run on the `.inp` deck; results read from `.frd`/`.dat` by our own reader |
| PrePoMax 2.6.0 | GPL-3.0 (from its GitLab project; not re-checked here) | — | Source of the ccx build. Opened as a viewer for manual inspection only. |
| Netgen | LGPL-2.1 | github.com/NGSolve/netgen `LICENSE` | Planned second mesher in the fallback chain (milestone 4). LGPL allows linking, but run it as a separate process unless a decision says otherwise. |
| fTetWild | MPL-2.0 | github.com/wildmeshing/fTetWild `LICENSE.MPL2` | Planned last-resort mesher (milestone 4), separate executable on a tessellation |
| LibreOffice | MPL-2.0 | libreoffice.org (not re-checked here) | Optional `.docx` → `.pdf` conversion (`soffice --headless --convert-to pdf`) when Microsoft Word is not installed |
| Microsoft Word | proprietary, user's own installation | — | Optional PDF conversion through COM when installed |

Not to be used: TetGen (AGPL), CGAL (GPL), and any Gmsh, CalculiX or PrePoMax source code.

Gmsh's Python package (`pip install gmsh`) links the Gmsh library, so it is **not** a dependency, and a test fails the build if `import gmsh` appears under `src/` or `tests/`.

## The installer (installer/, D24)

| Component | Licence | In Setup.exe? | How it is used |
|---|---|---|---|
| Python 3.13.5 embeddable (python.org) | PSF-2.0 | yes | Private runtime in `<app>\python`; AsmFEA and its dependencies installed into its `Lib\site-packages` from wheels (licences in each `*.dist-info`) |
| Inno Setup 6 | Inno Setup licence (free, commercial use allowed) | build tool only | Compiles `installer\AsmFEA.iss` |
| CalculiX 2.22 for Windows (`calculix_2.22_4win.zip`, dhondt.de) | GPL-2.0 | no: downloaded by Setup, SHA-256 pinned | `ccx_dynamic.exe` only; byte-identical to the PrePoMax 2.6.0 build used for development |
| Intel oneMKL 2022.2.1 and Intel OpenMP 2022.2.1 (PyPI wheels `mkl`, `intel-openmp`) | Intel Simplified Software License (redistributable) | no: downloaded by Setup, SHA-256 pinned | `mkl_rt.2`, `mkl_core.2`, `mkl_intel_thread.2`, `mkl_def.2`, `libiomp5md` beside ccx for PARDISO (the same set PrePoMax ships); licence texts copied to `<app>\licenses` |
| Gmsh 4.15.2 (gmsh.info Windows zip) | GPL-2.0-or-later | no: downloaded by Setup, SHA-256 pinned | `gmsh.exe` with its `LICENSE.txt` |
| SolidWorks interop DLLs | proprietary | no | Copied by Setup from the user's SOLIDWORKS 2026 installation |
