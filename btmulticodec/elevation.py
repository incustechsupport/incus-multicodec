"""Optional: run as administrator, so mice and keyboards work in windows that run as administrator.

Windows stops a normal process from sending input to an elevated window (UIPI), and the app moves
the pointer and types with SendInput. Running the app elevated lifts that, except on the secure
desktop (UAC prompts, Ctrl+Alt+Del), which no app can reach.

To start elevated without a UAC prompt every time, the app registers an on-demand scheduled task
that runs it with the highest privileges (one prompt, when the option is turned on). Every normal
start (Start menu, sign-in, after an update) sees the setting, starts the task and exits.

The task never runs the per-user install: that folder (%LOCALAPPDATA%\\Programs) is writable by every
program the user runs, so any of them could swap the exe and get administrator rights through the
task without a prompt. Instead the elevated step copies the app into a folder under Program Files,
which only administrators can change, and the task runs that copy. After an update the copy is
refreshed, which takes one UAC prompt. Everything elevated runs from System32 by full path, and the
task is registered from an in-memory definition (no file another program could edit in between).
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import subprocess
import sys
from xml.sax.saxutils import escape

from . import __version__

logger = logging.getLogger(__name__)

TASK_NAME = "Incus Multicodec (administrator)"
LEGACY_TASK_NAME = "Windows Bluetooth Multicodec (administrator)"  # registered by 0.1.4-0.1.5
CREATE_NO_WINDOW = 0x08000000
SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
SCHTASKS = os.path.join(SYSTEM32, "schtasks.exe")
POWERSHELL = os.path.join(SYSTEM32, "WindowsPowerShell", "v1.0", "powershell.exe")
EXPLORER = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
PROTECTED_DIR = os.path.join(os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles", r"C:\Program Files"),
                             "Incus Multicodec (administrator)")
MARKER = "copied-from.json"  # in PROTECTED_DIR: {"version", "source"}
# Where the administrator copy keeps an update's installer before running it (admin-only, like
# PROTECTED_DIR; a separate folder because refreshing the copy mirrors PROTECTED_DIR)
UPDATES_DIR = PROTECTED_DIR + " updates"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def running_protected() -> bool:
    """True when this is the administrator copy under Program Files (or a dev run from source)."""
    if not _frozen():
        return True
    here = os.path.normcase(os.path.dirname(os.path.abspath(sys.executable)))
    return here == os.path.normcase(PROTECTED_DIR)


def _marker() -> dict:
    try:
        with open(os.path.join(PROTECTED_DIR, MARKER), encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def installed_exe() -> str:
    """The per-user install's exe (where updates go), also when called from the administrator copy."""
    if running_protected() and _frozen():
        source = _marker().get("source")
        if source:
            return os.path.join(source, os.path.basename(sys.executable))
    return sys.executable


def _command() -> tuple[str, str]:
    if _frozen():
        return os.path.join(PROTECTED_DIR, os.path.basename(sys.executable)), ""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pythonw, "-m btmulticodec tray"


def copy_is_current() -> bool:
    """Does the administrator copy match this install? (Dev runs from source have no copy.)"""
    if not _frozen():
        return True
    marker = _marker()
    return (marker.get("version") == __version__
            and os.path.isfile(os.path.join(PROTECTED_DIR, os.path.basename(sys.executable))))


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([SCHTASKS, *args], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)


def _existing_tasks() -> list[str]:
    return [name for name in (TASK_NAME, LEGACY_TASK_NAME) if _schtasks("/query", "/tn", name).returncode == 0]


def task_exists() -> bool:
    return bool(_existing_tasks())


def start_task() -> bool:
    """Start the elevated copy through the task; no prompt. False if the task is missing."""
    result = _schtasks("/run", "/tn", (_existing_tasks() or [TASK_NAME])[0])
    if result.returncode != 0:
        logger.warning(f"couldn't start the administrator task: {result.stderr.strip() or result.stdout.strip()}")
    return result.returncode == 0


def _task_xml() -> str:
    command, arguments = _command()
    # Priority 4 = normal: scheduled tasks otherwise run at below-normal CPU, I/O and memory priority
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts Incus Multicodec with administrator rights, so Bluetooth mice and keyboards also work in windows that run as administrator.</Description></RegistrationInfo>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
    <AllowStartOnDemand>true</AllowStartOnDemand>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(command)}</Command><Arguments>{escape(arguments)}</Arguments></Exec></Actions>
</Task>
"""


def _elevated(executable: str, parameters: str) -> int:
    """Run a command with a UAC prompt and wait for it. Returns its exit code (or raises if declined)."""
    import ctypes.wintypes as w

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", w.HWND), ("lpVerb", w.LPCWSTR),
                    ("lpFile", w.LPCWSTR), ("lpParameters", w.LPCWSTR), ("lpDirectory", w.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", w.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", w.LPCWSTR), ("hkeyClass", w.HKEY), ("dwHotKey", w.DWORD),
                    ("hIcon", w.HANDLE), ("hProcess", w.HANDLE)]
    SEE_MASK_NOCLOSEPROCESS = 0x40
    info = SHELLEXECUTEINFOW(cbSize=ctypes.sizeof(SHELLEXECUTEINFOW), fMask=SEE_MASK_NOCLOSEPROCESS,
                             lpVerb="runas", lpFile=executable, lpParameters=parameters, lpDirectory=SYSTEM32,
                             nShow=0)
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise PermissionError("administrator permission was not given")
    kernel32 = ctypes.windll.kernel32
    kernel32.WaitForSingleObject(info.hProcess, 180_000)
    code = w.DWORD()
    kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(info.hProcess)
    return code.value


def _ps_quote(text: str) -> str:
    # PowerShell also ends single-quoted strings at the typographic quotes; double those too
    for quote in ("'", "‘", "’", "‚", "‛"):
        text = text.replace(quote, quote * 2)
    return "'" + text + "'"


def _run_powershell(script: str) -> int:
    """Run a PowerShell script elevated (one UAC prompt unless already elevated). The script travels
    inside the command line (-EncodedCommand), so there is no file for another program to change."""
    encoded = base64.b64encode(("$ErrorActionPreference = 'Stop'\n" + script).encode("utf-16-le")).decode()
    parameters = f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}"
    if is_admin():
        return subprocess.run([POWERSHELL, *parameters.split()], capture_output=True,
                              creationflags=CREATE_NO_WINDOW).returncode
    return _elevated(POWERSHELL, parameters)


def _copy_script() -> str:
    """Copy this install into PROTECTED_DIR (mirrored) and record where it came from."""
    source = os.path.dirname(os.path.abspath(sys.executable))
    marker = json.dumps({"version": __version__, "source": source})
    return f"""
$src = {_ps_quote(source)}
$dst = {_ps_quote(PROTECTED_DIR)}
& {_ps_quote(os.path.join(SYSTEM32, 'robocopy.exe'))} $src $dst /MIR /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) {{ exit 2 }}
[System.IO.File]::WriteAllText((Join-Path $dst {_ps_quote(MARKER)}), {_ps_quote(marker)})
"""


def install_task() -> None:
    """Copy the app to Program Files and register the task (one UAC prompt, unless already elevated)."""
    script = _copy_script() if _frozen() else ""
    # The 0.1.4-0.1.5 task runs the per-user exe, which any program can replace: it must go, or the
    # bypass this layout closes stays open on upgraded PCs
    script += f"""
Unregister-ScheduledTask -TaskName {_ps_quote(LEGACY_TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Xml {_ps_quote(_task_xml())} -Force | Out-Null
"""
    code = _run_powershell(script)
    if code != 0:
        raise RuntimeError(f"couldn't set up the administrator task (exit code {code})")


def refresh_copy() -> None:
    """After an update: bring the administrator copy up to date (one UAC prompt unless elevated).
    The task is registered again too, so it points at the copy even if it predates this layout."""
    install_task()


def remove_task() -> None:
    """Remove the task (current and pre-0.1.6 names) and the administrator copy."""
    script = ""
    for name in (TASK_NAME, LEGACY_TASK_NAME):
        script += (f"Unregister-ScheduledTask -TaskName {_ps_quote(name)} -Confirm:$false "
                   f"-ErrorAction SilentlyContinue\n")
    remove_dir = "".join(f"if (Test-Path -LiteralPath {_ps_quote(folder)}) "
                         f"{{ Remove-Item -LiteralPath {_ps_quote(folder)} -Recurse -Force }}\n"
                         for folder in (PROTECTED_DIR, UPDATES_DIR))
    if running_protected() and _frozen():
        # This process runs from that folder and can't delete it: an elevated helper waits for us to
        # exit (the caller quits straight after) and then removes it
        later = base64.b64encode((f"Wait-Process -Id {os.getpid()} -Timeout 60 -ErrorAction SilentlyContinue\n"
                                  + remove_dir).encode("utf-16-le")).decode()
        script += (f"Start-Process -FilePath {_ps_quote(POWERSHELL)} -WindowStyle Hidden -ArgumentList "
                   f"'-NoProfile -NonInteractive -EncodedCommand {later}'\n")
    else:
        script += remove_dir
    if not task_exists() and not os.path.isdir(PROTECTED_DIR) and not os.path.isdir(UPDATES_DIR):
        return
    code = _run_powershell(script)
    if code != 0:
        raise RuntimeError(f"couldn't remove the administrator task (exit code {code})")


def start_unelevated() -> None:
    """Start a normal (not elevated) copy from an elevated one: Explorer starts programs at its own level."""
    if not _frozen():
        command, arguments = _command()
        subprocess.Popen([command, *arguments.split()], creationflags=CREATE_NO_WINDOW)  # dev runs only
    else:
        subprocess.Popen([EXPLORER, installed_exe()])
