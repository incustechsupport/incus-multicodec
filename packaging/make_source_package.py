"""Build dist\\incus-multicodec-<version>-third-party-sources.zip, attached to every GitHub release:

    .venv\\Scripts\\python packaging\\make_source_package.py

The LGPL licences ask that whoever ships their libraries also offers the source of exactly what was
shipped. This collects it: the third_party checkouts the native DLLs were built from (as `git archive`
of the commit built, plus the build configuration we apply), libusb's release source, the source
distributions of the LGPL Python packages, and our build scripts. Incus Multicodec's own source is
the GitHub repository itself.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from btmulticodec import __version__  # noqa: E402

CACHE = ROOT / "third_party" / "downloads"
# Built into the shipped binaries from these checkouts
CHECKOUTS = {
    "libfreeaptx": "aptx.dll (LGPL 2.1 or later)",
    "sbc": "sbc.dll (LGPL 2.1 or later)",
    "libwdi": "wdi-simple.exe (LGPL 3.0); built with native/libwdi_config.h as msvc/config.h",
    "libldac": "ldac.dll (Apache 2.0; included for completeness)",
}
DOWNLOADS = {
    # libusb-1.0.dll comes from the libusb-package wheel, which builds this release unmodified
    "libusb-1.0.26.tar.bz2": "https://github.com/libusb/libusb/releases/download/v1.0.26/libusb-1.0.26.tar.bz2",
    "pystray-0.19.5.tar.gz": "https://github.com/moses-palmer/pystray/archive/refs/tags/v0.19.5.tar.gz",
    "libusb1-3.4.0.tar.gz": "https://files.pythonhosted.org/packages/source/l/libusb1/libusb1-3.4.0.tar.gz",
}
BUILD_FILES = ["native/build.cmd", "native/build-wdi.cmd", "native/aptx.def", "native/sbc.def",
               "native/libwdi_config.h", "native/compat/msvc_compat.h", "native/compat/sbc_compat.h",
               "native/compat/unistd.h", "requirements.txt", "THIRD_PARTY_NOTICES.md"]


def fetch(name: str, url: str) -> bytes:
    path = CACHE / name
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": "incus-multicodec-build"})
        path.write_bytes(urllib.request.urlopen(request, timeout=120).read())
    return path.read_bytes()


def main() -> None:
    out = ROOT / "dist" / f"incus-multicodec-{__version__}-third-party-sources.zip"
    out.parent.mkdir(exist_ok=True)
    readme = [f"Incus Multicodec {__version__}: source code of the third-party components it ships",
              "", "Incus Multicodec's own source: https://github.com/incustechsupport/incus-multicodec",
              "Licences and notices: THIRD_PARTY_NOTICES.md", "", "Contents:"]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, what in CHECKOUTS.items():
            repo = ROOT / "third_party" / name
            commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                                    check=True).stdout.strip()
            url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True,
                                 text=True).stdout.strip()
            archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=zip", f"--prefix={name}/", "HEAD"],
                                     capture_output=True, check=True).stdout
            z.writestr(f"{name}-{commit[:7]}.zip", archive)
            readme.append(f"- {name}-{commit[:7]}.zip: {url} at {commit}, for {what}")
        for name, url in DOWNLOADS.items():
            z.writestr(name, fetch(name, url))
            readme.append(f"- {name}: {url}")
        for rel in BUILD_FILES:
            z.write(ROOT / rel, f"build/{rel}")
        readme += ["- build/: the scripts and configuration the DLLs and wdi-simple.exe were built with",
                   "  (see the Build section of the repository's README.md)"]
        z.writestr("README.txt", "\r\n".join(readme) + "\r\n")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
