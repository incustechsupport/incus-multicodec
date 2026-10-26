"""Ask Windows to treat the app as real-time audio.

A tray app with no window is a "background" process to Windows 11: on battery or the Balanced plan
it can be put in efficiency mode (EcoQoS: slow cores, coalesced timers), which shows up as audio
pauses. The audio threads also join MMCSS, the scheduler class Windows' own audio engine uses.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

logger = logging.getLogger(__name__)

PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4
ABOVE_NORMAL_PRIORITY_CLASS = 0x8000

_local = threading.local()


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


def boost_process() -> None:
    """Opt out of efficiency mode and run above normal priority. Safe to call more than once."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Without these, ctypes passes the pseudo-handle (-1) as a 32-bit int and both calls fail
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    process = kernel32.GetCurrentProcess()
    # ControlMask = what we decide, StateMask = 0: "never throttle these"
    state = _PowerThrottlingState(1, PROCESS_POWER_THROTTLING_EXECUTION_SPEED
                                  | PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION, 0)
    if not kernel32.SetProcessInformation(process, PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state)):
        logger.warning(f"SetProcessInformation(power throttling) failed: {ctypes.get_last_error()}")
    if not kernel32.SetPriorityClass(process, ABOVE_NORMAL_PRIORITY_CLASS):
        logger.warning(f"SetPriorityClass failed: {ctypes.get_last_error()}")


def audio_thread(task: str = "Audio") -> None:
    """Register the calling thread with MMCSS (once per thread). Windows drops it again when the thread ends."""
    if getattr(_local, "mmcss", None) is not None:
        return
    try:
        avrt = ctypes.WinDLL("avrt", use_last_error=True)
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        index = wintypes.DWORD(0)
        _local.mmcss = avrt.AvSetMmThreadCharacteristicsW(task, ctypes.byref(index)) or 0
        if not _local.mmcss:
            logger.debug(f"MMCSS {task!r} unavailable: {ctypes.get_last_error()}")
    except OSError as error:
        _local.mmcss = 0
        logger.debug(f"MMCSS unavailable: {error}")
