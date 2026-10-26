"""The background engine: owns the Bluetooth Multicodec dongle and keeps one audio sink connected and streaming.

Runs its own asyncio loop on a worker thread. The UI calls the public methods from any thread
and gets told about changes through on_change(); read `status` for the current picture.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys
import threading
from dataclasses import dataclass, field

from bumble import hci
from bumble.a2dp import make_audio_source_service_sdp_records
from bumble.avdtp import AVDTP_TSEP_SNK, Listener, Protocol, find_avdtp_service_with_connection
from bumble.core import AdvertisingData, PhysicalTransport
from bumble.device import Device, DeviceConfiguration
from bumble.pairing import PairingConfig, PairingDelegate
from bumble.transport import open_transport

import usb1

from . import adapters, bumble_patches, capture, selection  # noqa: F401 (bumble_patches: applied on import)
from .hidhost import HidHost, advertised_name, is_hid_advertisement
from .settings import KEYS_FILE, STATE_DIR, Settings, system_adapter
from .stream import LivePump

logger = logging.getLogger(__name__)

APP_DIR = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).resolve().parent.parent))
FIRMWARE_DIR = APP_DIR / "firmware"
CLASS_OF_DEVICE = 0x28010C  # computer/laptop, Audio + Capturing service bits
ADAPTER_POLL_SECONDS = 5
RECONNECT_SECONDS = 20


class NotASpeaker(RuntimeError):
    """The device can't receive audio from us (no A2DP sink), so retrying is pointless."""


def classic_address(address: str) -> hci.Address:
    """BR/EDR peers always have public addresses; Bumble keys its keystore by the "/P"-qualified form."""
    return hci.Address(address.split("/")[0] + "/P")


@dataclass
class Status:
    adapter: str | None = None  # name of the dongle in use
    adapter_problem: str | None = None  # why there is none
    device: str | None = None  # address of the connected sink
    device_name: str | None = None
    streaming: str | None = None  # e.g. "LDAC HQ (auto)"
    kbps: float = 0.0
    codecs: list[str] = field(default_factory=list)  # codecs the connected sink offers
    message: str | None = None  # last notable event or error
    scanning: bool = False
    found: dict[str, str] = field(default_factory=dict)  # address -> name from the last scan
    found_kind: dict[str, str] = field(default_factory=dict)  # address -> "audio" | "hid" | "hid-le"
    found_class: dict[str, int] = field(default_factory=dict)  # address -> class of device (classic)
    found_rssi: dict[str, int] = field(default_factory=dict)  # address -> signal strength (dBm)


class _Delegate(PairingDelegate):
    """We have a "display": keyboards that need a passkey get it shown as a notification."""

    def __init__(self, engine: Engine):
        super().__init__(io_capability=PairingDelegate.DISPLAY_OUTPUT_ONLY)
        self.engine = engine

    async def display_number(self, number: int, digits: int) -> None:
        self.engine._changed(f"Type {number:0{digits}d} on the keyboard, then press Enter")


class Engine:
    def __init__(self, settings: Settings, on_change=lambda: None, transport: str | None = None):
        self.settings = settings
        self.on_change = on_change
        self.transport_spec = transport
        self.status = Status()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._thread_main, name="engine", daemon=True)
        self.device: Device | None = None
        self.connection = None
        self.stream = None
        self.capture = None
        self._want_device: str | None = settings.last_device
        self._wake = None  # asyncio.Event, created on the loop
        self.hid = HidHost(self)
        self._pending_change = None  # asyncio.TimerHandle for _changed_soon
        self._session: asyncio.Task | None = None  # the running adapter session
        self._released = False  # adapter handed back; don't reopen it
        self._unreachable: str | None = None  # device whose reconnect failure was already reported

    # ------------------------------------------------------------------ public (any thread)
    def start(self) -> None:
        self.thread.start()

    def connect(self, address: str) -> None:
        self._call(self._connect_request(address))

    def disconnect(self) -> None:
        self._call(self._disconnect_request())

    def scan(self, seconds: int = 10, fresh: bool = True) -> None:
        """Search for devices; fresh=False keeps what earlier searches found (continuous searching)."""
        self._call(self._scan(seconds, fresh))

    def pair(self, address: str, name: str | None = None) -> None:
        self._call(self._pair(address, name))

    def forget(self, address: str) -> None:
        self._call(self._forget(address))

    def restream(self) -> None:
        """Re-negotiate the stream after a codec/source setting change."""
        self._call(self._restream())

    def shutdown(self) -> None:
        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop).result(timeout=10)

    def release_adapter(self) -> None:
        """Close the adapter (blocking) so its driver can be swapped without a Windows restart."""
        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._release(), self.loop).result(timeout=20)

    def resume_adapter(self) -> None:
        self._call(self._resume())

    # ------------------------------------------------------------------ plumbing
    def _call(self, coroutine) -> None:
        asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def _changed(self, message: str | None = None) -> None:
        if message is not None:
            self.status.message = message
            logger.info(message)
        try:
            self.on_change()
        except Exception:
            logger.exception("on_change failed")

    def _changed_soon(self, delay: float = 0.5) -> None:
        """Coalesce bursts of status changes into one UI refresh."""
        if self._pending_change is None:
            def fire():
                self._pending_change = None
                self._changed()
            self._pending_change = self.loop.call_later(delay, fire)

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self) -> None:
        self._wake = asyncio.Event()
        os.environ.setdefault("BUMBLE_INTEL_FIRMWARE_DIR", str(FIRMWARE_DIR / "intel"))
        os.environ.setdefault("BUMBLE_RTK_FIRMWARE_DIR", str(FIRMWARE_DIR / "realtek"))
        while True:
            spec = self.transport_spec or await self._wait_for_adapter()
            if self._released:
                continue
            self._session = asyncio.ensure_future(self._run_adapter(spec))
            try:
                await self._session
            except asyncio.CancelledError:
                if not self._released:
                    raise
            except usb1.USBErrorAccess:
                # Another driver still holds the adapter (a switch waiting for a restart, or Windows'
                # own driver came back): say so once instead of logging a traceback every few seconds
                self.status.adapter = None
                self._set_problem("Bluetooth adapter is busy: restart Windows to finish switching it over")
            except Exception as error:
                logger.exception("adapter session ended")
                self.status.adapter = None
                self._changed(f"Bluetooth dongle stopped: {error}")
            self._session = None
            self.hid.detach()
            self.device = self.connection = self.stream = None
            await asyncio.sleep(ADAPTER_POLL_SECONDS)

    def _set_problem(self, problem: str | None) -> None:
        if self.status.adapter_problem != problem:
            self.status.adapter_problem = problem
            self._changed(problem)

    async def _wait_for_adapter(self) -> str:
        while True:
            if not self._released:
                listed = await asyncio.to_thread(adapters.list_adapters)
                found = [a for a in listed if a.ours]
                # A dongle wins over the built-in adapter (mid-switch from the no-dongle fallback)
                found.sort(key=lambda a: not a.removable)
                if found:
                    self.status.adapter_problem = None
                    self.status.adapter = found[0].name
                    return found[0].transport
                pending = [a for a in listed if a.restart_pending]
                marker = system_adapter()
                if pending or (marker and not any(a.instance_id == marker for a in listed)):
                    # Switched, but Windows' own driver couldn't let go of the adapter until a restart
                    # (the PC's own Bluetooth drops off the bus in the meantime)
                    self._set_problem("Restart Windows to finish switching the Bluetooth adapter over")
                else:
                    self._set_problem("No dongle set up for Bluetooth Multicodec")
            await asyncio.sleep(ADAPTER_POLL_SECONDS)

    async def _release(self) -> None:
        self._released = True
        self._want_device = None
        if self.connection is not None:
            try:
                await self.connection.disconnect()
            except Exception:
                pass
        self._teardown_capture()
        if self._session is not None:
            self._session.cancel()
            await asyncio.wait([self._session])  # leaving the transport's `async with` closes the USB handle
        self._set_problem("Bluetooth adapter handed back to Windows")

    async def _resume(self) -> None:
        self._released = False
        self._want_device = self.settings.last_device

    async def _run_adapter(self, spec: str) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        async with await open_transport(spec) as transport:
            try:
                device = await self._power_on(transport, le=True)  # LE for Bluetooth LE mice and keyboards
            except hci.HCI_Error as error:
                # Classic-only controllers reject the LE setup; audio and Classic HID still work
                logger.info(f"LE setup rejected ({error}); running Classic-only")
                device = await self._power_on(transport, le=False)
            await device.set_connectable(True)
            self.device = device
            self.hid.attach(device)
            self.status.adapter = self.status.adapter or spec
            self._changed(f"Bluetooth dongle ready ({device.public_address})")

            # Keep trying to reach the wanted device; headphones that power on later can also connect to us
            terminated = asyncio.ensure_future(transport.source.terminated)
            while not terminated.done():
                if self._want_device and self.connection is None and not self.status.scanning:
                    await self._try_connect(self._want_device)
                self._wake.clear()
                try:
                    await asyncio.wait_for(asyncio.shield(self._wake.wait()), RECONNECT_SECONDS)
                except asyncio.TimeoutError:
                    pass

    async def _power_on(self, transport, le: bool) -> Device:
        config = DeviceConfiguration.from_dict({
            "name": os.environ.get("COMPUTERNAME", "Windows PC"),
            "class_of_device": CLASS_OF_DEVICE,
            "classic_enabled": True,
            "le_enabled": le,
            "keystore": f"JsonKeyStore:{KEYS_FILE}",
        })
        device = Device.from_config_with_hci(config, transport.source, transport.sink)
        device.pairing_config_factory = lambda connection: PairingConfig(
            sc=True, mitm=True, bonding=True, delegate=_Delegate(self))
        device.sdp_service_records = {0x00010001: make_audio_source_service_sdp_records(0x00010001)}
        listener = Listener.for_device(device, version=(1, 3))
        listener.on("connection", self._on_incoming_avdtp)
        device.on("connection", self._on_acl_connection)
        await device.power_on()
        return device

    # ------------------------------------------------------------------ connections
    async def _try_connect(self, address: str) -> None:
        try:
            connection = await self.device.connect(classic_address(address), transport=PhysicalTransport.BR_EDR,
                                                   timeout=10)
        except Exception as error:
            # Say it once per stretch of failures (it retries every RECONNECT_SECONDS), not every time
            if self._unreachable != address:
                self._unreachable = address
                name = self.settings.devices.get(address, address)
                self._changed(f"Can't reach {name} right now ({error}); it will connect when it's on and in range")
            else:
                logger.debug(f"connect {address} failed: {error}")
            return
        self._unreachable = None
        await self._setup(connection, initiator=True)

    def _on_acl_connection(self, connection) -> None:
        if connection.transport != PhysicalTransport.BR_EDR or self.connection is not None:
            return
        address = str(connection.peer_address).split("/")[0]
        if address not in self.settings.devices:
            return  # not one of ours; let it be (pairing goes through pair())
        asyncio.ensure_future(self._setup(connection, initiator=False))

    def _on_incoming_avdtp(self, protocol: Protocol) -> None:
        # Headphones that open signalling themselves: drive the stream from our side over their channel
        self._incoming_protocol = protocol

    async def _setup(self, connection, initiator: bool) -> None:
        # One setup per connection: an outgoing connect also fires the "connection" event, and two
        # setups would open two AVDTP sessions, which speakers answer by refusing the audio channel
        if self.connection is not None:
            return
        self.connection = connection
        address = str(connection.peer_address).split("/")[0]
        name = self.settings.devices.get(address, address)
        self.status.device, self.status.device_name = address, name
        connection.on("disconnection", lambda reason: self._on_disconnection(connection, reason))
        try:
            await connection.authenticate()
            await connection.encrypt()
            await self._start_stream(connection, initiator)
        except NotASpeaker as error:
            # e.g. a TV that sends its sound to headphones: it will never take ours, so stop trying
            self.settings.devices.pop(address, None)
            if self.settings.last_device == address:
                self.settings.last_device = None
            self.settings.save()
            self._want_device = None
            self._changed(f"{name} {error}")
            try:
                await connection.disconnect()
            except Exception:
                pass
        except Exception as error:
            logger.exception("stream setup failed")
            self._changed(f"Couldn't start audio on {name}: {error}")
            try:
                await connection.disconnect()
            except Exception:
                pass

    def _on_disconnection(self, connection, reason) -> None:
        if connection is not self.connection:
            return
        name = self.status.device_name
        self._teardown_capture()
        self.connection = self.stream = None
        self.status.device = self.status.device_name = self.status.streaming = None
        self.status.codecs = []
        self._changed(f"{name} disconnected")
        if self._wake:
            self._wake.set()

    async def _start_stream(self, connection, initiator: bool = True) -> None:
        protocol = getattr(self, "_incoming_protocol", None)
        if protocol is not None and protocol.l2cap_channel.connection is not connection:
            protocol = None
        if protocol is None and not initiator:
            # The device connected to us: it usually opens AVDTP signalling itself; use its channel
            # rather than opening a second one (two sessions make speakers refuse the audio channel)
            for _ in range(20):
                await asyncio.sleep(0.1)
                protocol = getattr(self, "_incoming_protocol", None)
                if protocol is not None and protocol.l2cap_channel.connection is connection:
                    break
                protocol = None
        if protocol is None:
            version = await find_avdtp_service_with_connection(connection)
            if not version:
                raise NotASpeaker("has no Bluetooth audio playback, so it can't play the PC's sound")
            protocol = await Protocol.connect(connection, version)
        endpoints = list(await protocol.discover_remote_endpoints())
        if not any(e.tsep == AVDTP_TSEP_SNK for e in endpoints):
            raise NotASpeaker(
                "only sends audio (it's a Bluetooth audio source, like a TV that plays to headphones), so it "
                "can't play the PC's sound. If it has a \"Bluetooth speaker\" or \"sound receive\" mode, "
                "turn that on and pair it again.")
        sinks = selection.find_sinks(protocol)
        self.status.codecs = [c.label for c in selection.CODECS if c.key in sinks]
        key = selection.choose(sinks, self.settings.codec)
        if key is None:
            wanted = "a codec" if self.settings.codec == "auto" else selection.BY_KEY[self.settings.codec].label
            raise RuntimeError(f"it doesn't support {wanted} Bluetooth Multicodec can send")

        caps = selection.codec_info(sinks[key])
        self.capture = self._open_capture()
        rate = self.capture.output_rate(selection.sample_rates(key, caps))
        configuration = selection.configure(key, caps, rate)
        pump = LivePump(self.device.host, self.capture, rate,
                        selection.session_factory(key, configuration, rate, self.settings.ldac_quality),
                        on_stats=self._on_stats)
        source = protocol.add_source(configuration, pump)
        self.stream = await protocol.create_stream(source, sinks[key])
        await self.stream.start()
        self.protocol = protocol
        self.settings.last_device = self._want_device = self.status.device
        self.settings.save()
        self.status.streaming = selection.BY_KEY[key].label
        self._changed(f"Streaming {selection.BY_KEY[key].label} to {self.status.device_name}")

    def _open_capture(self):
        if self.settings.source == "system":
            return capture.SystemAudioCapture(mute_speakers=self.settings.mute_speakers)
        return capture.DeviceCapture(self.settings.source)

    def _teardown_capture(self) -> None:
        if self.capture:
            try:
                self.capture.close()
            except Exception:
                logger.exception("closing capture")
            self.capture = None

    def _on_stats(self, mode, kbps, backlog_ms, dropped) -> None:
        self.status.streaming, self.status.kbps = mode, kbps
        self._changed()

    # ------------------------------------------------------------------ requests
    async def _connect_request(self, address: str) -> None:
        if self.connection is not None:
            await self._disconnect_request()
        self._want_device = address
        self._wake.set()

    async def _disconnect_request(self) -> None:
        self._want_device = None
        if self.connection is not None:
            await self.connection.disconnect()

    async def _restream(self) -> None:
        if self.connection is not None:
            address = self.status.device
            await self.connection.disconnect()
            self._want_device = address
            self._wake.set()

    async def _scan(self, seconds: int, fresh: bool = True) -> None:
        if self.device is None or self.status.scanning:
            return
        self.status.scanning = True
        classic = True
        if fresh:
            self.status.found, self.status.found_kind, self.status.found_rssi = {}, {}, {}
            self._changed("Searching for headphones, speakers, mice and keyboards... (put them in pairing mode)")

        def add(key, name, kind, rssi=None):
            if rssi is not None:
                self.status.found_rssi[key] = rssi
            if (name and self.status.found.get(key) != name) or key not in self.status.found:
                self.status.found[key] = name or key
                self.status.found_kind[key] = kind
                self._changed_soon()  # adverts repeat many times a second; batch the tray refreshes

        def on_inquiry(address, class_of_device, data, rssi):
            major = (class_of_device >> 8) & 0x1F
            kind = {0x04: "audio", 0x05: "hid"}.get(major)  # Audio/Video, Peripheral
            if kind:
                key = str(address).split("/")[0]
                self.status.found_class[key] = class_of_device
                add(key, advertised_name(data), kind, rssi)

        def on_advertisement(advertisement):
            if is_hid_advertisement(advertisement.data):
                add(str(advertisement.address), advertised_name(advertisement.data), "hid-le", advertisement.rssi)

        self.hid.paused = True
        self.device.on("inquiry_result", on_inquiry)
        self.device.on("advertisement", on_advertisement)
        try:
            if self.device.scanning:
                await self.device.stop_scanning()
            if self.device.le_enabled:
                await self.device.start_scanning(active=True)
            # A Classic inquiry takes most of the radio's time and makes streamed audio stutter: repeat
            # (non-fresh) searches skip it while music plays; LE scanning (mice, keyboards) is cheap
            classic = fresh or self.stream is None
            if classic:
                await self.device.start_discovery()
            await asyncio.sleep(seconds)
        finally:
            # Never leave a full-duty scan running: it starves the radio while audio streams
            for stop in (self.device.stop_discovery if classic else None,
                         self.device.stop_scanning if self.device.le_enabled else None):
                if stop is None:
                    continue
                try:
                    await stop()
                except Exception:
                    pass
            self.device.remove_listener("inquiry_result", on_inquiry)
            self.device.remove_listener("advertisement", on_advertisement)
            self.hid.paused = False
            self.status.scanning = False
            if fresh:
                self._changed(f"Found {len(self.status.found)} device(s)")
            else:
                self._changed_soon()

    async def _pair(self, address: str, name: str | None) -> None:
        kind = self.status.found_kind.get(address, "audio")
        name = name or self.status.found.get(address, address)
        if kind != "audio":
            self._changed(f"Pairing with {name}...")
            try:
                if kind == "hid-le":
                    await self.hid.pair_le(address, name)
                else:
                    await self.hid.pair_classic(address, name, self.status.found_class.get(address, 0))
            except Exception as error:
                logger.exception("HID pairing failed")
                self.settings.hid_devices.pop(address, None)
                self.settings.save()
                self._changed(f"Couldn't pair {name}: {error}")
            return
        if self.connection is not None:
            await self._disconnect_request()
        try:
            await self.device.keystore.delete(str(classic_address(address)))
        except (KeyError, AttributeError):
            pass
        self.settings.devices[address] = name or self.status.found.get(address, address)
        self.settings.save()
        self._changed(f"Pairing with {self.settings.devices[address]}...")
        await self._connect_request(address)

    async def _forget(self, address: str) -> None:
        if self.hid.is_hid(address):
            await self.hid.forget(address)
            self._changed("Device removed")
            return
        if self.status.device == address:
            await self._disconnect_request()
        try:
            await self.device.keystore.delete(str(classic_address(address)))
        except (KeyError, AttributeError):
            pass
        self.settings.devices.pop(address, None)
        if self.settings.last_device == address:
            self.settings.last_device = None
        self.settings.save()
        self._changed("Device removed")

    async def _shutdown(self) -> None:
        self._want_device = None
        if self.connection is not None:
            try:
                await self.connection.disconnect()
            except Exception:
                pass
        self._teardown_capture()
