"""Copy the licence texts of everything the PyInstaller bundle ships into licenses/ (run before a release):

    .venv\\Scripts\\python packaging\\collect_licenses.py

- licenses/python/<package>-<version>/: the licence files of every Python package installed in the venv,
  except the build tools that never end up in the bundle.
- licenses/python-runtime/: Python's own licence (it covers the OpenSSL, libffi, bzip2... DLLs that come
  with it) and Tcl/Tk's (the devices window uses tkinter).

Native components (codecs, libwdi, libusb, PortAudio, the WDK co-installers, firmware) have their licence
files in licenses/ by hand, as THIRD_PARTY_NOTICES.md lists them.
"""

from __future__ import annotations

import importlib.metadata as md
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "licenses"
# Build-time only: PyInstaller and its helpers, and the venv's own installers
BUILD_ONLY = {"pip", "setuptools", "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile",
              "pywin32-ctypes", "packaging"}
MARKERS = ("LICEN", "COPYING", "NOTICE", "AUTHORS")


def main() -> None:
    python_dir = OUT / "python"
    shutil.rmtree(python_dir, ignore_errors=True)
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if name.lower() in BUILD_ONLY:
            continue
        files = [f for f in (dist.files or []) if any(m in f.name.upper() for m in MARKERS)
                 and ".dist-info" in str(f)]
        dest = python_dir / f"{name}-{dist.version}"
        dest.mkdir(parents=True)
        if not files:
            # pyserial ships no licence file; its metadata names the licence
            (dest / "LICENSE-from-metadata.txt").write_text(
                f"{name} {dist.version}: {dist.metadata.get('License') or 'see the project'}\n"
                f"{dist.metadata.get('Home-page') or ''}\n", encoding="utf-8")
        for f in files:
            # Keep sub-paths for packages with several licence files (numpy's vendored libraries)
            relative = pathlib.PurePosixPath(str(f)).relative_to(pathlib.PurePosixPath(str(f)).parts[0])
            target = dest / pathlib.Path(*relative.parts[1:] if relative.parts[0] == "licenses" else relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dist.locate_file(f), target)
    runtime = OUT / "python-runtime"
    runtime.mkdir(exist_ok=True)
    base = pathlib.Path(sys.base_prefix)
    shutil.copyfile(base / "LICENSE.txt", runtime / "Python-LICENSE.txt")
    # Tcl and Tk share one licence text; Python's Windows install keeps only Tk's copy
    shutil.copyfile(base / "tcl" / "tk8.6" / "license.terms", runtime / "Tcl-Tk-license.terms")
    print(f"licence texts for {len(list(python_dir.iterdir()))} Python packages in {python_dir}")


if __name__ == "__main__":
    main()
