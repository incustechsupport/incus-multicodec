"""Turn decoded HID input into Windows input with SendInput.

Keys go in as hardware scan codes, so the user's keyboard layout applies as for any keyboard.
Windows doesn't auto-repeat injected keys (the keyboard driver normally does), so held keys are
repeated here using the user's keyboard delay/rate settings. Limits: SendInput can't reach the
sign-in/UAC secure desktop, nor (from a non-elevated process) elevated windows.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
from ctypes import wintypes

from .hidreport import InputState

logger = logging.getLogger(__name__)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0001, 0x0800, 0x1000
BUTTON_FLAGS = {  # HID button -> (down, up, mouseData)
    1: (0x0002, 0x0004, 0), 2: (0x0008, 0x0010, 0), 3: (0x0020, 0x0040, 0),
    4: (0x0080, 0x0100, 1), 5: (0x0080, 0x0100, 2),  # XBUTTON1 (back), XBUTTON2 (forward)
}
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0008
WHEEL_DELTA = 120

# HID keyboard usage -> PC set-1 scan code (0xE0xx = extended)
SCANCODES = {
    **{0x04 + i: c for i, c in enumerate([
        0x1E, 0x30, 0x2E, 0x20, 0x12, 0x21, 0x22, 0x23, 0x17, 0x24, 0x25, 0x26, 0x32, 0x31, 0x18, 0x19,
        0x10, 0x13, 0x1F, 0x14, 0x16, 0x2F, 0x11, 0x2D, 0x15, 0x2C])},  # a-z
    **{0x1E + i: 0x02 + i for i in range(10)},  # 1-9, 0
    0x28: 0x1C, 0x29: 0x01, 0x2A: 0x0E, 0x2B: 0x0F, 0x2C: 0x39, 0x2D: 0x0C, 0x2E: 0x0D, 0x2F: 0x1A,
    0x30: 0x1B, 0x31: 0x2B, 0x32: 0x2B, 0x33: 0x27, 0x34: 0x28, 0x35: 0x29, 0x36: 0x33, 0x37: 0x34,
    0x38: 0x35, 0x39: 0x3A,
    **{0x3A + i: 0x3B + i for i in range(10)},  # F1-F10
    0x44: 0x57, 0x45: 0x58,  # F11, F12
    0x46: 0xE037, 0x47: 0x46, 0x49: 0xE052, 0x4A: 0xE047, 0x4B: 0xE049, 0x4C: 0xE053, 0x4D: 0xE04F,
    0x4E: 0xE051, 0x4F: 0xE04D, 0x50: 0xE04B, 0x51: 0xE050, 0x52: 0xE048,
    0x53: 0x45, 0x54: 0xE035, 0x55: 0x37, 0x56: 0x4A, 0x57: 0x4E, 0x58: 0xE01C,
    0x59: 0x4F, 0x5A: 0x50, 0x5B: 0x51, 0x5C: 0x4B, 0x5D: 0x4C, 0x5E: 0x4D, 0x5F: 0x47, 0x60: 0x48,
    0x61: 0x49, 0x62: 0x52, 0x63: 0x53, 0x64: 0x56, 0x65: 0xE05D, 0x67: 0x59,
    **{0x68 + i: c for i, c in enumerate([0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x76])},
    0x87: 0x73, 0x88: 0x70, 0x89: 0x7D, 0x8A: 0x79, 0x8B: 0x7B,
    0xE0: 0x1D, 0xE1: 0x2A, 0xE2: 0x38, 0xE3: 0xE05B, 0xE4: 0xE01D, 0xE5: 0x36, 0xE6: 0xE038, 0xE7: 0xE05C,
}
PAUSE_USAGE, VK_PAUSE = 0x48, 0x13  # Pause has no plain make code; use its virtual key
MODIFIERS = set(range(0xE0, 0xE8))

# Consumer-page usage -> virtual key
CONSUMER_VK = {
    0xE9: 0xAF, 0xEA: 0xAE, 0xE2: 0xAD,  # volume up/down/mute
    0xCD: 0xB3, 0xB5: 0xB0, 0xB6: 0xB1, 0xB7: 0xB2,  # play/pause, next, previous, stop
    0x223: 0xAC, 0x224: 0xA6, 0x225: 0xA7, 0x221: 0xAA, 0x22A: 0xAB,  # browser home/back/forward/search/favourites
    0x18A: 0xB4, 0x192: 0xB7, 0x194: 0xB6, 0x183: 0xB5,  # mail, calculator, my computer, media select
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(inputs: list[INPUT]) -> None:
    if not inputs:
        return
    array = (INPUT * len(inputs))(*inputs)
    if ctypes.windll.user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)) != len(inputs):
        # Typically UIPI (an elevated window has focus) or the secure desktop
        logger.debug(f"SendInput blocked (error {ctypes.get_last_error()})")


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    return INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)))


def _key(usage: int, up: bool) -> INPUT | None:
    if usage == PAUSE_USAGE:
        return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(VK_PAUSE, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)))
    code = SCANCODES.get(usage)
    if code is None:
        return None
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if code > 0xFF else 0)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, code & 0xFF, flags, 0, 0)))


def _vk(vk: int, up: bool) -> INPUT:
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)))


def _repeat_timing() -> tuple[float, float]:
    """(initial delay, interval) in seconds from the Windows keyboard settings."""
    SPI_GETKEYBOARDDELAY, SPI_GETKEYBOARDSPEED = 0x0016, 0x000A
    delay, speed = wintypes.UINT(1), wintypes.UINT(31)
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETKEYBOARDDELAY, 0, ctypes.byref(delay), 0)
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETKEYBOARDSPEED, 0, ctypes.byref(speed), 0)
    per_second = 2.5 + speed.value * (30 - 2.5) / 31
    return (delay.value + 1) * 0.25, 1 / per_second


class _DeviceInput:
    def __init__(self):
        self.buttons: set[int] = set()
        self.keys: set[int] = set()
        self.consumer: set[int] = set()
        self.repeat: asyncio.TimerHandle | None = None


class Injector:
    """Feeds decoded reports from any number of HID devices into Windows."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.devices: dict[str, _DeviceInput] = {}

    def apply(self, device: str, state: InputState) -> None:
        d = self.devices.setdefault(device, _DeviceInput())
        out: list[INPUT] = []
        if state.has_pointer:
            for button in sorted(state.buttons - d.buttons):
                if button in BUTTON_FLAGS:
                    down, _, data = BUTTON_FLAGS[button]
                    out.append(_mouse(down, data=data))
            for button in sorted(d.buttons - state.buttons):
                if button in BUTTON_FLAGS:
                    _, up, data = BUTTON_FLAGS[button]
                    out.append(_mouse(up, data=data))
            d.buttons = set(state.buttons)
            if state.dx or state.dy:
                out.append(_mouse(MOUSEEVENTF_MOVE, state.dx, state.dy))
            if state.wheel:
                out.append(_mouse(MOUSEEVENTF_WHEEL, data=state.wheel * WHEEL_DELTA))
            if state.hwheel:
                out.append(_mouse(MOUSEEVENTF_HWHEEL, data=state.hwheel * WHEEL_DELTA))
        if state.has_keys:
            pressed = state.keys - d.keys
            released = d.keys - state.keys
            # Modifiers down first and up last, so shortcuts land in the right order
            for usage in sorted(pressed, key=lambda u: u not in MODIFIERS):
                if (i := _key(usage, False)) is not None:
                    out.append(i)
            for usage in sorted(released, key=lambda u: u in MODIFIERS):
                if (i := _key(usage, True)) is not None:
                    out.append(i)
            d.keys = set(state.keys)
            new_keys = [u for u in pressed if u not in MODIFIERS]
            if new_keys:
                self._start_repeat(d, new_keys[-1])
            elif d.repeat and not any(u not in MODIFIERS for u in d.keys):
                self._stop_repeat(d)
        if state.has_consumer:
            out += [_vk(CONSUMER_VK[u], False) for u in state.consumer - d.consumer if u in CONSUMER_VK]
            out += [_vk(CONSUMER_VK[u], True) for u in d.consumer - state.consumer if u in CONSUMER_VK]
            d.consumer = set(state.consumer)
        _send(out)

    def release_all(self, device: str) -> None:
        """A device went away: let go of everything it was holding."""
        d = self.devices.pop(device, None)
        if not d:
            return
        self._stop_repeat(d)
        out = [_mouse(BUTTON_FLAGS[b][1], data=BUTTON_FLAGS[b][2]) for b in d.buttons if b in BUTTON_FLAGS]
        out += [i for u in d.keys if (i := _key(u, True)) is not None]
        out += [_vk(CONSUMER_VK[u], True) for u in d.consumer if u in CONSUMER_VK]
        _send(out)

    def _start_repeat(self, d: _DeviceInput, usage: int) -> None:
        self._stop_repeat(d)
        delay, interval = _repeat_timing()

        def tick():
            if usage not in d.keys:
                d.repeat = None
                return
            if (i := _key(usage, False)) is not None:
                _send([i])
            d.repeat = self.loop.call_later(interval, tick)
        d.repeat = self.loop.call_later(delay, tick)

    @staticmethod
    def _stop_repeat(d: _DeviceInput) -> None:
        if d.repeat:
            d.repeat.cancel()
            d.repeat = None
