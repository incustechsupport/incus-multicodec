Incus Multicodec third-party notices
====================================

Incus Multicodec (copyright 2026 Incus Technologies Limited, MIT licence, see LICENSE) includes the
components below. Their full licence texts are in the `licenses` folder (in an installed copy:
`_internal\licenses`).

Source code for the LGPL-licensed components, exactly as built, including our build scripts and
configuration, is attached to every release at
https://github.com/incustechsupport/incus-multicodec/releases (`...-third-party-sources.zip`).
Incus Multicodec's own source code is in the same repository.

Codecs (separate DLLs in `_internal\btmulticodec\bin`)
------------------------------------------------------
- **LDAC encoder** (`ldac.dll`): libldac by Sony Corporation, from the Android Open Source Project
  (https://android.googlesource.com/platform/external/libldac, commit eeee1a3), built without
  changes. Apache License 2.0: `licenses/libldac-Apache-2.0.txt`, `licenses/libldac-NOTICE`.
- **aptX and aptX HD encoders** (`aptx.dll`, `aptxhd.dll`): Qualcomm's encoders as published in the
  Android Open Source Project (https://android.googlesource.com/platform/packages/modules/Bluetooth,
  `system/embdrv/encoder_for_aptx` and `encoder_for_aptxhd`, commit 745ee92), built without changes
  together with Incus Multicodec's own small wrapper (`native/aptx_aosp.c`). Apache License 2.0:
  `licenses/aosp-bluetooth-NOTICE` (the AOSP Bluetooth module's licence and notice).
- aptX Low Latency is not supported.
- **SBC encoder** (`sbc.dll`): the BlueZ SBC library (https://git.kernel.org/pub/scm/bluetooth/sbc.git,
  release 2.2), built without changes. GNU LGPL 2.1 or later: `licenses/libsbc-LGPL-2.1.txt`.
- **AAC**: `aacmf.dll` is Incus Multicodec's own code calling the AAC encoder that comes with
  Windows (Media Foundation). No AAC encoder code is included.
- The LGPL-licensed DLLs can be replaced with your own builds of the same libraries: Incus
  Multicodec loads them by file name from `_internal\btmulticodec\bin`.

Driver setup
------------
- **wdi-simple.exe**: libwdi by Pete Batard (https://github.com/pbatard/libwdi, commit 30df0c0),
  built with our configuration file (`native/libwdi_config.h`, copied over `msvc/config.h`) and
  no other changes. GNU LGPL 3.0: `licenses/libwdi-LGPL-3.0.txt`.
- It embeds Microsoft's WinUSB co-installers (`winusbcoinstaller2.dll`, `WdfCoInstaller01011.dll`)
  from the Windows Driver Kit 8.0 redistributable components, unmodified, under Microsoft's
  redistribution terms: `licenses/microsoft-wdk8-redist-License.txt`.

Bluetooth stack and runtime
---------------------------
- **Bumble** (Google, https://github.com/google/bumble): the Bluetooth host stack. Apache License 2.0.
- **libusb** (`libusb-1.0.dll`, version 1.0.26, via the libusb-package Python package): GNU LGPL 2.1
  or later, `licenses/libusb-LGPL-2.1.txt`. The DLL is unmodified and can be replaced.
- **PortAudio** (`libportaudio64bit.dll`, via sounddevice): MIT-style licence,
  `licenses/portaudio-LICENSE.txt`.
- **pystray** (LGPL 3.0) and **libusb1** (LGPL 2.1 or later) are shipped as plain Python source
  files in `_internal`, so they can be replaced with your own versions.
- **Python** 3.13 (PSF License) and the libraries that come with it (OpenSSL, libffi, bzip2, xz,
  SQLite, zlib and others): `licenses/python-runtime/Python-LICENSE.txt`. **Tcl/Tk** (BSD-style):
  `licenses/python-runtime/Tcl-Tk-license.terms`.
- Every Python package in the bundle, with its own licence files, in `licenses/python/`:
  bumble, cffi, click, comtypes, cryptography, importlib_resources, libusb-package, libusb1, numpy,
  pillow, platformdirs, prompt_toolkit, psutil, PyAudioWPatch, pycaw, pycparser, pyee, pyserial,
  pyserial-asyncio, pystray, pyusb, six, sounddevice, typing_extensions, wcwidth, websockets.
  Their licences are MIT, BSD, Apache 2.0, PSF, MIT-CMU or LGPL as listed in each folder.

Firmware
--------
Bluetooth adapter firmware from the linux-firmware repository
(https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git), downloaded by
`packaging/fetch_firmware.py` and shipped unmodified in `_internal\firmware`:
- Intel Bluetooth firmware (`ibt-*.sfi`, `ibt-*.ddc`), redistributable in binary form:
  `licenses/firmware-LICENCE.ibt_firmware`.
- Realtek Bluetooth firmware (`rtl*_fw.bin`, `rtl*_config.bin`), redistributable in binary form:
  `licenses/firmware-LICENCE.rtlwifi_firmware.txt`.

Trademarks
----------
LDAC is a trademark of Sony Corporation. aptX is a trademark of Qualcomm Technologies
International, Ltd. Bluetooth is a registered trademark of Bluetooth SIG, Inc. Windows is a
trademark of the Microsoft group of companies. Incus Multicodec is not affiliated with, certified
by or endorsed by any of them; the names are used only to say what it works with.
