# Build the asmfea add-in and run its tests. No registration, no SolidWorks needed.
#
#   powershell -ExecutionPolicy Bypass -File addin\build.ps1 [-NoTests]
#
# Compiles with the Roslyn csc against the .NET Framework 4.8 runtime assemblies (no targeting
# pack or NuGet on this machine; DECISIONS D16) and the SolidWorks 2026 interop assemblies
# from a SolidWorks install (SolidWorks' files: their types are embedded at compile time, never copied).
# Output: addin\bin\. Registering for SolidWorks is a separate, deliberate step (addin\README.md).
param([switch]$NoTests, [string]$OutDir, [string]$SwApi = '26')   # -OutDir: build elsewhere while SolidWorks holds bin\ locked; -SwApi: interop major version to compile against (26 = SolidWorks 2018)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bin = if ($OutDir) { $OutDir } else { Join-Path $here 'bin' }
New-Item -ItemType Directory -Force $bin | Out-Null

# --- tools -------------------------------------------------------------------
$csc = @(
    "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\Roslyn\csc.exe",
    "${env:ProgramFiles}\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\Roslyn\csc.exe",
    "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\Roslyn\csc.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $csc) { throw 'Roslyn csc.exe not found (install Visual Studio Build Tools)' }
$fw = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319"

# The SolidWorks interop types are EMBEDDED (/link, "no PIA"): only the API types we use are compiled into our
# DLLs, so the add-in runs in any SolidWorks from the version it is compiled against onwards, with no interop
# DLL beside it. It is compiled against the OLDEST supported version's API (SolidWorks 2018, interop 26.x), so
# a call newer than that fails here, not in a user's SolidWorks (DECISIONS D26). -SwApi picks another version.
$sw = Get-ChildItem "$env:ProgramFiles\SOLIDWORKS Corp" -Directory |
    Where-Object { (Test-Path "$($_.FullName)\SolidWorks.Interop.sldworks.dll") -and
                   ((Get-Item "$($_.FullName)\SolidWorks.Interop.sldworks.dll").VersionInfo.FileVersion -like "$SwApi.*") } |
    Select-Object -First 1
if (-not $sw) { throw "SolidWorks interop assemblies of version $SwApi.x (SolidWorks $([int]$SwApi + 1992)) not found" }
$swdir = $sw.FullName
Write-Host "SolidWorks API: $swdir (interop $((Get-Item "$swdir\SolidWorks.Interop.sldworks.dll").VersionInfo.FileVersion), embedded)"
$swrefs = @('sldworks', 'swconst', 'swpublished') | ForEach-Object { "/link:$swdir\SolidWorks.Interop.$_.dll" }
# Microsoft.CSharp: with embedded interop types, API calls returning object come back as dynamic
$fwrefs = @('System.dll', 'System.Core.dll', 'System.Xml.dll', 'Microsoft.CSharp.dll') | ForEach-Object { "/reference:$fw\$_" }
$common = @('/nologo', '/noconfig', '/nostdlib+', "/reference:$fw\mscorlib.dll", '/langversion:7.3', '/platform:x64',
            '/optimize+', '/debug:pdbonly', '/warnaserror+', '/nowarn:1701,1702') + $fwrefs

function Build([string]$name, [string]$target, [string[]]$extra) {
    $src = @(Get-ChildItem (Join-Path $here $name) -Filter *.cs -Recurse | ForEach-Object { $_.FullName })
    $ext = if ($target -eq 'exe') { 'exe' } else { 'dll' }
    $out = Join-Path $bin "$name.$ext"
    & $csc @common "/target:$target" "/out:$out" @extra @src
    if ($LASTEXITCODE -ne 0) { throw "build of $name failed" }
    Write-Host "built $out"
}

Build 'AsmFea.Extract' 'library' $swrefs
# the task-pane icon (AsmFEA_Icon.png) is embedded, so no image file ships beside the DLL
Build 'AsmFea.AddIn' 'library' ($swrefs + "/reference:$bin\AsmFea.Extract.dll" + "/reference:$fw\System.Windows.Forms.dll" + "/reference:$fw\System.Drawing.dll" +
                                "/resource:$(Join-Path $here 'AsmFea.AddIn\AsmFEA_Icon.png'),AsmFea.AddIn.Icon.png")
Build 'AsmFea.Tests' 'exe' ($swrefs + "/reference:$bin\AsmFea.Extract.dll")

# No interop DLLs are copied: their types are embedded in our assemblies (/link above).

if ($NoTests) { return }
$sample = Join-Path $bin 'sample_model.json'
& (Join-Path $bin 'AsmFea.Tests.exe') $sample
if ($LASTEXITCODE -ne 0) { throw "$LASTEXITCODE add-in test(s) failed" }
