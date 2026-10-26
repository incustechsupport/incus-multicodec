Windows Bluetooth Multicodec third-party notices
================================================

Bluetooth Multicodec bundles the following components. Full licence texts are in the `licenses` folder.

Codecs (separate DLLs, built from unmodified upstream source)
-------------------------------------------------------------
- libldac (Sony, via the Android Open Source Project): Apache License 2.0. ldac.dll.
- libfreeaptx (aptX / aptX HD encoder): GNU LGPL 2.1. aptx.dll.
  Source: https://github.com/iamthehorker/libfreeaptx
- AAC: aacmf.dll is Bluetooth Multicodec's own wrapper around the AAC encoder that ships with Windows
  (Media Foundation); no AAC encoder code is distributed with Bluetooth Multicodec.
- libsbc (BlueZ SBC codec): GNU LGPL 2.1. sbc.dll.
  Source: https://git.kernel.org/pub/scm/bluetooth/sbc.git
- The LGPL-licensed DLLs may be replaced with your own builds of the same libraries;
  Bluetooth Multicodec loads them by file name from the `btmulticodec\bin` folder.

Driver setup
------------
- libwdi / wdi-simple (WinUSB installer): GNU LGPL 3.0. Source: https://github.com/pbatard/libwdi
- Microsoft WinUSB co-installers from the WDK 8.0 redistributable components, embedded in
  wdi-simple.exe under Microsoft's redistribution terms.

Bluetooth stack and runtime
---------------------------
- Bumble (Google): Apache License 2.0.
- libusb, via libusb-package / libusb1: GNU LGPL 2.1.
- PortAudio (via PyAudioWPatch and sounddevice): MIT-style licence.
- PyAudioWPatch: Apache License 2.0. sounddevice: MIT. pycaw, comtypes: MIT.
- pystray: GNU LGPL 3.0. Pillow: MIT-CMU (HPND). NumPy: BSD 3-Clause.
- Python: PSF License.

Firmware (from linux-firmware, redistributable in binary form)
---------------------------------------------------------------
- Intel Bluetooth firmware (ibt-*.sfi, ibt-*.ddc): see licenses/firmware-LICENCE.ibt_firmware
- Realtek Bluetooth firmware (rtl*_fw.bin, rtl*_config.bin): see licenses/firmware-LICENCE.rtlwifi_firmware.txt

Trademarks
----------
LDAC is a trademark of Sony Corporation. aptX is a trademark of Qualcomm Technologies
International, Ltd. Bluetooth is a trademark of Bluetooth SIG, Inc. Bluetooth Multicodec is not affiliated
with, certified by or endorsed by any of them. The names are used only to identify codec
compatibility.
