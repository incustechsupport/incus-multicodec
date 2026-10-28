"""Download the Intel and Realtek Bluetooth firmware that ships with the installer (into ./firmware).

    .venv\\Scripts\\python packaging\\fetch_firmware.py

Uses Bumble's downloaders, then adds the files linux-firmware publishes only as symlinks
(e.g. ibt-0040-4150.sfi -> ibt-0040-0041.sfi), which Bumble's Intel downloader skips.
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIRMWARE = ROOT / "firmware"
WHENCE = "https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git/plain/WHENCE"


def run_bumble_tool(module: str, out: pathlib.Path, env: dict | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    # The tools report individual 404s (expected for symlinked files) but exit non-zero on real failures
    result = subprocess.run([sys.executable, "-m", module, "--output-dir", str(out)], env=env, timeout=900)
    if result.returncode:
        raise SystemExit(f"{module} failed (exit {result.returncode})")


def main() -> None:
    run_bumble_tool("bumble.tools.intel_fw_download", FIRMWARE / "intel")

    # bumble 0.0.236's rtk_fw_download does `from tools import rtk_util`; give it that module
    with tempfile.TemporaryDirectory() as shim:
        (pathlib.Path(shim) / "tools").mkdir()
        (pathlib.Path(shim) / "tools" / "__init__.py").write_text("")
        (pathlib.Path(shim) / "tools" / "rtk_util.py").write_text("from bumble.tools.rtk_util import *\n")
        env = dict(os.environ, PYTHONPATH=shim)
        run_bumble_tool("bumble.tools.rtk_fw_download", FIRMWARE / "realtek", env)

    request = urllib.request.Request(WHENCE, headers={"User-Agent": "incus-multicodec"})
    whence = urllib.request.urlopen(request, timeout=60).read().decode()
    added = 0
    for name, target in re.findall(r"^Link: intel/(ibt-[\w-]+\.(?:sfi|ddc)) -> (ibt-[\w-]+\.(?:sfi|ddc))$", whence, re.M):
        source, alias = FIRMWARE / "intel" / target, FIRMWARE / "intel" / name
        if source.is_file() and not alias.exists():
            shutil.copyfile(source, alias)
            added += 1
    print(f"intel: added {added} aliased firmware files")

    # Don't let a silent download failure ship an installer without firmware
    from bumble.drivers import intel
    missing = [f"{base}.sfi" for base in intel.INTEL_FW_IMAGE_NAMES if not (FIRMWARE / "intel" / f"{base}.sfi").is_file()]
    realtek = list((FIRMWARE / "realtek").glob("rtl*_fw.bin"))
    if missing or len(realtek) < 10:
        raise SystemExit(f"firmware incomplete: missing Intel {missing}, {len(realtek)} Realtek images")
    print(f"firmware complete: {len(intel.INTEL_FW_IMAGE_NAMES)} Intel images, {len(realtek)} Realtek images")


if __name__ == "__main__":
    main()
