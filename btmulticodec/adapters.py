"""Find USB Bluetooth adapters and hand one to (or back from) WinUSB.

Bluetooth Multicodec only ever drives a *dedicated* adapter: a dongle switched to WinUSB, so Windows keeps
its own Bluetooth adapter for mice, phones and Phone Link. The switch uses libwdi's
wdi-simple.exe (inbox WinUSB + a catalog signed by a throwaway local certificate), so it works
with Secure Boot on and needs no paid signing.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import re
import subprocess
from ctypes import wintypes
from dataclasses import dataclass

BIN = pathlib.Path(__file__).parent / "bin"
BLUETOOTH_COMPATIBLE_ID = "USB\\CLASS_E0&SUBCLASS_01&PROT_01"
WINDOWS_BT_SERVICE = "BTHUSB"

DIGCF_PRESENT = 0x02
DIGCF_ALLCLASSES = 0x04
SPDRP_DEVICEDESC = 0x00
SPDRP_HARDWAREID = 0x01
SPDRP_COMPATIBLEIDS = 0x02
SPDRP_SERVICE = 0x04
SPDRP_FRIENDLYNAME = 0x0C
SPDRP_CAPABILITIES = 0x0F
CM_DEVCAP_REMOVABLE = 0x04
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", ctypes.c_byte * 16),
                ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]


@dataclass
class Adapter:
    instance_id: str
    name: str
    vid: int
    pid: int
    service: str  # BTHUSB = Windows owns it, WinUSB = available to Bluetooth Multicodec
    removable: bool

    @property
    def usb_id(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"

    @property
    def windows_owned(self) -> bool:
        return self.service.upper() == WINDOWS_BT_SERVICE

    @property
    def ours(self) -> bool:
        return self.service.upper() == "WINUSB"

    @property
    def transport(self) -> str:
        return f"usb:{self.usb_id}"


def _registry_property(setupapi, hdev, info, prop) -> tuple[int, bytes] | None:
    reg_type = wintypes.DWORD()
    size = wintypes.DWORD()
    setupapi.SetupDiGetDeviceRegistryPropertyW(hdev, ctypes.byref(info), prop, ctypes.byref(reg_type),
                                               None, 0, ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if not setupapi.SetupDiGetDeviceRegistryPropertyW(hdev, ctypes.byref(info), prop, ctypes.byref(reg_type),
                                                      buf, size, None):
        return None
    return reg_type.value, buf.raw


def _string(raw) -> str:
    return raw[1].decode("utf-16-le").split("\0")[0] if raw else ""


def _multi(raw) -> list[str]:
    return [s for s in raw[1].decode("utf-16-le").split("\0") if s] if raw else []


def list_adapters() -> list[Adapter]:
    """Every present USB Bluetooth radio, whichever driver it is bound to."""
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

    hdev = setupapi.SetupDiGetClassDevsW(None, "USB", None, DIGCF_ALLCLASSES | DIGCF_PRESENT)
    if hdev in (None, INVALID_HANDLE_VALUE):
        raise ctypes.WinError(ctypes.get_last_error())
    adapters = []
    try:
        index = 0
        while True:
            info = SP_DEVINFO_DATA(cbSize=ctypes.sizeof(SP_DEVINFO_DATA))
            if not setupapi.SetupDiEnumDeviceInfo(hdev, index, ctypes.byref(info)):
                break
            index += 1
            compatible = [c.upper() for c in _multi(_registry_property(setupapi, hdev, info, SPDRP_COMPATIBLEIDS))]
            if BLUETOOTH_COMPATIBLE_ID not in compatible:
                continue
            hardware_ids = _multi(_registry_property(setupapi, hdev, info, SPDRP_HARDWAREID))
            match = next((re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", h, re.I) for h in hardware_ids), None)
            if not match:
                continue
            instance = ctypes.create_unicode_buffer(512)
            setupapi.SetupDiGetDeviceInstanceIdW(hdev, ctypes.byref(info), instance, 512, None)
            caps = _registry_property(setupapi, hdev, info, SPDRP_CAPABILITIES)
            adapters.append(Adapter(
                instance_id=instance.value,
                name=_string(_registry_property(setupapi, hdev, info, SPDRP_FRIENDLYNAME))
                or _string(_registry_property(setupapi, hdev, info, SPDRP_DEVICEDESC)),
                vid=int(match.group(1), 16),
                pid=int(match.group(2), 16),
                service=_string(_registry_property(setupapi, hdev, info, SPDRP_SERVICE)),
                removable=bool(caps and int.from_bytes(caps[1][:4], "little") & CM_DEVCAP_REMOVABLE),
            ))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return adapters


SUPPORTED, UNRELIABLE, UNSUPPORTED, UNKNOWN = "supported", "may be unreliable", "not supported", "untested"

# Chips by USB vendor ID that Bluetooth Multicodec can't drive, or drives with caveats. Realtek and Intel
# parts are judged by Bumble's own firmware tables (exact VID:PID) before this is consulted.
VENDOR_VERDICTS = {
    0x0A12: (UNRELIABLE, "CSR chip",
             "works without firmware, but cheap clones of this chip are common and some drop audio; "
             "LDAC may settle at a lower bitrate"),
    0x0A5C: (UNRELIABLE, "Broadcom chip",
             "runs without the firmware patch Windows normally loads; usually fine, occasionally flaky"),
    0x0BDA: (UNSUPPORTED, "Realtek chip", "Bluetooth Multicodec has no firmware for this Realtek model"),
    0x8087: (UNSUPPORTED, "Intel chip", "older Intel model whose firmware Bluetooth Multicodec can't load"),
    0x0E8D: (UNSUPPORTED, "MediaTek chip", "needs a MediaTek firmware loader Bluetooth Multicodec doesn't have"),
    0x0CF3: (UNSUPPORTED, "Qualcomm Atheros chip", "needs a Qualcomm firmware loader Bluetooth Multicodec doesn't have"),
    0x33FA: (UNSUPPORTED, "Barrot chip", "only works with its maker's own Windows driver"),
    0x10D7: (UNSUPPORTED, "Actions chip", "only works with its maker's own Windows driver"),
}


def chip_support(adapter: Adapter) -> tuple[str, str, str]:
    """(verdict, chip description, explanation) for using this adapter with Bluetooth Multicodec."""
    from bumble.drivers import intel, rtk

    usb = (adapter.vid, adapter.pid)
    if usb in rtk.RTK_USB_PRODUCTS:
        return SUPPORTED, "Realtek chip", "firmware included"
    if usb in intel.INTEL_USB_PRODUCTS:
        return SUPPORTED, "Intel chip", "firmware included"
    if adapter.vid in VENDOR_VERDICTS:
        return VENDOR_VERDICTS[adapter.vid]
    return UNKNOWN, "unknown chip", "hasn't been tested; it will work if the chip runs without a firmware upload"


def ours() -> list[Adapter]:
    return [a for a in list_adapters() if a.ours]


SYSTEM_ADAPTER_WARNING = (
    "This is the Bluetooth Windows itself uses. While Bluetooth Multicodec has it, Windows Bluetooth is off: "
    "Bluetooth mice, keyboards, phones, Phone Link and other headphones disconnect, including at the "
    "sign-in screen, until you give it back from the tray menu. Make sure you have a touchpad or wired "
    "mouse/keyboard first. A separate USB Bluetooth dongle avoids all of this."
)


def _hard_problem(adapter: Adapter, adapters: list[Adapter]) -> str | None:
    """Reasons this adapter can never be used, whatever else is plugged in."""
    if adapter.ours:
        return "it is already set up for Bluetooth Multicodec"
    twins = [a for a in adapters if a.usb_id == adapter.usb_id and a.instance_id != adapter.instance_id]
    if twins:
        # The WinUSB driver is installed per VID:PID, so it would capture the twin as well
        return f"another adapter has the same USB ID ({adapter.usb_id}), and both would be switched"
    verdict, chip, why = chip_support(adapter)
    if verdict == UNSUPPORTED:
        return f"{chip} not supported: {why}"
    return None


def holding_builtin(adapters: list[Adapter]) -> list[Adapter]:
    """Built-in adapters Bluetooth Multicodec currently has (the no-dongle fallback)."""
    return [a for a in adapters if a.ours and not a.removable]


def is_system_adapter(adapter: Adapter, adapters: list[Adapter]) -> bool:
    """Built into the PC, or the only Bluetooth adapter Windows has: taking it costs the user Windows Bluetooth."""
    if not adapter.removable:
        return True
    if holding_builtin(adapters):
        return False  # switching to this dongle gives the built-in one back to Windows
    return adapter.windows_owned and not any(
        a.windows_owned for a in adapters if a.instance_id != adapter.instance_id)


def takeover_problem(adapter: Adapter, adapters: list[Adapter] | None = None) -> str | None:
    """Why this adapter can't be switched to WinUSB, or None if it can (see takeover_warning for caveats)."""
    adapters = adapters if adapters is not None else list_adapters()
    if problem := _hard_problem(adapter, adapters):
        return problem
    if is_system_adapter(adapter, adapters):
        # The PC's own Bluetooth is only a fallback for when there's no dongle to use instead
        if any(a.ours for a in adapters):
            return "a dongle is already set up for Bluetooth Multicodec"
        dongles = [a for a in adapters if a.instance_id != adapter.instance_id
                   and not is_system_adapter(a, adapters) and _hard_problem(a, adapters) is None]
        if dongles:
            return f"use the {dongles[0].name} dongle instead, so Windows keeps its own Bluetooth"
    return None


def _run_elevated(exe: pathlib.Path, args: str) -> int:
    """ShellExecuteEx 'runas' and wait; returns the exit code (UAC prompt shown to the user)."""
    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    SEE_MASK_NOCLOSEPROCESS = 0x40
    info = SHELLEXECUTEINFOW(cbSize=ctypes.sizeof(SHELLEXECUTEINFOW), fMask=SEE_MASK_NOCLOSEPROCESS,
                             lpVerb="runas", lpFile=str(exe), lpParameters=args, nShow=0)
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError()  # includes the user declining the UAC prompt
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return code.value


def take_over(adapter: Adapter, force: bool = False) -> None:
    if not force and (problem := takeover_problem(adapter)):
        raise RuntimeError(f"won't use {adapter.name}: {problem}")
    dest = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "windows-bluetooth-multicodec" / "driver"
    args = (f'--name "Bluetooth Multicodec adapter" --manufacturer "Windows Bluetooth Multicodec" --inf btmulticodec.inf '
            f'--vid 0x{adapter.vid:04X} --pid 0x{adapter.pid:04X} --type 0 --dest "{dest}" --silent')
    code = _run_elevated(BIN / "wdi-simple.exe", args)
    if code:
        raise RuntimeError(f"WinUSB install failed (wdi-simple exit {code})")


def give_back(adapter: Adapter) -> None:
    """Remove the WinUSB binding so Windows' own driver takes the adapter again."""
    script = (
        f"$ErrorActionPreference='Stop';"
        f"$id='{adapter.instance_id}';"
        f"$inf=(Get-PnpDeviceProperty -InstanceId $id -KeyName DEVPKEY_Device_DriverInfPath).Data;"
        f"pnputil /remove-device $id | Out-Null;"
        f"if ($inf -like 'oem*.inf') {{ pnputil /delete-driver $inf /force | Out-Null }};"
        f"pnputil /scan-devices | Out-Null"
    )
    powershell = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    code = _run_elevated(powershell, f'-NoProfile -NonInteractive -Command "{script}"')
    if code:
        raise RuntimeError(f"restoring the Windows driver failed (exit {code})")


if __name__ == "__main__":
    from .__main__ import cmd_adapters
    cmd_adapters(None)
