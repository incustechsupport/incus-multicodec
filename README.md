# Incus Multicodec

**Download:** [latest release](https://github.com/incustechsupport/incus-multicodec/releases/latest) ·
[website](https://software.incustech.app/multicodec/)

High-quality Bluetooth audio for Windows 10/11: **LDAC, aptX HD, aptX, AAC and SBC**,
streamed through a cheap dedicated USB Bluetooth dongle. Windows keeps its own Bluetooth adapter
for mice, keyboards, phones and Phone Link; Incus Multicodec never touches it.

```
Windows apps ──► default output ──(WASAPI loopback)──► Incus Multicodec ──► encoder ──► USB dongle (WinUSB) ──► headphones
                  (PC speakers muted while streaming)            LDAC/aptX/SBC      Bumble Bluetooth stack
```

- No virtual sound card and no kernel driver: audio is captured with WASAPI loopback, which is
  taken before Windows' mute/volume. Incus Multicodec mutes the PC speakers while it streams and applies
  the Windows volume itself.
- The dongle is switched to Microsoft's inbox WinUSB driver with libwdi (the engine behind
  Zadig). That works with Secure Boot on and needs no paid driver signing.

## Codecs

| Codec | Encoder | Rates | Bitrate @48k | Notes |
|---|---|---|---|---|
| LDAC | Sony `libldac` (Apache-2.0) | 44.1–96 kHz | 990/660/330 kbps | best quality by default: 990 kbps whenever the link holds it, 24-bit input |
| aptX HD | Qualcomm's encoder from AOSP (Apache-2.0) | 44.1/48 kHz | 576 kbps | 24-bit, RTP framed |
| aptX | Qualcomm's encoder from AOSP (Apache-2.0) | 44.1/48 kHz | 384 kbps | raw codewords, no RTP |
| AAC | Windows' Media Foundation encoder | 44.1/48 kHz | up to 192 kbps CBR | LATM framed; not on Windows "N" without the Media Feature Pack |
| SBC | BlueZ `libsbc` (LGPL-2.1) | 44.1/48 kHz | ~328 kbps | bitpool 53, for everything else |

"Best quality available" (the default) picks LDAC > aptX HD > aptX > AAC > SBC from what the headphones offer.
AAC uses the encoder built into Windows, which Microsoft has licensed, so Incus Multicodec ships no AAC code.

## Dongles

What matters is the chip inside, not the brand. Incus Multicodec checks it before switching a dongle over
(`bt-multicodec-cli adapters`, or the tray's *Bluetooth dongle* menu):

| Verdict | Chips |
|---|---|
| **Supported** | Realtek RTL8761B/BU/BUV/CU/CUV and other Realtek parts with known firmware (TP-Link UB500, ASUS BT500, Edimax, most "Bluetooth 5.0/5.1 USB" dongles); Intel AX200/AX201/AX210/AX211/BE200 as a second adapter |
| **May be unreliable** | CSR8510 A10 (`0a12:0001`, clones common); Broadcom BCM20702 (ASUS BT400, Plugable), run without their firmware patch |
| **Not supported** | MediaTek, Qualcomm Atheros, Barrot and Actions chips (many no-name "Bluetooth 5.3/5.4" dongles); older Intel; Realtek models without known firmware |
| **Untested** | anything else: works if the chip needs no firmware upload |

"Supported" means Bumble's driver tables list that exact USB ID and the firmware is bundled.
Dongles that appear as USB *sound cards* (Creative BT-W5, Avantree DG80) are not Bluetooth
adapters; they do aptX themselves and don't need Incus Multicodec.

Incus Multicodec never takes an adapter whose USB ID matches another one plugged in (the
WinUSB binding is per USB ID).

### No dongle? Use the PC's own Bluetooth

When no usable dongle is plugged in, the PC's built-in Bluetooth (or Windows' only adapter) can be
used instead, after a warning: **while Incus Multicodec has it, Windows Bluetooth is off.**
To keep the essentials working, Incus Multicodec then acts as the host for **mice and
keyboards** itself (Bluetooth LE and Classic, including media keys and passkey pairing), feeding
their input to Windows. Phones, Phone Link, game controllers and other Bluetooth devices don't work
until the adapter is given back (tray → *Bluetooth dongle* → *Give back to Windows*; quitting the
app offers this too). Bluetooth mice and keyboards paired in Windows need pairing again in the app.

**Switching the PC's own Bluetooth usually needs one Windows restart**: Windows is using that
adapter, so it can't swap the driver until it restarts (the adapter may vanish until then; the tray
says "Restart Windows to finish"). The app is set to start at sign-in when you do this. A pending
switch can be cancelled from the tray. Giving the adapter back doesn't need a restart: the app
closes it first.

Input from these mice and keyboards can't reach the sign-in screen, UAC prompts or windows running
as administrator (a Windows limit on injected input), so keep a touchpad or wired mouse/keyboard
handy. With a supported dongle plugged in, the built-in adapter is never offered.

## Using it

1. Plug in the dongle, start **Incus Multicodec** (tray icon) → *Bluetooth dongle* → *Use … for Incus Multicodec*
   (one UAC prompt).
2. *Devices* → *Add a device…* with the headphones (or mouse/keyboard) in pairing mode → *Pair …*.
   Keyboards that need a passkey show it as a notification: type it on the keyboard, then Enter.
3. Play anything. Headphones, mice and keyboards reconnect automatically next time.

*Bluetooth dongle* → *Give back to Windows* undoes step 1 (so does uninstalling).

## Building from source

Requires Visual Studio 2022+ with C++ tools, Python 3.13, Git.

```
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
git clone --depth 1 https://android.googlesource.com/platform/external/libldac third_party\libldac
:: Qualcomm's aptX and aptX HD encoders: system\embdrv\encoder_for_aptx and encoder_for_aptxhd of
::   https://android.googlesource.com/platform/packages/modules/Bluetooth, copied into third_party\aosp-aptx
::   (the commit used is in third_party\aosp-aptx\SOURCE.txt)
git clone --depth 1 https://git.kernel.org/pub/scm/bluetooth/sbc.git          third_party\sbc
git clone --depth 1 https://github.com/pbatard/libwdi                         third_party\libwdi
:: WDK 8.0 redistributable (WinUSB co-installers needed by libwdi's INF template):
::   download https://go.microsoft.com/fwlink/p/?LinkID=253170 (wdfcoinstaller.msi), then
msiexec /a wdfcoinstaller.msi /qn TARGETDIR=%CD%\third_party\wdk8
native\build.cmd          :: ldac.dll, aptx.dll, aptxhd.dll, sbc.dll, aacmf.dll, procloop.dll
native\build-wdi.cmd      :: wdi-simple.exe
.venv\Scripts\python packaging\fetch_firmware.py   :: Intel + Realtek firmware into firmware\
.venv\Scripts\python -m btmulticodec selftest
.venv\Scripts\python packaging\collect_licenses.py :: licence texts of every bundled Python package into licenses\
.venv\Scripts\pyinstaller packaging\multicodec.spec --noconfirm     :: dist\incus-multicodec\
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\multicodec.iss   :: dist\incus-multicodec-setup-*.exe
.venv\Scripts\python packaging\make_source_package.py :: dist\incus-multicodec-*-third-party-sources.zip
```

A release attaches both the installer and the sources zip to a GitHub release tagged `v<version>`,
then updates `latest.json` (version, URL, SHA-256, size) on software.incustech.app, which the
app's update check reads.

`fetch_firmware.py` works around Bumble 0.0.236's broken `rtk_fw_download` import and adds the
Intel images linux-firmware only publishes as symlinks (e.g. `ibt-0040-4150` → `ibt-0040-0041`).

From Git Bash, run the `.cmd` scripts as `cmd //c 'native\build.cmd'`.

## Command line

`bt-multicodec-cli.exe` (or `python -m btmulticodec`): `adapters`, `setup-adapter <vid:pid>`,
`release-adapter`, `info`, `scan`, `pair <addr>`, `codecs <addr>`,
`play <addr> [--codec …] [--quality …] [--source system|"CABLE Output"]`, `selftest`.

Logs: `%LOCALAPPDATA%\windows-bluetooth-multicodec\multicodec.log`. Settings and pairings live in the same folder.

## Limits

- Audio out (A2DP source), plus mice and keyboards when using the PC's own Bluetooth.
  Calls/microphone (HFP), phones and game controllers are not supported.
- One audio device at a time. Keyboard LEDs (Caps Lock light) aren't driven yet.
- Lip-sync: LDAC/aptX add ~150–250 ms like on any platform. aptX Low Latency isn't offered (Qualcomm's
  open-source release covers aptX and aptX HD only); aptX LL headphones get plain aptX.
- See THIRD_PARTY_NOTICES.md for licences and trademarks.

## Licence

MIT (see LICENSE) for this project's own code. Bundled third-party components keep their own
licences; see THIRD_PARTY_NOTICES.md. TERMS.txt is what the installer shows (licence, no-warranty
terms, privacy summary). Privacy notice: https://software.incustech.app/multicodec/privacy/
