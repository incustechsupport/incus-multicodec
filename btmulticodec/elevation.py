"""Optional: run as administrator, so mice and keyboards work in windows that run as administrator.

Windows stops a normal process from sending input to an elevated window (UIPI), and the app moves
the pointer and types with SendInput. Running the app elevated lifts that, except on the secure
desktop (UAC prompts, Ctrl+Alt+Del), which no app can reach.

To start elevated without a UAC prompt every time, the app registers an on-demand scheduled task
that runs it with the highest privileges (one prompt, when the option is turned on). Every normal
start (Start menu, sign-in, after an update) sees the setting, starts the task and exits.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

TASK_NAME = "Incus Multicodec (administrator)"
LEGACY_TASK_NAME = "Windows Bluetooth Multicodec (administrator)"  # registered by 0.1.4-0.1.5
CREATE_NO_WINDOW = 0x08000000


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _command() -> tuple[str, str]:
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pythonw, "-m btmulticodec tray"


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks.exe", *args], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)


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
                             lpVerb="runas", lpFile=executable, lpParameters=parameters, nShow=0)
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise PermissionError("administrator permission was not given")
    kernel32 = ctypes.windll.kernel32
    kernel32.WaitForSingleObject(info.hProcess, 60_000)
    code = w.DWORD()
    kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(info.hProcess)
    return code.value


def install_task() -> None:
    """Register the task (one UAC prompt, unless already elevated)."""
    path = os.path.join(tempfile.gettempdir(), "incus-multicodec-task.xml")
    with open(path, "w", encoding="utf-16") as f:
        f.write(_task_xml())
    try:
        if is_admin():
            result = _schtasks("/create", "/tn", TASK_NAME, "/xml", path, "/f")
            code = result.returncode
        else:
            code = _elevated("schtasks.exe", f'/create /tn "{TASK_NAME}" /xml "{path}" /f')
        if code != 0:
            raise RuntimeError(f"schtasks couldn't create the task (exit code {code})")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def remove_task() -> None:
    for name in _existing_tasks():
        code = (_schtasks("/delete", "/tn", name, "/f").returncode if is_admin()
                else _elevated("schtasks.exe", f'/delete /tn "{name}" /f'))
        if code != 0:
            raise RuntimeError(f"schtasks couldn't remove the task (exit code {code})")


def start_unelevated() -> None:
    """Start a normal (not elevated) copy from an elevated one: Explorer starts programs at its own level."""
    command, arguments = _command()
    if arguments:
        subprocess.Popen([command, *arguments.split()], creationflags=CREATE_NO_WINDOW)  # dev runs only
    else:
        subprocess.Popen(["explorer.exe", command])
