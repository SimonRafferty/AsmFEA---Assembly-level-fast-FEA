; AsmFEA installer (Inno Setup 6.1+). Built by installer\build.py, which fills build\app first.
;
; Installs the bundled Python runtime with AsmFEA, and the SolidWorks add-in. Downloads CalculiX,
; the Intel MKL runtime it needs for the PARDISO solver, and Gmsh from their official sources
; (checksums pinned below: a changed file is refused). Writes {app}\asmfea.toml and registers the
; add-in (RegAsm /codebase). The add-in embeds the SolidWorks API types it uses (compiled against
; SOLIDWORKS 2018), so one copy serves SOLIDWORKS 2018 and later; the user ticks the versions it should
; load in (HKLM\SOFTWARE\AsmFEA, SolidWorksVersions; it stays dormant in the others, D26).
; Uninstall unregisters it and removes everything it put there.
;
; Test switch: /NOSOLIDWORKS skips the SolidWorks check, the version choice and the registration
; (for checking the download and the runtime on a machine without SolidWorks).

#ifndef AppVersion
  #define AppVersion "0.6.0"
#endif

[Setup]
AppId={{6A5A626F-B5F4-4774-8C4C-1607590D7CA4}
AppName=AsmFEA
AppVersion={#AppVersion}
AppVerName=AsmFEA {#AppVersion}
AppPublisher=AsmFEA
DefaultDirName={autopf}\AsmFEA
DefaultGroupName=AsmFEA
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=Output
OutputBaseFilename=AsmFEA-Setup-{#AppVersion}
SetupIconFile=build\AsmFEA.ico
UninstallDisplayIcon={app}\AsmFEA.ico
UninstallDisplayName=AsmFEA (assembly FEA for SOLIDWORKS)
Compression=lzma2/ultra64
SolidCompression=yes
LZMANumBlockThreads=4
LZMAUseSeparateProcess=yes
WizardStyle=modern
InfoBeforeFile=before-install.txt
CloseApplications=no
SetupLogging=yes

[Files]
Source: "build\app\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\AsmFEA - Getting started"; Filename: "{app}\Getting started.html"
Name: "{group}\AsmFEA - Quick start guide (PDF)"; Filename: "{app}\AsmFEA quick start guide.pdf"
Name: "{group}\AsmFEA - Verification report"; Filename: "{app}\AsmFEA verification report.html"
Name: "{group}\AsmFEA - Verification report (PDF)"; Filename: "{app}\AsmFEA verification report.pdf"
Name: "{group}\AsmFEA folder"; Filename: "{app}"
Name: "{group}\Uninstall AsmFEA"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\Getting started.html"; Description: "Open the getting-started guide"; Flags: postinstall shellexec skipifsilent nowait
Filename: "{app}\AsmFEA verification report.html"; Description: "Open the verification report (AsmFEA against hand calculations)"; Flags: postinstall shellexec skipifsilent nowait

[UninstallDelete]
Type: filesandordirs; Name: "{app}\solver"
Type: filesandordirs; Name: "{app}\gmsh"
Type: filesandordirs; Name: "{app}\addin"
Type: files; Name: "{app}\asmfea.toml"
Type: filesandordirs; Name: "{app}\python"
; the Intel licence texts Setup unpacked from the downloads, then the folder itself
Type: filesandordirs; Name: "{app}\licenses"
Type: dirifempty; Name: "{app}"

[Code]
const
  MIN_YEAR = 2018;          { the add-in is compiled against the SOLIDWORKS 2018 API }
  MAX_YEAR = 2040;
  ASMFEA_KEY = 'SOFTWARE\AsmFEA';
  CCX_URL = 'http://www.dhondt.de/calculix_2.22_4win.zip';
  CCX_SHA = 'a1f91281944c96d6cd914cc020421e8ae65973b3e15d055dc63a3e3e3066d281';
  MKL_URL = 'https://files.pythonhosted.org/packages/8e/80/ad5fb0f2c9880c3d2b8df3ac508021cf5eb3a6909b250250430e66557bbd/mkl-2022.2.1-py2.py3-none-win_amd64.whl';
  MKL_SHA = '67f6ed435c2fd6b526d0d360259fa2be9453365ecde75f589db61c3624ef1cd8';
  OMP_URL = 'https://files.pythonhosted.org/packages/a0/39/9be1803f45c779ef2df43920fe24140915b992a1e42bf9a37e3beec1ee48/intel_openmp-2022.2.1-py2.py3-none-win_amd64.whl';
  OMP_SHA = '77414289c14cb48d7f99926da69c9ced9e70c27feb825b0608f304f9d49844ae';
  GMSH_URL = 'https://gmsh.info/bin/Windows/gmsh-4.15.2-Windows64.zip';
  GMSH_SHA = 'a8208b56aedee3434907a893e303422acacd9003140766e09f2abbc664ba7157';
  MKL_BIN = 'mkl-2022.2.1.data/data/Library/bin/';

var
  DownloadPage: TDownloadWizardPage;
  VersionPage: TInputOptionWizardPage;
  SWYears: array of Integer;        { installed SOLIDWORKS versions, newest first }

{ Installed SOLIDWORKS versions: a registry entry whose folder holds an SLDWORKS.exe of THAT version
  (upgrades leave stale entries pointing at another version's folder) }
function DetectSolidWorks: Integer;
var
  y, major: Integer;
  dir, ver: String;
begin
  SetArrayLength(SWYears, 0);
  for y := MAX_YEAR downto MIN_YEAR do
    if RegQueryStringValue(HKLM64, 'SOFTWARE\SolidWorks\SOLIDWORKS ' + IntToStr(y) + '\Setup', 'SolidWorks Folder', dir) and
       GetVersionNumbersString(AddBackslash(dir) + 'SLDWORKS.exe', ver) then
    begin
      major := StrToIntDef(Copy(ver, 1, Pos('.', ver) - 1), 0);
      if major + 1992 = y then
      begin
        SetArrayLength(SWYears, GetArrayLength(SWYears) + 1);
        SWYears[GetArrayLength(SWYears) - 1] := y;
      end;
    end;
  Result := GetArrayLength(SWYears);
end;

{ The ticked versions as "2026,2025" }
function ChosenVersions: String;
var
  i: Integer;
begin
  Result := '';
  for i := 0 to GetArrayLength(SWYears) - 1 do
    if (GetArrayLength(SWYears) = 1) or VersionPage.Values[i] then
    begin
      if Result <> '' then Result := Result + ',';
      Result := Result + IntToStr(SWYears[i]);
    end;
end;

function NoSolidWorks: Boolean;
begin
  Result := ExpandConstant('{param:NOSOLIDWORKS|0}') <> '0';
end;

function SolidWorksRunning: Boolean;
var
  rc: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq SLDWORKS.exe" | find /I "SLDWORKS.exe"',
                 '', SW_HIDE, ewWaitUntilTerminated, rc) and (rc = 0);
end;

function InitializeSetup: Boolean;
begin
  Result := False;
  if not FileExists(ExpandConstant('{sys}\tar.exe')) then
  begin
    MsgBox('This installer needs Windows 10 (version 1803 or later) or Windows 11.', mbError, MB_OK);
    Exit;
  end;
  if (not NoSolidWorks) and (DetectSolidWorks = 0) then
  begin
    MsgBox('AsmFEA is an add-in for SOLIDWORKS 2018 or later, and no such SOLIDWORKS was found on this computer.' + #13#10#13#10 +
           'Install SOLIDWORKS first, then run this installer again.', mbError, MB_OK);
    Exit;
  end;
  Result := True;
end;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  Result := True;
end;

procedure InitializeWizard;
var
  i: Integer;
  prev: String;
begin
  VersionPage := CreateInputOptionPage(wpSelectDir, 'SOLIDWORKS versions',
    'Which SOLIDWORKS should AsmFEA be added to?',
    'AsmFEA will load only in the versions ticked below. To change this later, run this installer again.',
    False, False);
  { the previous choice when re-running the installer, else every version }
  if not RegQueryStringValue(HKLM64, ASMFEA_KEY, 'SolidWorksVersions', prev) then prev := '';
  for i := 0 to GetArrayLength(SWYears) - 1 do
  begin
    VersionPage.Add('SOLIDWORKS ' + IntToStr(SWYears[i]));
    VersionPage.Values[i] := (prev = '') or (Pos(IntToStr(SWYears[i]), prev) > 0);
  end;
  DownloadPage := CreateDownloadPage('Downloading the solver and mesher',
    'CalculiX, the Intel MKL runtime and Gmsh come from their official sites (about 310 MB).',
    @OnDownloadProgress);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { nothing to choose with one version (or none, in the /NOSOLIDWORKS test) }
  Result := (PageID = VersionPage.ID) and (NoSolidWorks or (GetArrayLength(SWYears) <= 1));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = VersionPage.ID) and (ChosenVersions = '') then
  begin
    MsgBox('Tick at least one SOLIDWORKS version.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  { the deepest installed file is about 130 characters below the folder; Windows refuses to load
    a DLL whose path passes 260 (found while testing: "The filename or extension is too long") }
  if (CurPageID = wpSelectDir) and (Length(WizardDirValue) > 100) then
  begin
    MsgBox('Please choose a shorter folder (at most 100 characters), for example C:\Program Files\AsmFEA.',
           mbError, MB_OK);
    Result := False;
    Exit;
  end;
  if CurPageID <> wpReady then Exit;
  while (not NoSolidWorks) and SolidWorksRunning do
    if MsgBox('Please close SOLIDWORKS before installing (save your work first).' + #13#10 +
              'Click Retry when it is closed.', mbInformation, MB_RETRYCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  DownloadPage.Clear;
  DownloadPage.Add(CCX_URL, 'calculix.zip', CCX_SHA);
  DownloadPage.Add(MKL_URL, 'mkl.whl', MKL_SHA);
  DownloadPage.Add(OMP_URL, 'omp.whl', OMP_SHA);
  DownloadPage.Add(GMSH_URL, 'gmsh.zip', GMSH_SHA);
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
    except
      if DownloadPage.AbortedByUser then
        Log('Download aborted by the user.')
      else
        SuppressibleMsgBox('A download failed:' + #13#10 + GetExceptionMessage + #13#10#13#10 +
                           'Check the internet connection and try again.', mbCriticalError, MB_OK, IDOK);
      Result := False;
    end;
  finally
    DownloadPage.Hide;
  end;
end;

{ tar (bsdtar, part of Windows 10/11) reads zip files and wheels }
procedure Untar(const Archive, DestDir: String; Strip: Integer; const Members: String);
var
  rc: Integer;
begin
  ForceDirectories(DestDir);
  if not Exec(ExpandConstant('{sys}\tar.exe'),
              Format('-xf "%s" -C "%s" --strip-components %d %s', [Archive, DestDir, Strip, Members]),
              '', SW_HIDE, ewWaitUntilTerminated, rc) or (rc <> 0) then
    RaiseException(Format('Could not unpack %s (tar exit code %d).', [ExtractFileName(Archive), rc]));
end;

procedure Need(const Path: String);
begin
  if not FileExists(Path) then RaiseException('Missing after unpacking: ' + Path);
end;

function TomlPath(const Path: String): String;
begin
  Result := '''' + Path + '''';    { TOML literal string: backslashes kept as they are }
end;

procedure InstallDependencies;
var
  app, solver, gmsh, lic: String;
  threads: Integer;
  toml: TArrayOfString;
begin
  app := ExpandConstant('{app}');
  solver := app + '\solver';
  gmsh := app + '\gmsh';
  lic := app + '\licenses';
  WizardForm.StatusLabel.Caption := 'Unpacking CalculiX...';
  Untar(ExpandConstant('{tmp}\calculix.zip'), solver, 1, 'calculix_2.22_4win/ccx_dynamic.exe calculix_2.22_4win/README_Install');
  WizardForm.StatusLabel.Caption := 'Unpacking the Intel MKL runtime...';
  Untar(ExpandConstant('{tmp}\mkl.whl'), solver, 4,
        MKL_BIN + 'mkl_rt.2.dll ' + MKL_BIN + 'mkl_core.2.dll ' + MKL_BIN + 'mkl_intel_thread.2.dll ' + MKL_BIN + 'mkl_def.2.dll');
  Untar(ExpandConstant('{tmp}\omp.whl'), solver, 4, 'intel_openmp-2022.2.1.data/data/Library/bin/libiomp5md.dll');
  Untar(ExpandConstant('{tmp}\mkl.whl'), lic + '\Intel MKL', 1, 'mkl-2022.2.1.dist-info/LICENSE.txt');
  Untar(ExpandConstant('{tmp}\omp.whl'), lic + '\Intel OpenMP', 1, 'intel_openmp-2022.2.1.dist-info/LICENSE.txt');
  WizardForm.StatusLabel.Caption := 'Unpacking Gmsh...';
  Untar(ExpandConstant('{tmp}\gmsh.zip'), gmsh, 1, 'gmsh-4.15.2-Windows64/gmsh.exe gmsh-4.15.2-Windows64/LICENSE.txt gmsh-4.15.2-Windows64/README.txt');
  Need(solver + '\ccx_dynamic.exe');
  Need(solver + '\mkl_rt.2.dll');
  Need(solver + '\libiomp5md.dll');
  Need(gmsh + '\gmsh.exe');

  { solver threads: the physical cores (about half the logical ones), at most 8 (DECISIONS D3) }
  threads := StrToIntDef(GetEnv('NUMBER_OF_PROCESSORS'), 2) div 2;
  if threads < 1 then threads := 1;
  if threads > 8 then threads := 8;
  SetArrayLength(toml, 8);
  toml[0] := '# Written by the AsmFEA installer: where the solver and mesher are.';
  toml[1] := '[solver]';
  toml[2] := 'ccx = ' + TomlPath(solver + '\ccx_dynamic.exe');
  toml[3] := 'threads = ' + IntToStr(threads);
  toml[4] := '';
  toml[5] := '[mesh]';
  toml[6] := 'gmsh = ' + TomlPath(gmsh + '\gmsh.exe');
  toml[7] := '';
  if not SaveStringsToUTF8FileWithoutBOM(app + '\asmfea.toml', toml, False) then
    RaiseException('Could not write ' + app + '\asmfea.toml');
end;

procedure InstallAddin;
var
  app, dll: String;
  rc: Integer;
begin
  app := ExpandConstant('{app}');
  dll := app + '\addin\AsmFea.AddIn.dll';
  { the versions the add-in may load in; it stays dormant in the others }
  if not RegWriteStringValue(HKLM64, ASMFEA_KEY, 'SolidWorksVersions', ChosenVersions) then
    RaiseException('Could not record the chosen SOLIDWORKS versions');
  WizardForm.StatusLabel.Caption := 'Registering the SOLIDWORKS add-in...';
  if not Exec(ExpandConstant('{dotnet4064}\RegAsm.exe'), '/codebase "' + dll + '"', '', SW_HIDE,
              ewWaitUntilTerminated, rc) or (rc <> 0) then
    RaiseException(Format('Registering the add-in failed (RegAsm exit code %d).', [rc]));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then Exit;
  try
    InstallDependencies;
    if not NoSolidWorks then InstallAddin;
  except
    MsgBox('AsmFEA was not installed completely:' + #13#10 + GetExceptionMessage + #13#10#13#10 +
           'Run the installer again; if it fails again, send the file ' + ExpandConstant('{log}') +
           ' to whoever gave you AsmFEA.', mbCriticalError, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  dll: String;
  rc: Integer;
begin
  if CurUninstallStep <> usUninstall then Exit;
  dll := ExpandConstant('{app}\addin\AsmFea.AddIn.dll');
  if FileExists(dll) then
    Exec(ExpandConstant('{dotnet4064}\RegAsm.exe'), '/u "' + dll + '"', '', SW_HIDE, ewWaitUntilTerminated, rc);
  RegDeleteKeyIncludingSubkeys(HKLM64, ASMFEA_KEY);
end;
