# PyInstaller spec: builds dist\incus-multicodec\ with bt-multicodec.exe (tray, no console) and bt-multicodec-cli.exe.
#   .venv\Scripts\pyinstaller packaging\multicodec.spec --noconfirm
import pathlib

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = pathlib.Path(SPECPATH).parent

datas = [
    (str(ROOT / "btmulticodec" / "bin"), "btmulticodec/bin"),  # ldac.dll, aptx.dll, sbc.dll, wdi-simple.exe
    (str(ROOT / "firmware"), "firmware"),
    (str(ROOT / "licenses"), "licenses"),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
    (str(ROOT / "TERMS.txt"), "."),
    (str(ROOT / "LICENSE"), "."),
]
datas += collect_data_files("_sounddevice_data")
binaries = collect_dynamic_libs("libusb_package") + collect_dynamic_libs("pyaudiowpatch")
hiddenimports = (
    collect_submodules("bumble.drivers")
    + collect_submodules("bumble.transport")
    + ["pystray._win32", "pycaw.pycaw", "comtypes.stream", "usb1"]
)

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "scipy", "PyQt5", "PySide6"],  # tkinter is used by the devices window
    # LGPL libraries stay plain .py files in _internal, so users can replace them with their own versions
    module_collection_mode={"pystray": "py", "usb1": "py"},
)
# Only the PortAudio build sounddevice loads here (64-bit, no ASIO). The package (and PyInstaller's hook
# for it) also brings 32-bit, ARM, macOS and ASIO-enabled builds; the ASIO ones carry Steinberg's ASIO
# SDK terms with them.
def _unused_portaudio(entry):
    name = entry[0].replace("\\", "/")
    return "portaudio-binaries/" in name and not name.endswith("/libportaudio64bit.dll")


a.datas = [d for d in a.datas if not _unused_portaudio(d)]
a.binaries = [b for b in a.binaries if not _unused_portaudio(b)]
pyz = PYZ(a.pure)

tray = EXE(pyz, a.scripts, [], exclude_binaries=True, name="bt-multicodec", console=False,
           version=None, icon=None)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="bt-multicodec-cli", console=True)
COLLECT(tray, cli, a.binaries, a.datas, name="incus-multicodec")
