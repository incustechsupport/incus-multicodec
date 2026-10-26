"""System tray front end for the engine."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import winreg

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem as Item

from . import adapters, capture, selection
from .engine import Engine
from .settings import LOG_FILE, STATE_DIR, Settings, set_system_adapter, system_adapter

logger = logging.getLogger(__name__)

APP_NAME = "Windows Bluetooth Multicodec"
RUN_VALUE = "windows-bluetooth-multicodec"  # autostart entry name
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MB_YESNO, MB_ICONWARNING, MB_ICONERROR, IDYES = 0x04, 0x30, 0x10, 6
# A box opened from a tray app has no window of its own to sit on; without these it can open
# behind other windows or without input focus
MB_SETFOREGROUND, MB_TOPMOST = 0x00010000, 0x00040000
WM_APP_UPDATE_MENU = 0x8000 + 0x101  # WM_APP-range message: rebuild the menu on the tray's own thread


def _icon_image(state: str) -> Image.Image:
    """A headphones glyph: grey = no dongle, white = idle, blue = streaming."""
    colour = {"none": (128, 128, 128), "idle": (235, 235, 235), "streaming": (64, 156, 255)}[state]
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.arc((8, 6, 56, 54), start=180, end=360, fill=colour, width=7)
    d.rounded_rectangle((5, 30, 19, 56), radius=4, fill=colour)
    d.rounded_rectangle((45, 30, 59, 56), radius=4, fill=colour)
    return img


def _ask(text: str, title: str = APP_NAME) -> bool:
    """Blocking Yes/No box. Never call it on the tray thread: use it from a _background action."""
    flags = MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
    return ctypes.windll.user32.MessageBoxW(None, text, title, flags) == IDYES


def _error(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST)


def _background(action):
    """Run a menu action on its own thread.

    pystray calls menu actions from inside the tray window's message handler, straight after the
    popup menu closes. A message box opened there never gets real mouse/keyboard input (it can't be
    clicked or closed), and anything slow freezes the tray. So actions that ask, wait or do I/O run
    on a worker thread, where MessageBox gets its own modal message loop.
    """
    def start(*_):
        threading.Thread(target=action, name="tray-action", daemon=True).start()
    return start


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw}" -m btmulticodec tray'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


class TrayApp:
    def __init__(self, transport: str | None = None):
        self.settings = Settings.load()
        self.engine = Engine(self.settings, on_change=self._on_change, transport=transport)
        self.icon = pystray.Icon("windows-bluetooth-multicodec", _icon_image("none"), APP_NAME, menu=Menu(self._menu_items))
        # pystray's update_menu() rebuilds (and destroys) the menu on the calling thread; the engine
        # calls in from its own thread, so hop over to the tray thread first
        self.icon._message_handlers[WM_APP_UPDATE_MENU] = lambda wparam, lparam: self.icon.update_menu()
        self._last_message = None

    def run(self) -> None:
        self.engine.start()
        self.icon.run()

    # ------------------------------------------------------------------ state -> UI
    def _on_change(self) -> None:
        s = self.engine.status
        state = "streaming" if s.streaming else ("idle" if s.adapter and not s.adapter_problem else "none")
        self.icon.icon = _icon_image(state)
        self.icon.title = self._status_line()[:127]
        if s.message and s.message != self._last_message:
            self._last_message = s.message
            try:
                self.icon.notify(s.message, APP_NAME)
            except Exception:
                pass
        self._update_menu()

    def _update_menu(self) -> None:
        hwnd = getattr(self.icon, "_hwnd", None)
        if hwnd and threading.current_thread() is not getattr(self.icon, "_thread", None):
            ctypes.windll.user32.PostMessageW(hwnd, WM_APP_UPDATE_MENU, 0, 0)
        else:
            self.icon.update_menu()

    def _status_line(self) -> str:
        s = self.engine.status
        if s.adapter_problem:
            return s.adapter_problem
        if s.streaming:
            return f"{s.streaming} · {s.kbps:.0f} kbps → {s.device_name}"
        if s.device_name:
            return f"Connecting to {s.device_name}..."
        if s.scanning:
            return "Searching..."
        return "Not connected"

    # ------------------------------------------------------------------ menu
    def _menu_items(self):
        s = self.engine.status
        return (
            Item(self._status_line(), None, enabled=False),
            Menu.SEPARATOR,
            Item("Devices", Menu(self._device_items)),
            Item("Codec", Menu(self._codec_items)),
            Item("LDAC quality", Menu(self._quality_items)),
            Item("Audio source", Menu(self._source_items)),
            Item("Mute PC speakers while streaming", self._toggle_mute,
                 checked=lambda _: self.settings.mute_speakers),
            Menu.SEPARATOR,
            Item("Bluetooth dongle", Menu(self._adapter_items)),
            Item("Start at login", lambda: set_autostart(not autostart_enabled()),
                 checked=lambda _: autostart_enabled()),
            Item("Open log", lambda: os.startfile(LOG_FILE) if LOG_FILE.exists() else os.startfile(STATE_DIR)),
            Item("Quit", _background(self._quit)),
        )

    def _device_items(self):
        s = self.engine.status
        items = []
        for address, name in sorted(self.settings.devices.items(), key=lambda kv: kv[1].lower()):
            connected = s.device == address
            label = f"{name}  ({', '.join(s.codecs)})" if connected and s.codecs else name
            items.append(Item(label, Menu(
                Item("Disconnect" if connected else "Connect",
                     (lambda a: lambda: self.engine.disconnect())(address) if connected
                     else (lambda a: lambda: self.engine.connect(a))(address)),
                Item("Forget", _background((lambda a, n: lambda: _ask(f"Forget {n}?") and self.engine.forget(a))(address, name))),
            ), checked=(lambda c: lambda _: c)(connected)))
        hid_connected = self.engine.hid.connected()
        if self.settings.hid_devices:
            if items:
                items.append(Menu.SEPARATOR)
            items.append(Item("Mice & keyboards", None, enabled=False))
            for address, info in sorted(self.settings.hid_devices.items(), key=lambda kv: kv[1]["name"].lower()):
                connected = address in hid_connected
                items.append(Item(info["name"] if connected else f"{info['name']}  (asleep or off)", Menu(
                    Item("Forget", _background((lambda a, n: lambda: _ask(f"Forget {n}?") and self.engine.forget(a))(
                        address, info["name"]))),
                ), checked=(lambda c: lambda _: c)(connected)))
        if items:
            items.append(Menu.SEPARATOR)
        if s.scanning:
            items.append(Item("Searching... (put the device in pairing mode)", None, enabled=False))
        else:
            items.append(Item("Add a device...", lambda: self.engine.scan(12), enabled=self.engine.device is not None))
        kinds = {"audio": "headphones/speaker", "hid": "mouse/keyboard", "hid-le": "mouse/keyboard"}
        for address, name in s.found.items():
            if address not in self.settings.devices and address not in self.settings.hid_devices:
                kind = kinds.get(s.found_kind.get(address, "audio"), "")
                items.append(Item(f"Pair {name} ({kind})", (lambda a, n: lambda: self.engine.pair(a, n))(address, name)))
        return items

    def _codec_items(self):
        def choose(key):
            def action():
                self.settings.codec = key
                self.settings.save()
                self.engine.restream()
            return action
        return [Item("Best available", choose("auto"), checked=lambda _: self.settings.codec == "auto", radio=True)] + [
            Item(c.label, choose(c.key), radio=True, checked=(lambda k: lambda _: self.settings.codec == k)(c.key))
            for c in selection.CODECS
        ]

    def _quality_items(self):
        labels = {"auto": "Adaptive (recommended)", "hq": "990 kbps", "sq": "660 kbps", "mq": "330 kbps"}

        def choose(key):
            def action():
                self.settings.ldac_quality = key
                self.settings.save()
                self.engine.restream()
            return action
        return [Item(label, choose(key), radio=True, checked=(lambda k: lambda _: self.settings.ldac_quality == k)(key))
                for key, label in labels.items()]

    def _source_items(self):
        def choose(source):
            def action():
                self.settings.source = source
                self.settings.save()
                self.engine.restream()
            return action
        items = [Item("Everything Windows plays", choose("system"), radio=True,
                      checked=lambda _: self.settings.source == "system")]
        try:
            names = capture.list_recording_devices()
        except Exception:
            names = []
        for name in names:
            items.append(Item(name, choose(name), radio=True, checked=(lambda n: lambda _: self.settings.source == n)(name)))
        return items

    def _adapter_items(self):
        try:
            found = adapters.list_adapters()
        except Exception as error:
            return [Item(f"Can't list adapters: {error}", None, enabled=False)]
        items = []
        for a in found:
            verdict, chip, why = adapters.chip_support(a)
            if a.ours:
                items.append(Item(f"{a.name}: used by Bluetooth Multicodec ({chip}, {verdict})", Menu(
                    Item(why, None, enabled=False),
                    Item("Give back to Windows", _background((lambda ad: lambda: self._give_back(ad))(a))))))
            else:
                problem = adapters.takeover_problem(a, found)
                owner = "Windows" if a.windows_owned else "not in use"
                if problem:
                    items.append(Item(f"{a.name} ({owner}) – can't use: {problem.split(';')[0]}", None, enabled=False))
                else:
                    mark = "" if verdict == adapters.SUPPORTED else f" – {verdict}"
                    if adapters.is_system_adapter(a, found):
                        label = f"Use this PC's own Bluetooth ({a.name}){mark} – turns Windows Bluetooth off..."
                    else:
                        label = f"Use {a.name} ({chip}{mark}) for Bluetooth Multicodec..."
                    items.append(Item(label, _background((lambda ad, fd: lambda: self._take_over(ad, fd))(a, found))))
        if not any(a.ours for a in found) and not any(not adapters.takeover_problem(a, found) for a in found):
            items.append(Item("Plug in a USB Bluetooth dongle to use with Bluetooth Multicodec", None, enabled=False))
        elif not any(a.ours or not adapters.is_system_adapter(a, found) for a in found):
            items.append(Item("Tip: a USB Bluetooth dongle lets Windows keep its own Bluetooth", None, enabled=False))
        return items

    # ------------------------------------------------------------------ actions
    def _toggle_mute(self) -> None:
        self.settings.mute_speakers = not self.settings.mute_speakers
        self.settings.save()
        self.engine.restream()

    def _take_over(self, adapter: adapters.Adapter, found: list[adapters.Adapter]) -> None:
        verdict, chip, why = adapters.chip_support(adapter)
        caveat = "" if verdict == adapters.SUPPORTED else f"Note: {chip}, {verdict}. It {why}.\n\n"
        system = adapters.is_system_adapter(adapter, found)
        what = ("WARNING: " + adapters.SYSTEM_ADAPTER_WARNING if system
                else "Windows will stop using this dongle (its driver is switched to WinUSB).")
        if not _ask(f"Use {adapter.name} ({adapter.usb_id}) for Bluetooth Multicodec?\n\n{caveat}{what}\n\n"
                    "You can give it back from this menu at any time. "
                    "Windows will ask for administrator permission."):
            return

        builtin = adapters.holding_builtin(found)

        def work():
            try:
                adapters.take_over(adapter)
                set_system_adapter(adapter.instance_id if system else None)
            except Exception as error:
                _error(f"Couldn't set up {adapter.name}:\n{error}")
                return
            # Moving from the no-dongle fallback to a dongle: Windows gets its own Bluetooth back
            for old in builtin:
                try:
                    adapters.give_back(old)
                except Exception as error:
                    _error(f"{adapter.name} is set up, but giving {old.name} back to Windows failed:\n{error}\n\n"
                           "Use Bluetooth dongle → Give back to Windows to retry.")
        threading.Thread(target=work, daemon=True).start()

    def _give_back(self, adapter: adapters.Adapter) -> None:
        if not _ask(f"Give {adapter.name} back to Windows? Bluetooth Multicodec will stop streaming."):
            return

        def work():
            try:
                self.engine.disconnect()
                adapters.give_back(adapter)
            except Exception as error:
                _error(f"Couldn't restore {adapter.name}:\n{error}")
        threading.Thread(target=work, daemon=True).start()

    def _quit(self) -> None:
        try:
            self.engine.shutdown()
        except Exception:
            logger.exception("shutdown")
        # Leaving the PC's own Bluetooth on WinUSB with the app closed leaves the user with none at all
        system = [a for a in adapters.ours() if a.instance_id == system_adapter()]
        if system and _ask("Give this PC's Bluetooth back to Windows now?\n\n"
                           "If you don't, Windows Bluetooth stays off until Bluetooth Multicodec runs again."):
            try:
                adapters.give_back(system[0])
                set_system_adapter(None)
            except Exception as error:
                _error(f"Couldn't restore {system[0].name}:\n{error}")
        self.icon.stop()


def main(transport: str | None = None) -> None:
    TrayApp(transport).run()
