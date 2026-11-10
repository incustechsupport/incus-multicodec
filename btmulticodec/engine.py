"""The background engine: owns the Incus Multicodec dongle and keeps one audio sink connected and streaming.

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
from bumble.avdtp import AVDTP_TSEP_SNK, Listener, Protocol, State, find_avdtp_service_with_connection
from bumble.core import AdvertisingData, PhysicalTransport
from bumble.device import Device, DeviceConfiguration
from bumble.pairing import PairingConfig, PairingDelegate
from bumble.transport import open_transport

import usb1

from . import adapters, bumble_patches, capture, selection  # noqa: F401 (bumble_patches: applied on import)
from .hidhost import HidHost, advertised_name, is_hid_advertisement
from .settings import KEYS_FILE, STATE_DIR, Settings, system_adapter
from .stream import CaptureFailed, LivePump

logger = logging.getLogger(__name__)

APP_DIR = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).resolve().parent.parent))
FIRMWARE_DIR = APP_DIR / "firmware"
CLASS_OF_DEVICE = 0x28010C  # computer/laptop, Audio + Capturing service bits
ADAPTER_POLL_SECONDS = 5
RECONNECT_SECONDS = 20
# After a connection that came up but couldn't stream (or a stream that died), wait before trying
# again, doubling each time: otherwise a lasting failure pages the headphones every second or two
RETRY_FIRST_SECONDS = 5
RETRY_MAX_SECONDS = 300
# A stream counts as working (and the backoff is reset) once it has run this long: resetting it as
# soon as the stream starts let a stream that dies straight away retry every 5 s for ever
HEALTHY_STREAM_SECONDS = 15
# HCI errors meaning the headphones no longer have our pairing: retrying can't help
LOST_PAIRING_ERRORS = (hci.HCI_AUTHENTICATION_FAILURE_ERROR, hci.HCI_PIN_OR_KEY_MISSING_ERROR)


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
        self.pump: LivePump | None = None  # the running stream's packet pump
        self._failures: dict[str, int] = {}  # address -> setup/stream failures in a row
        self._retry_at: dict[str, float] = {}  # address -> loop time before which we don't reconnect
        self._lost = None  # the connection whose stream loss is already being handled
        self._capture_restarts: list[float] = []  # loop times of recent in-place capture restarts
        self._streaming_since: float | None = None  # loop time the current stream started

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
        name = getattr(coroutine, "__name__", "request")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)

        def log_failure(done) -> None:
            if not done.cancelled() and done.exception() is not None:
                logger.error(f"{name} failed", exc_info=done.exception())
        future.add_done_callback(log_failure)

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
                # Only reachable with a fixed transport (_wait_for_adapter waits while released):
                # wait for resume_adapter instead of spinning
                await asyncio.sleep(ADAPTER_POLL_SECONDS)
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
                    self._set_problem("No dongle set up for Incus Multicodec")
            await asyncio.sleep(ADAPTER_POLL_SECONDS)

    async def _release(self) -> None:
        self._released = True
        self._want_device = None
        if self.connection is not None:
            try:
                await self.connection.disconnect()
            except Exception:
                pass
        self._stop_pump()
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
                timeout = RECONNECT_SECONDS
                want = self._want_device
                if want and self.connection is None and not self.status.scanning:
                    wait = self._retry_at.get(want, 0) - self.loop.time()
                    if wait > 0:
                        timeout = min(timeout, wait)  # backing off after failures
                    else:
                        await self._try_connect(want)
                self._wake.clear()
                try:
                    await asyncio.wait_for(asyncio.shield(self._wake.wait()), timeout)
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
            if connection is not self.connection:
                # Another device took the slot while this link was coming up (we paged A, B connected
                # in): don't leave this ACL link open with nothing using it
                logger.info(f"dropping a second audio link to {connection.peer_address}")
                try:
                    await connection.disconnect()
                except Exception:
                    pass
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
        except hci.HCI_Error as error:
            if error.error_code in LOST_PAIRING_ERRORS:
                # The headphones forgot us (reset, or paired with too many devices): retrying only
                # fails again, so stop until the user pairs them again
                logger.warning(f"{name}: pairing lost ({error})")
                if self._want_device == address:
                    self._want_device = None
                self._changed(f"{name} no longer has this PC's pairing. Pair it again from Devices.")
            else:
                logger.exception("stream setup failed")
                self._changed(f"Couldn't start audio on {name}: {error}{self._note_failure(address)}")
            try:
                await connection.disconnect()
            except Exception:
                pass
        except Exception as error:
            logger.exception("stream setup failed")
            self._changed(f"Couldn't start audio on {name}: {error}{self._note_failure(address)}")
            try:
                await connection.disconnect()
            except Exception:
                pass

    def _note_failure(self, address: str) -> str:
        """Count a failed setup or a dead stream, and schedule the next attempt further out each time."""
        count = self._failures.get(address, 0) + 1
        self._failures[address] = count
        delay = min(RETRY_MAX_SECONDS, RETRY_FIRST_SECONDS * 2 ** (count - 1))
        self._retry_at[address] = self.loop.time() + delay
        return f" (trying again in {delay} s)" if delay < 60 else f" (trying again in {delay // 60} min)"

    def _clear_backoff(self, address: str | None) -> None:
        if address:
            self._failures.pop(address, None)
            self._retry_at.pop(address, None)

    def _stop_pump(self) -> None:
        pump, self.pump = self.pump, None
        if pump is not None:
            asyncio.ensure_future(pump.stop())

    def _on_stream_lost(self, connection, why: str, error: BaseException | None = None) -> None:
        """The stream ended without the link going down: the headphones closed or aborted it, the
        audio channel dropped, or encoding/capture failed. Restart through a fresh connection."""
        if connection is not self.connection or connection is None or self._lost is connection:
            return  # stale, or already being handled (a CLOSE is followed by the channel closing)
        address, name = self.status.device, self.status.device_name
        if isinstance(error, CaptureFailed) and self.stream is not None and self.pump is not None:
            now = self.loop.time()
            self._capture_restarts = [t for t in self._capture_restarts if now - t < 60] + [now]
            if len(self._capture_restarts) <= 3:
                asyncio.ensure_future(self._restart_capture(connection, error))
                return
            why = f"audio capture keeps failing: {error}"
        self._lost = connection
        logger.warning(f"stream to {name} ended: {why}")
        self._stop_pump()
        self._teardown_capture()
        self.status.streaming = None
        self._changed(f"Audio to {name} stopped ({why}){self._note_failure(address) if address else ''}")
        asyncio.ensure_future(self._drop(connection))

    async def _drop(self, connection) -> None:
        try:
            await connection.disconnect()
        except Exception:
            pass

    async def _restart_capture(self, connection, error: BaseException) -> None:
        """Capture died or the output device changed: reopen it and carry on with the same stream,
        if the new capture can feed the rate already negotiated with the headphones."""
        pump, stream = self.pump, self.stream
        logger.warning(f"restarting audio capture: {error}")
        await pump.stop()
        # No capture while we reopen it: a START from the headphones meanwhile must not start the pump
        # on the closed one (LivePump.start waits for a capture); we start it below if still streaming
        pump.capture = None
        self._teardown_capture()
        await asyncio.sleep(1)  # let Windows settle (service restart, device switch)
        if connection is not self.connection or self.stream is not stream or self._lost is connection:
            return
        try:
            new_capture = self._open_capture()  # on this thread, like the first open
            try:
                new_capture.output_rate([pump.sample_rate])  # raises if it can't feed the negotiated rate
            except Exception:
                new_capture.close()
                raise
        except Exception as reopen_error:
            logger.exception("couldn't reopen capture")
            self.pump = pump
            self._on_stream_lost(connection, f"audio capture failed: {reopen_error}")
            return
        self.capture = pump.capture = new_capture
        self.pump = pump
        channel = getattr(stream, "rtp_channel", None)
        if channel is None:
            self._on_stream_lost(connection, "the audio channel closed")
            return
        if stream.state == State.STREAMING:
            await pump.start(channel)  # a no-op if a START from the headphones already restarted it
        # (if they suspended meanwhile, their next START starts the pump with the new capture)
        self._changed(f"Audio capture restarted ({error})")

    def _on_disconnection(self, connection, reason) -> None:
        if connection is not self.connection:
            return
        name = self.status.device_name
        # Stop the pump first: with capture closed under it, it would wait for audio forever and keep
        # its encoder (and Media Foundation, for AAC) alive
        self._stop_pump()
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
            raise RuntimeError(f"it doesn't support {wanted} Incus Multicodec can send")

        caps = selection.codec_info(sinks[key])
        self.capture = self._open_capture()
        rate = self.capture.output_rate(selection.sample_rates(key, caps))
        configuration = selection.configure(key, caps, rate)
        pump = LivePump(self.device.host, self.capture, rate,
                        selection.session_factory(key, configuration, rate, self.settings.ldac_quality),
                        on_stats=self._on_stats,
                        on_failure=lambda error: self._on_stream_lost(connection, str(error), error))
        self.pump = pump
        source = protocol.add_source(configuration, pump)
        # Bumble's source only stops the pump on SUSPEND. When the headphones CLOSE or ABORT the stream
        # (multipoint handover, an error on their side), stop it ourselves and recover
        source.on("close", lambda: self._on_stream_lost(connection, "the headphones closed the stream"))
        source.on("abort", lambda: self._on_stream_lost(connection, "the headphones aborted the stream"))
        self.stream = await protocol.create_stream(source, sinks[key])
        await self.stream.start()
        # The audio channel closing on its own. (Bumble only wires its source's rtp_channel_close
        # event when the headphones open that channel; we open it, so listen on the channel itself.)
        # It also closes when the whole link drops: give the disconnection a moment to arrive, so
        # that case is handled as a plain disconnect
        channel = getattr(self.stream, "rtp_channel", None)
        if channel is not None:
            channel.on("close", lambda: self.loop.call_later(
                0.5, self._on_stream_lost, connection, "the audio channel closed"))
        self.protocol = protocol
        self.settings.last_device = self._want_device = self.status.device
        self._streaming_since = self.loop.time()  # backoff is cleared once it has run a while (_on_stats)
        self._capture_restarts = []
        self._lost = None
        self.settings.save()
        self.status.streaming = selection.BY_KEY[key].label
        self._changed(f"Streaming {selection.BY_KEY[key].label} to {self.status.device_name}")

    def _open_capture(self):
        if self.settings.source == "system":
            return capture.open_system_audio(mute_speakers=self.settings.mute_speakers)
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
        if (self._streaming_since is not None
                and self.loop.time() - self._streaming_since >= HEALTHY_STREAM_SECONDS):
            self._streaming_since = None
            self._clear_backoff(self.status.device)
        self._changed()

    # ------------------------------------------------------------------ requests
    async def _connect_request(self, address: str) -> None:
        if self.connection is not None:
            await self._disconnect_request()
        self._want_device = address
        self._clear_backoff(address)  # the user asked: try now, whatever failed before
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
            self._clear_backoff(address)
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
                # Bumble's default scans 100% of the time, which starves streamed audio of radio time.
                # A quarter still finds devices in pairing mode (they advertise quickly) within seconds
                if self.stream is not None:
                    await self.device.start_scanning(active=True, scan_interval=120, scan_window=30)
                else:
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
        self._stop_pump()
        self._teardown_capture()
