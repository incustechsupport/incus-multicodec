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
from bumble.avdtp import Listener, Protocol, find_avdtp_service_with_connection
from bumble.core import AdvertisingData, PhysicalTransport
from bumble.device import Device, DeviceConfiguration
from bumble.transport import open_transport

from . import adapters, capture, selection
from .settings import KEYS_FILE, STATE_DIR, Settings
from .stream import LivePump

logger = logging.getLogger(__name__)

APP_DIR = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).resolve().parent.parent))
FIRMWARE_DIR = APP_DIR / "firmware"
CLASS_OF_DEVICE = 0x28010C  # computer/laptop, Audio + Capturing service bits
ADAPTER_POLL_SECONDS = 5
RECONNECT_SECONDS = 20


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

    # ------------------------------------------------------------------ public (any thread)
    def start(self) -> None:
        self.thread.start()

    def connect(self, address: str) -> None:
        self._call(self._connect_request(address))

    def disconnect(self) -> None:
        self._call(self._disconnect_request())

    def scan(self, seconds: int = 10) -> None:
        self._call(self._scan(seconds))

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

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self) -> None:
        self._wake = asyncio.Event()
        os.environ.setdefault("BUMBLE_INTEL_FIRMWARE_DIR", str(FIRMWARE_DIR / "intel"))
        os.environ.setdefault("BUMBLE_RTK_FIRMWARE_DIR", str(FIRMWARE_DIR / "realtek"))
        while True:
            spec = self.transport_spec or await self._wait_for_adapter()
            try:
                await self._run_adapter(spec)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("adapter session ended")
                self.status.adapter = None
                self._changed(f"Bluetooth dongle stopped: {error}")
            self.device = self.connection = self.stream = None
            await asyncio.sleep(ADAPTER_POLL_SECONDS)

    async def _wait_for_adapter(self) -> str:
        while True:
            found = await asyncio.to_thread(adapters.ours)
            if found:
                self.status.adapter_problem = None
                self.status.adapter = found[0].name
                return found[0].transport
            if self.status.adapter_problem is None:
                self.status.adapter_problem = "No dongle set up for Bluetooth Multicodec"
                self._changed()
            await asyncio.sleep(ADAPTER_POLL_SECONDS)

    async def _run_adapter(self, spec: str) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        async with await open_transport(spec) as transport:
            config = DeviceConfiguration.from_dict({
                "name": os.environ.get("COMPUTERNAME", "Windows PC"),
                "class_of_device": CLASS_OF_DEVICE,
                "classic_enabled": True,
                "le_enabled": False,
                "keystore": f"JsonKeyStore:{KEYS_FILE}",
            })
            device = Device.from_config_with_hci(config, transport.source, transport.sink)
            device.sdp_service_records = {0x00010001: make_audio_source_service_sdp_records(0x00010001)}
            listener = Listener.for_device(device, version=(1, 3))
            listener.on("connection", self._on_incoming_avdtp)
            device.on("connection", self._on_acl_connection)
            await device.power_on()
            await device.set_connectable(True)
            self.device = device
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

    # ------------------------------------------------------------------ connections
    async def _try_connect(self, address: str) -> None:
        try:
            connection = await self.device.connect(classic_address(address), transport=PhysicalTransport.BR_EDR,
                                                   timeout=10)
        except Exception as error:
            logger.debug(f"connect {address} failed: {error}")
            return
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
        if self.connection is not None and self.connection is not connection:
            return
        self.connection = connection
        address = str(connection.peer_address).split("/")[0]
        name = self.settings.devices.get(address, address)
        self.status.device, self.status.device_name = address, name
        connection.on("disconnection", lambda reason: self._on_disconnection(connection, reason))
        try:
            await connection.authenticate()
            await connection.encrypt()
            await self._start_stream(connection)
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

    async def _start_stream(self, connection) -> None:
        self._incoming_protocol = None
        await asyncio.sleep(0.5)  # give headphones that open AVDTP themselves a moment
        protocol = getattr(self, "_incoming_protocol", None)
        if protocol is None or protocol.l2cap_channel.connection is not connection:
            version = await find_avdtp_service_with_connection(connection)
            if not version:
                raise RuntimeError("it has no A2DP audio sink")
            protocol = await Protocol.connect(connection, version)
        await protocol.discover_remote_endpoints()
        sinks = selection.find_sinks(protocol)
        self.status.codecs = [c.label for c in selection.CODECS if c.key in sinks]
        key = selection.choose(sinks, self.settings.codec)
        if key is None:
            raise RuntimeError(f"it doesn't support {selection.BY_KEY[self.settings.codec].label}")

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

    async def _scan(self, seconds: int) -> None:
        if self.device is None or self.status.scanning:
            return
        self.status.scanning, self.status.found = True, {}
        self._changed("Searching for headphones and speakers... (put them in pairing mode)")

        def on_result(address, class_of_device, data, rssi):
            major = (class_of_device >> 8) & 0x1F
            if major != 0x04:  # Audio/Video only
                return
            name = data.get(AdvertisingData.COMPLETE_LOCAL_NAME) or data.get(AdvertisingData.SHORTENED_LOCAL_NAME)
            key = str(address).split("/")[0]
            if name or key not in self.status.found:
                self.status.found[key] = name or key
                self._changed()

        self.device.on("inquiry_result", on_result)
        try:
            await self.device.start_discovery()
            await asyncio.sleep(seconds)
            await self.device.stop_discovery()
        finally:
            self.device.remove_listener("inquiry_result", on_result)
            self.status.scanning = False
            self._changed(f"Found {len(self.status.found)} audio device(s)")

    async def _pair(self, address: str, name: str | None) -> None:
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
