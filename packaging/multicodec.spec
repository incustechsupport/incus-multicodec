# PyInstaller spec: builds dist\windows-bluetooth-multicodec\ with bt-multicodec.exe (tray, no console) and bt-multicodec-cli.exe.
#   .venv\Scripts\pyinstaller packaging\multicodec.spec --noconfirm
import pathlib

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = pathlib.Path(SPECPATH).parent

datas = [
    (str(ROOT / "btmulticodec" / "bin"), "btmulticodec/bin"),  # ldac.dll, aptx.dll, sbc.dll, wdi-simple.exe
    (str(ROOT / "firmware"), "firmware"),
    (str(ROOT / "licenses"), "licenses"),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
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
    excludes=["tkinter", "matplotlib", "scipy", "PyQt5", "PySide6"],
)
pyz = PYZ(a.pure)

tray = EXE(pyz, a.scripts, [], exclude_binaries=True, name="bt-multicodec", console=False,
           version=None, icon=None)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="bt-multicodec-cli", console=True)
COLLECT(tray, cli, a.binaries, a.datas, name="windows-bluetooth-multicodec")
