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

import hashlib
import json
import logging
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
    return tuple(int(part) for part in re.findall(r"\d+", text)[:4])


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


def download(release: Release, progress=None) -> str:
    """Download and verify the installer; returns its path. progress(done_bytes, total_bytes) is optional."""
    folder = os.path.join(tempfile.gettempdir(), "incus-multicodec-update")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"incus-multicodec-setup-{release.version}.exe")
    digest = hashlib.sha256()
    done = 0
    with _get(release.url, 60) as response, open(path + ".part", "wb") as out:
        while chunk := response.read(256 * 1024):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, release.size)
    if done != release.size or digest.hexdigest() != release.sha256:
        os.remove(path + ".part")
        raise ValueError("the downloaded installer didn't match the published checksum; nothing was installed")
    os.replace(path + ".part", path)
    return path


def run_installer(path: str) -> None:
    """Start the installer detached. The caller must exit straight away so its files can be replaced."""
    logger.info(f"running {path}")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/update=1"],
                     creationflags=flags, close_fds=True)
