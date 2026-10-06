# asmfea SolidWorks add-in

C# (.NET Framework 4.8) add-in for SolidWorks 2026. It exports the neutral model (`model.json` plus one STEP file per unique part and configuration) and exposes the automation object that the asmfea API and MCP use. It contains no analysis logic.

| Project | Contents |
| --- | --- |
| `AsmFea.Extract` | Extraction: the assembly walk, materials, members, sheets, fasteners, mates, face fingerprints, and the JSON writer |
| `AsmFea.AddIn` | The `ISwAddin` class: the Export command, the COM automation object (`AsmFea.AddIn`), and registration |
| `AsmFea.Tests` | Console test runner for everything that needs no SolidWorks |

## Build and test (no SolidWorks needed)

```powershell
powershell -ExecutionPolicy Bypass -File addin\build.ps1
```

The script compiles with Roslyn `csc` against the .NET 4.8 runtime assemblies and the SolidWorks 2026 interop DLLs from the install folder. It then runs `AsmFea.Tests.exe`, which writes `bin\sample_model.json`. Afterwards, `python\tests\test_addin_output.py` checks that file against the schema and the Python loader.

## Register (administrator, SolidWorks closed)

The build copies SolidWorks' three interop DLLs into `bin\`. SolidWorks does not put them in the GAC, and RegAsm cannot load the add-in without them. Run RegAsm from an *elevated* prompt; it needs HKLM. From a normal shell, `Start-Process cmd -Verb RunAs` raises the UAC prompt.

Registration is a deliberate step and is done in a session with the user present. Building never registers.

```powershell
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\RegAsm.exe /codebase "addin\bin\AsmFea.AddIn.dll"
```

Registration does three things:

- It registers the COM class `AsmFea.AddIn` (CLSID `{77cf8ee6-9213-44db-8379-945fd7578fc2}`).
- It lists the add-in under `HKLM\SOFTWARE\SolidWorks\Addins`.
- It sets it to load at start-up under `HKCU\Software\SolidWorks\AddInsStartup`.

To remove it, run the same command with `/unregister`.

`/codebase` points COM at `addin\bin`, so rebuilding while SolidWorks has the DLL loaded fails. Close SolidWorks before rebuilding a registered add-in, or build elsewhere with `build.ps1 -OutDir <folder>` to test. A rebuild needs no re-registration; the CLSID and types do not change.

## Use

- **Tools > asmfea > Export FEA model:** writes `<Assembly>_FEA\Study1\model.json` and `parts\*.step` beside the active assembly, then shows a summary.
- **Automation:** `ISldWorks.GetAddInObject("AsmFea.AddIn")` returns the object:
  - `Version`
  - `ExportModel(study, folder)`
  - `GetStudyJson(study)`
  - `SetStudyJson(study, json)`
  - `ListStudies()`
  - `HighlightFaces(study, faceRefIdsJson)`

  The Python side is `asmfea.sw.ComSession`, which holds the SolidWorks lock shared with layoutcad.

The extractor never rebuilds, modifies or saves a document. It does two things in memory only:

- It resolves lightweight components.
- It writes STEP files with Save As Copy.

A part whose referenced configuration is not its active one is reported as `STEP_CONFIG_INACTIVE` instead of being switched over.

See `LIVE-TESTS.md` for what must be confirmed in SolidWorks.
