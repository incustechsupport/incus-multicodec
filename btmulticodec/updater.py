"""Updates: a manifest on software.incustech.app, installers on GitHub Releases.

The site publishes the manifest:
    {"version": "0.1.3", "url": "https://.../incus-multicodec-setup-0.1.3.exe",
     "sha256": "...", "size": 43039811}
A newer version is downloaded to %TEMP%, checked against the manifest's SHA-256, and run silently
with /update=1, which makes the installer start the app again when it's done. The manifest comes
over HTTPS from our server and pins the installer's SHA-256, so the download host (GitHub, which
redirects to its file servers) can only deliver exactly that file.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import msvcrt
import os
import re
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass

from . import __version__

logger = logging.getLogger(__name__)

MANIFEST_URL = "https://software.incustech.app/multicodec/latest.json"
# Where installers may come from: GitHub Releases, and our server (whose /downloads/ only redirects to
# GitHub; 0.1.4-0.1.5 accept nothing else, so their manifest still names it)
DOWNLOAD_HOSTS = ("https://github.com/incustechsupport/incus-multicodec/releases/download/",
                  "https://software.incustech.app/")
USER_AGENT = f"incus-multicodec/{__version__} (updater)"


@dataclass
class Release:
    version: str
    url: str
    sha256: str
    size: int


def parse_version(text: str) -> tuple[int, ...]:
    """'0.2.0' -> (0, 2, 0, 0, 1). A pre-release ('0.2.0-rc1') sorts before its release: the last
    element is 0 for pre-releases and 1 for releases, and rc numbers don't count as a fourth part."""
    # The release part is the leading digits and dots; anything after it ("-rc1", "rc1", "b2") is a
    # pre-release tag
    match = re.match(r"[vV]?([0-9.]*)(.*)", text.strip())
    core, pre = match.group(1), match.group(2).strip()
    numbers = [int(part) for part in core.split(".") if part.isdigit()][:4]
    numbers += [0] * (4 - len(numbers))
    return tuple(numbers) + (0 if pre else 1,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def _get(url: str, timeout: float):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)


def latest(timeout: float = 15) -> Release:
    """The release the server offers now. Raises on network or format errors."""
    with _get(MANIFEST_URL, timeout) as response:
        data = json.loads(response.read(64 * 1024))
    release = Release(str(data["version"]), str(data["url"]), str(data["sha256"]).lower(), int(data["size"]))
    if not release.url.startswith(DOWNLOAD_HOSTS) or not re.fullmatch(r"[0-9a-f]{64}", release.sha256):
        raise ValueError("the update manifest doesn't look right")
    return release


def check() -> Release | None:
    """The newer release, or None when this is the latest."""
    release = latest()
    return release if is_newer(release.version) else None


def _folder() -> str:
    return os.path.join(tempfile.gettempdir(), "incus-multicodec-update")


def _clean(folder: str, keep: str | None = None) -> None:
    """Delete earlier downloads (each is ~40 MB and was never removed before)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        path = os.path.join(folder, name)
        if path != keep and name.startswith("incus-multicodec-setup-"):
            try:
                os.remove(path)
            except OSError:
                pass  # still running, or locked: next time


def download(release: Release, progress=None) -> str:
    """Download and verify the installer; returns its path. progress(done_bytes, total_bytes) is optional.

    run_installer() checks the file again, under a lock, right before starting it."""
    folder = _folder()
    os.makedirs(folder, exist_ok=True)
    _clean(folder)
    path = os.path.join(folder, f"incus-multicodec-setup-{release.version}.exe")
    digest = hashlib.sha256()
    done = 0
    try:
        with _get(release.url, 60) as response, open(path + ".part", "wb") as out:
            while chunk := response.read(256 * 1024):
                done += len(chunk)
                if done > release.size:
                    raise ValueError("the download is bigger than the published installer; nothing was installed")
                out.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(done, release.size)
        if done != release.size or digest.hexdigest() != release.sha256:
            raise ValueError("the downloaded installer didn't match the published checksum; nothing was installed")
    except BaseException:
        try:
            os.remove(path + ".part")
        except OSError:
            pass
        raise
    os.replace(path + ".part", path)
    return path


def _open_locked(path: str) -> int:
    """Open `path` for reading so nobody can change, replace or delete it while the handle is open
    (share mode read-only, no FILE_SHARE_WRITE/DELETE). Returns a CRT file descriptor."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING = 0x80000000, 0x1, 3
    handle = kernel32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, 0, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError(ctypes.get_last_error(), f"couldn't open {path}")
    return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)


def run_installer(path: str, release: Release) -> None:
    """Check the installer again and start it detached, holding a lock on the file in between, so it
    can't be swapped after the check (the app may be running as administrator, and %TEMP% is open to
    the user's other programs). The caller must exit straight away so its files can be replaced."""
    from . import elevation

    def verified(fd: int) -> bytes:
        data = bytearray()
        while chunk := os.read(fd, 1024 * 1024):
            data += chunk
        if len(data) != release.size or hashlib.sha256(data).hexdigest() != release.sha256:
            raise ValueError("the installer changed after it was downloaded; nothing was installed")
        return bytes(data)

    fd = _open_locked(path)
    try:
        data = verified(fd)
    finally:
        os.close(fd)
    if elevation.is_admin():
        # The installer inherits our administrator rights, and Setup reopens its own exe by name after
        # it starts, past any lock we hold. So run it from a folder only administrators can change
        os.makedirs(elevation.UPDATES_DIR, exist_ok=True)
        _clean(elevation.UPDATES_DIR)
        path = os.path.join(elevation.UPDATES_DIR, os.path.basename(path))
        with open(path, "wb") as out:
            out.write(data)
    fd = _open_locked(path)
    try:
        verified(fd)
        logger.info(f"running {os.path.basename(path)}")
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/update=1"],
                         creationflags=flags, close_fds=True)
    finally:
        os.close(fd)
