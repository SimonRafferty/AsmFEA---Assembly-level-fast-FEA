"""Build the AsmFEA installer (installer\\Output\\AsmFEA-Setup-<version>.exe).

Run from the repo root:  .venv\\Scripts\\python installer\\build.py

What goes INTO the installer (build\\app):
  python\\   the python.org embeddable Python 3.13 with AsmFEA and its libraries installed into
            Lib\\site-packages (from wheels; byte-compiled, because Program Files is read-only)
  addin\\    AsmFea.AddIn.dll, AsmFea.Extract.dll (the SolidWorks interop DLLs are NOT shipped:
            Setup copies them from the user's SolidWorks 2026 installation)
  licenses\\ third-party notices; Getting started.html; AsmFEA.ico

What Setup DOWNLOADS on the user's machine (AsmFEA.iss, checksums pinned there):
  CalculiX 2.22 for Windows (dhondt.de), Intel MKL + OpenMP runtimes (PyPI wheels), Gmsh 4.15.2
  (gmsh.info). They are not packaged (user decision 2026-10-03, DECISIONS D24).

Needs: Inno Setup 6 (winget install JRSoftware.InnoSetup), internet for the first build.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CACHE = HERE / "cache"
BUILD = HERE / "build" / "app"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
ISCC = [Path.home() / "AppData/Local/Programs/Inno Setup 6/ISCC.exe",
        Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe")]

PY_URL = "https://www.python.org/ftp/python/3.13.5/python-3.13.5-embed-amd64.zip"
PY_SHA256 = "7d2650fd9d1b9d002d4a315d5f354247fd6a44f30517c7ef577b08f57a0fb6d9"


def fetch(url: str, sha256: str) -> Path:
    """Download once into cache/ and check the checksum every time."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / url.rsplit("/", 1)[1]
    if not path.exists():
        print("downloading", url)
        urllib.request.urlretrieve(url, path)
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != sha256:
        path.unlink()
        raise SystemExit(f"checksum mismatch for {url}: {got}")
    return path


def run(*cmd, **kw):
    print(">", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def version() -> str:
    import tomllib
    return tomllib.loads((ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def python_runtime() -> None:
    py = BUILD / "python"
    with zipfile.ZipFile(fetch(PY_URL, PY_SHA256)) as z:
        z.extractall(py)
    # the embeddable Python ignores site-packages unless its ._pth file names it
    pth = next(py.glob("python3*._pth"))
    pth.write_text("\n".join([next(py.glob("python3*.zip")).name, ".", r"Lib\site-packages", "import site", ""]))
    # AsmFEA as a wheel, then it and its dependencies from wheels only, into the runtime
    wheels = HERE / "build" / "wheels"
    shutil.rmtree(wheels, ignore_errors=True)
    run(VENV_PY, "-m", "pip", "wheel", "--no-deps", "-w", wheels, ROOT / "python")
    wheel = next(wheels.glob("asmfea-*.whl"))
    # the same library versions as the tested development environment (.venv), not whatever is newest
    frozen = subprocess.run([str(VENV_PY), "-m", "pip", "freeze", "--exclude-editable"], check=True,
                            capture_output=True, text=True).stdout
    constraints = HERE / "build" / "constraints.txt"
    pins = [line for line in frozen.splitlines() if line and not line.lower().startswith("asmfea")]
    constraints.write_text("\n".join(pins) + "\n")
    run(VENV_PY, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
        "--only-binary=:all:", "-c", constraints, "--target", py / "Lib" / "site-packages", f"{wheel}[report]")
    # pypardiso for the hybrid solver (D49), without its MKL wheels: at run time it uses the MKL runtime the
    # installer unpacks beside CalculiX (hybrid.solve._pardiso), so the download does not grow by 600 MB
    run(VENV_PY, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
        "--only-binary=:all:", "--no-deps", "-c", constraints, "--target", py / "Lib" / "site-packages", "pypardiso")
    # numba compiles the hybrid solver's element stiffness loop (2026-10-09; optional at run time, it falls back to
    # NumPy). Its compiled-code cache goes to the user's own folder, as Program Files is read-only
    run(VENV_PY, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
        "--only-binary=:all:", "--no-deps", "-c", constraints, "--target", py / "Lib" / "site-packages",
        "numba", "llvmlite")
    shutil.rmtree(py / "Lib" / "site-packages" / "bin", ignore_errors=True)    # console scripts: unused
    run(py / "python.exe", "-m", "compileall", "-q", "-j0", py / "Lib" / "site-packages")
    # smoke test with the bundled runtime alone
    run(py / "python.exe", "-c", "import asmfea, asmfea.pipeline, OCP, pyvista, docx, scipy, numba; "
        "from asmfea.post import viewer; from asmfea.hybrid.elements import _make_kernel; "
        "assert _make_kernel() is not None; print('bundled runtime OK', asmfea.__name__)")


def addin() -> None:
    out = BUILD / "addin"
    run("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ROOT / "addin" / "build.ps1",
        "-NoTests", "-OutDir", out)
    for p in out.iterdir():   # keep only our two DLLs: Setup copies the interop DLLs from SolidWorks
        if p.name not in ("AsmFea.AddIn.dll", "AsmFea.Extract.dll"):
            p.unlink()


def guide(out: Path) -> None:
    """The getting-started guide as one file: its guide_images/* pictures embedded as data URIs."""
    import base64
    import re
    html = (HERE / "Getting started.html").read_text(encoding="utf-8")

    def embed(m):
        img = HERE / "guide_images" / m.group(1)
        if not img.exists():
            raise SystemExit(f"guide image missing: {img}")
        return 'src="data:image/png;base64,' + base64.b64encode(img.read_bytes()).decode("ascii") + '"'
    html = re.sub(r'src="guide_images/([^"]+\.png)"', embed, html)
    if 'src="guide_images/' in html:
        raise SystemExit("the guide refers to a guide image that was not embedded")
    out.write_text(html, encoding="utf-8")


def icon() -> Path:
    from PIL import Image
    ico = HERE / "build" / "AsmFEA.ico"
    Image.open(ROOT / "addin" / "AsmFea.AddIn" / "AsmFEA_Icon.png").convert("RGBA").save(
        ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ico


def main() -> None:
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    python_runtime()
    addin()
    shutil.copytree(HERE / "licenses", BUILD / "licenses")
    guide(BUILD / "Getting started.html")
    # and the guide as a PDF, printed from the page as built (images embedded)
    from asmfea.verify.html import print_pdf
    if print_pdf(BUILD / "Getting started.html", BUILD / "AsmFEA quick start guide.pdf") is None:
        raise SystemExit("could not print the quick start guide to PDF (Microsoft Edge not found)")
    # the verification report (asmfea verify cases/verification), as a web page and a PDF copy
    for ext in (".html", ".pdf"):
        src = ROOT / "cases" / "verification" / f"AsmFEA_Verification_Report{ext}"
        if not src.exists():
            raise SystemExit(f"missing {src}: run  asmfea verify cases/verification  first")
        shutil.copy2(src, BUILD / f"AsmFEA verification report{ext}")
    shutil.copy2(icon(), BUILD / "AsmFEA.ico")
    iscc = next((p for p in ISCC if p.exists()), None)
    if iscc is None:
        raise SystemExit("Inno Setup 6 not found: winget install JRSoftware.InnoSetup")
    run(iscc, f"/DAppVersion={version()}", HERE / "AsmFEA.iss")
    print("built", next((HERE / "Output").glob(f"AsmFEA-Setup-{version()}.exe")))


if __name__ == "__main__":
    sys.exit(main())
