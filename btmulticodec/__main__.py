"""Windows Bluetooth Multicodec: LDAC / aptX HD / aptX / AAC / aptX LL / SBC Bluetooth audio for Windows, on a dedicated USB dongle.

Usage:  python -m btmulticodec                  tray app (default)
        python -m btmulticodec adapters         list Bluetooth adapters and which one Bluetooth Multicodec may use
        python -m btmulticodec setup-adapter <vid:pid>      switch a dongle to WinUSB for Bluetooth Multicodec
        python -m btmulticodec release-adapter              give Bluetooth Multicodec's dongle back to Windows
        python -m btmulticodec info | scan | selftest
        python -m btmulticodec codecs <address>
        python -m btmulticodec pair <address>
        python -m btmulticodec play <address> [--codec auto|ldac|aptx-hd|aptx|aac|aptx-ll|sbc]
                               [--quality auto|hq|sq|mq] [--source system|"CABLE Output"]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys

from . import adapters, ldac, selection
from .settings import LOG_FILE, STATE_DIR, Settings, set_system_adapter

log = logging.getLogger("btmulticodec")


def setup_logging(verbose: bool, to_file: bool) -> None:
    handlers: list[logging.Handler] = []
    if to_file:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2,
                                                             encoding="utf-8"))
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not verbose:
        logging.getLogger("bumble").setLevel(logging.WARNING)


def default_transport(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = adapters.ours()
    if not found:
        raise SystemExit("no Bluetooth adapter is set up for Bluetooth Multicodec; see `python -m btmulticodec adapters`")
    return found[0].transport


# ----------------------------------------------------------------------------- CLI commands
def cmd_adapters(_args) -> None:
    found = adapters.list_adapters()
    if not found:
        print("no USB Bluetooth adapters found")
    for a in found:
        problem = adapters.takeover_problem(a, found)
        verdict, chip, why = adapters.chip_support(a)
        status = "Bluetooth Multicodec" if a.ours else ("Windows" if a.windows_owned else a.service or "no driver")
        print(f"{a.usb_id}  {status:8}  {'removable' if a.removable else 'built-in ':9}  {a.name}")
        print(f"           {chip}: {verdict} ({why})")
        if not a.ours:
            if problem:
                print(f"           not usable: {problem}")
            elif adapters.is_system_adapter(a, found):
                print("           can be used as a fallback, but Windows Bluetooth is off while it is (setup-adapter --yes)")
            else:
                print("           can be set up for Bluetooth Multicodec")


def cmd_setup_adapter(args) -> None:
    found = adapters.list_adapters()
    match = [a for a in found if a.usb_id == args.usb_id.lower()]
    if not match:
        raise SystemExit(f"no Bluetooth adapter {args.usb_id}")
    adapter = match[0]
    system = adapters.is_system_adapter(adapter, found)
    if system and not args.yes and not args.force:
        raise SystemExit(f"{adapters.SYSTEM_ADAPTER_WARNING}\n\nRe-run with --yes to go ahead.")
    adapters.take_over(adapter, force=args.force)
    set_system_adapter(adapter.instance_id if system else None)
    print(f"{adapter.name} is now set up for Bluetooth Multicodec")


def cmd_release_adapter(_args) -> None:
    for a in adapters.ours():
        adapters.give_back(a)
        print(f"{a.name} given back to Windows")


async def _with_device(args, work):
    """Run work(device) on a powered-on Bumble device (no streaming engine)."""
    from bumble.a2dp import make_audio_source_service_sdp_records
    from bumble.device import Device, DeviceConfiguration
    from bumble.transport import open_transport

    from .engine import CLASS_OF_DEVICE, FIRMWARE_DIR
    from .settings import KEYS_FILE

    os.environ.setdefault("BUMBLE_INTEL_FIRMWARE_DIR", str(FIRMWARE_DIR / "intel"))
    os.environ.setdefault("BUMBLE_RTK_FIRMWARE_DIR", str(FIRMWARE_DIR / "realtek"))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    spec = default_transport(args.transport)
    print(f"opening {spec} (firmware load can take a few seconds)...")
    async with await open_transport(spec) as transport:
        device = Device.from_config_with_hci(DeviceConfiguration.from_dict({
            "name": os.environ.get("COMPUTERNAME", "Windows PC"), "class_of_device": CLASS_OF_DEVICE,
            "classic_enabled": True, "le_enabled": False, "keystore": f"JsonKeyStore:{KEYS_FILE}",
        }), transport.source, transport.sink)
        device.sdp_service_records = {0x00010001: make_audio_source_service_sdp_records(0x00010001)}
        await device.power_on()
        await work(device)


async def _connect(device, address: str, repair: bool = False):
    from bumble import hci
    from bumble.core import PhysicalTransport

    from .engine import classic_address
    peer = classic_address(address)
    if repair:
        try:
            await device.keystore.delete(str(peer))
        except KeyError:
            pass
    print(f"connecting to {address}...")
    connection = await device.connect(peer, transport=PhysicalTransport.BR_EDR)
    try:
        await connection.authenticate()
        await connection.encrypt()
    except hci.HCI_Error as error:
        await connection.disconnect()
        raise SystemExit(f"authentication failed ({error.error_name}). Put the device in pairing mode and run:"
                         f"  python -m btmulticodec pair {address}")
    return connection


async def _protocol(connection):
    from bumble.avdtp import Protocol, find_avdtp_service_with_connection
    version = await find_avdtp_service_with_connection(connection)
    if not version:
        raise SystemExit("that device has no A2DP sink service")
    protocol = await Protocol.connect(connection, version)
    endpoints = list(await protocol.discover_remote_endpoints())
    return protocol, endpoints, selection.find_sinks(protocol)


def cmd_selftest(_args) -> None:
    """Encode a 2 s test tone with every codec and report the resulting bitrate."""
    import numpy as np

    from .capture import float_to_s24
    from .stream import AacSession, AptxSession, LdacSession, SbcSession

    rate = 48000
    t = np.arange(rate * 2) / rate
    tone = float_to_s24(np.repeat(0.3 * np.sin(2 * np.pi * 1000 * t), 2).astype(np.float32))
    sbc_config = bytes([0x11, 0x15, 2, 53])  # 48 kHz joint stereo, 16 blocks, 8 subbands, loudness, bitpool 53
    sessions = {
        "LDAC 990": lambda: LdacSession(679, rate, ldac.EQMID_HQ),
        "LDAC 330": lambda: LdacSession(679, rate, ldac.EQMID_MQ),
        "aptX HD": lambda: AptxSession(679, rate, True, 20, "aptX HD"),
        "aptX": lambda: AptxSession(679, rate, False, 20, "aptX"),
        "aptX LL": lambda: AptxSession(679, rate, False, 4, "aptX LL"),
        "AAC": lambda: AacSession(679, rate, 192000),
        "SBC": lambda: SbcSession(679, sbc_config),
    }
    failed = False
    for name, make in sessions.items():
        try:
            session = make()
            chunk = session.chunk_frames * 6
            packets = [p for i in range(0, len(tone) - chunk + 1, chunk) for p in session.encode(tone[i:i + chunk])]
            session.close()
            print(f"  ok    {name:9} {sum(map(len, packets)) * 8 / 2000:5.0f} kbps, {len(packets) / 2:4.0f} packets/s")
        except Exception as error:
            failed = True
            print(f"  FAIL  {name:9} {error}")
    print(f"  {'ok' if (adapters.BIN / 'wdi-simple.exe').is_file() else 'FAIL'}    WinUSB installer (wdi-simple.exe)")
    if failed:
        raise SystemExit(1)


def cmd_info(args) -> None:
    async def work(device):
        q = device.host.acl_packet_queue
        print(f"address      {device.public_address}")
        print(f"acl buffers  {q.max_in_flight} x {q.max_packet_size} bytes")
        print(f"libldac      {ldac.version()}")
    asyncio.run(_with_device(args, work))


def cmd_scan(args) -> None:
    from bumble.core import AdvertisingData

    async def work(device):
        seen = {}

        def on_result(address, class_of_device, data, rssi):
            name = data.get(AdvertisingData.COMPLETE_LOCAL_NAME) or data.get(AdvertisingData.SHORTENED_LOCAL_NAME)
            if address not in seen or (name and not seen[address]):
                seen[address] = name
                print(f"{address}  rssi {rssi:4}  cod 0x{class_of_device:06X}  {name or ''}")
        device.on("inquiry_result", on_result)
        print(f"scanning for {args.seconds}s (put headphones in pairing mode)...")
        await device.start_discovery()
        await asyncio.sleep(args.seconds)
        await device.stop_discovery()
    asyncio.run(_with_device(args, work))


def cmd_codecs(args) -> None:
    async def work(device):
        connection = await _connect(device, args.address)
        _, endpoints, sinks = await _protocol(connection)
        for endpoint in endpoints:
            print(f"  seid {endpoint.seid}: {selection.codec_info(endpoint)}")
        print("usable: " + ", ".join(c.label for c in selection.CODECS if c.key in sinks))
        await connection.disconnect()
    asyncio.run(_with_device(args, work))


def cmd_pair(args) -> None:
    async def work(device):
        connection = await _connect(device, args.address, repair=True)
        settings = Settings.load()
        settings.devices[args.address.upper()] = connection.peer_name or args.address
        settings.save()
        print("paired")
        await connection.disconnect()
    asyncio.run(_with_device(args, work))


def cmd_play(args) -> None:
    from . import capture
    from .stream import LivePump

    async def work(device):
        connection = await _connect(device, args.address)
        protocol, endpoints, sinks = await _protocol(connection)
        key = selection.choose(sinks, args.codec)
        if key is None:
            offered = ", ".join(str(selection.codec_info(e)) for e in endpoints)
            raise SystemExit(f"no {args.codec} sink on this device (it offers: {offered})")
        caps = selection.codec_info(sinks[key])
        source = (capture.SystemAudioCapture(mute_speakers=not args.no_mute) if args.source == "system"
                  else capture.DeviceCapture(args.source))
        rate = source.output_rate(selection.sample_rates(key, caps))
        configuration = selection.configure(key, caps, rate)

        def on_stats(mode, kbps, backlog_ms, dropped):
            print(f"\r  {mode}  {kbps:6.0f} kbps  buffer {backlog_ms:4.0f} ms  dropped {dropped}   ",
                  end="", flush=True)

        pump = LivePump(device.host, source, rate,
                        selection.session_factory(key, configuration, rate, args.quality), on_stats)
        stream = await protocol.create_stream(protocol.add_source(configuration, pump), sinks[key])
        await stream.start()
        print(f"streaming {selection.BY_KEY[key].label} at {rate} Hz from {source.name}. Ctrl+C to stop.")
        disconnected = asyncio.Event()
        connection.on("disconnection", lambda reason: disconnected.set())
        try:
            await disconnected.wait()
            print("\ndevice disconnected")
        finally:
            source.close()
            if not disconnected.is_set():
                await stream.stop()
                await stream.close()
                await connection.disconnect()
    asyncio.run(_with_device(args, work))


def main():
    parser = argparse.ArgumentParser(prog="bt-multicodec", description=__doc__.splitlines()[0])
    parser.add_argument("--transport", help="Bumble transport; default: the adapter set up for Bluetooth Multicodec")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("tray", help="run the tray app (default)")
    sub.add_parser("adapters", help="list Bluetooth adapters")
    p = sub.add_parser("setup-adapter", help="switch a dongle to WinUSB for Bluetooth Multicodec")
    p.add_argument("usb_id", help="vid:pid, as shown by `adapters`")
    p.add_argument("--yes", action="store_true", help="accept turning Windows Bluetooth off (PC's own adapter, no dongle)")
    p.add_argument("--force", action="store_true", help="skip all safety checks")
    sub.add_parser("release-adapter", help="give Bluetooth Multicodec's dongle back to Windows")
    sub.add_parser("info", help="load firmware and show adapter info")
    sub.add_parser("selftest", help="check every codec encoder and the WinUSB installer")
    p = sub.add_parser("scan", help="find nearby Bluetooth devices")
    p.add_argument("--seconds", type=int, default=10)
    for name in ("codecs", "pair"):
        sub.add_parser(name).add_argument("address")
    p = sub.add_parser("play", help="stream to a device from the command line")
    p.add_argument("address")
    p.add_argument("--codec", default="auto", choices=["auto", *selection.BY_KEY])
    p.add_argument("--quality", default="auto", choices=selection.LDAC_QUALITY, help="LDAC bitrate")
    p.add_argument("--source", default="system", help='"system" or a recording device name')
    p.add_argument("--no-mute", action="store_true", help="leave the PC speakers on")
    args = parser.parse_args()

    command = args.command or "tray"
    setup_logging(args.verbose, to_file=command == "tray")
    try:
        if command == "tray":
            from . import tray
            tray.main(args.transport)
        else:
            globals()[f"cmd_{command.replace('-', '_')}"](args)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    sys.exit(main())
