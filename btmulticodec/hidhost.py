"""Mice and keyboards on the Bluetooth adapter Bluetooth Multicodec owns.

Needed when the app runs on the PC's own Bluetooth (no dongle): Windows has no Bluetooth then, so
we act as the HID host and feed input to Windows through inject.Injector.

- Bluetooth Low Energy (most modern mice/keyboards): HID over GATT. We connect as central, pair or
  re-encrypt, read the Report Map and subscribe to input Report characteristics. Bonded devices
  that wake up advertise; a low-duty background scan spots them and reconnects.
- Classic (BR/EDR) HID: the device connects to us on L2CAP PSMs 0x11/0x13 and sends DATA|INPUT
  reports on the interrupt channel; the report descriptor comes from SDP at pairing time.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bumble import hci, l2cap
from bumble.core import UUID, AdvertisingData, PhysicalTransport
from bumble.device import Device, Peer
from bumble.sdp import Client as SdpClient

from .hidreport import BOOT_KEYBOARD, BOOT_MOUSE, ReportDescriptor
from .inject import Injector

logger = logging.getLogger(__name__)

HID_CONTROL_PSM, HID_INTERRUPT_PSM = 0x0011, 0x0013
HID_SERVICE = UUID.from_16_bits(0x1812)
REPORT_MAP = UUID.from_16_bits(0x2A4B)
REPORT = UUID.from_16_bits(0x2A4D)
PROTOCOL_MODE = UUID.from_16_bits(0x2A4E)
REPORT_REFERENCE = UUID.from_16_bits(0x2908)
CLASSIC_HID_SERVICE = UUID.from_16_bits(0x1124)
SDP_HID_DESCRIPTOR_LIST = 0x0206
HIDP_DATA_INPUT = 0xA1
HIDP_SET_PROTOCOL_BOOT = 0x70
REPORT_TYPE_INPUT = 1
APPEARANCE_HID_CATEGORY = 0x0F  # appearance >> 6: keyboard, mouse, joystick, ...
SCAN_INTERVAL_MS, SCAN_WINDOW_MS = 240, 48  # 20% duty: spots waking devices without starving audio


def is_hid_advertisement(data: AdvertisingData) -> bool:
    for kind in (AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
                 AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS):
        if any(uuid == HID_SERVICE for uuid in (data.get(kind) or [])):
            return True
    appearance = data.get(AdvertisingData.APPEARANCE)
    return appearance is not None and (int(appearance) >> 6) == APPEARANCE_HID_CATEGORY


def advertised_name(data: AdvertisingData) -> str | None:
    return data.get(AdvertisingData.COMPLETE_LOCAL_NAME) or data.get(AdvertisingData.SHORTENED_LOCAL_NAME)


@dataclass
class Link:
    address: str
    connection: object
    descriptor: ReportDescriptor


class HidHost:
    def __init__(self, engine):
        self.engine = engine  # for settings, status changes and the injector
        self.device: Device | None = None
        self.injector: Injector | None = None
        self.links: dict[str, Link] = {}
        self.connecting: set[str] = set()
        self.scan_task: asyncio.Task | None = None
        self.paused = False  # while the user-driven "Add a device" scan runs

    @property
    def settings(self):
        return self.engine.settings

    def known(self) -> dict[str, dict]:
        return self.settings.hid_devices

    # ------------------------------------------------------------------ lifecycle
    def attach(self, device: Device) -> None:
        self.device = device
        self.injector = Injector(asyncio.get_running_loop())
        device.create_l2cap_server(l2cap.ClassicChannelSpec(HID_CONTROL_PSM), self._on_classic_channel)
        device.create_l2cap_server(l2cap.ClassicChannelSpec(HID_INTERRUPT_PSM), self._on_classic_channel)
        device.on("advertisement", self._on_advertisement)
        self.scan_task = asyncio.ensure_future(self._reconnect_loop())

    def detach(self) -> None:
        if self.scan_task:
            self.scan_task.cancel()
        for address in list(self.links):
            self.injector.release_all(address)
        self.links.clear()
        self.device = None

    def is_hid(self, address: str) -> bool:
        return address in self.known()

    def connected(self) -> set[str]:
        return set(self.links)

    # ------------------------------------------------------------------ LE (HID over GATT)
    async def _reconnect_loop(self) -> None:
        """Keep a low-duty passive scan running while a bonded LE device is away."""
        while True:
            await asyncio.sleep(2)
            device = self.device
            if device is None:
                continue
            missing = [a for a, info in self.known().items()
                       if info.get("le") and a not in self.links and a not in self.connecting]
            want_scan = bool(missing) and not self.paused
            try:
                if want_scan and not device.scanning:
                    await device.start_scanning(active=False, scan_interval=SCAN_INTERVAL_MS,
                                                scan_window=SCAN_WINDOW_MS, filter_duplicates=False)
                elif not want_scan and device.scanning and not self.paused:
                    await device.stop_scanning()
            except Exception as error:
                logger.debug(f"background scan: {error}")

    def _identity(self, address: hci.Address) -> str:
        if address.is_resolvable and self.device and self.device.address_resolver:
            resolved = self.device.address_resolver.resolve(address)
            if resolved:
                return str(resolved)
        return str(address)

    def _on_advertisement(self, advertisement) -> None:
        if self.paused:
            return  # the user's "Add a device" scan owns the radio; don't cut it short
        identity = self._identity(advertisement.address)
        info = self.known().get(identity)
        if info and info.get("le") and identity not in self.links and identity not in self.connecting:
            # Claim it now: a burst of advertisements arrives before the task first runs
            self.connecting.add(identity)
            asyncio.ensure_future(self.connect_le(identity, advertisement.address))

    async def connect_le(self, identity: str, current: hci.Address | None = None, pairing: bool = False) -> bool:
        """Connect (and pair, when pairing) an LE HID device. When pairing, `identity` may be the
        private address seen while scanning; the entry is re-keyed to the real identity once known."""
        self.connecting.add(identity)
        device = self.device
        connection = None
        key = identity
        try:
            if device.scanning:
                await device.stop_scanning()
            connection = await device.connect(current or hci.Address(identity), transport=PhysicalTransport.LE,
                                              own_address_type=hci.OwnAddressType.PUBLIC, timeout=15)
            if pairing:
                await connection.pair()
                await device.refresh_resolving_list()
                # Privacy-enabled devices advertise from changing private addresses; the bond is stored
                # under their identity address, which is also what _on_advertisement resolves to
                resolved = self._identity(current or hci.Address(identity))
                if resolved != key:
                    self.known()[resolved] = self.known().pop(key)
                    self.settings.save()
                    key = resolved
            else:
                await connection.encrypt()
            descriptor = await self._subscribe_gatt(key, connection)
            self._linked(key, connection, descriptor)
            return True
        except Exception as error:
            logger.info(f"LE HID {key}: {error}")
            if connection is not None:
                # A half-set-up link keeps the device from advertising, so it would never come back
                try:
                    await connection.disconnect()
                except Exception:
                    pass
            if pairing:
                raise
            return False
        finally:
            self.connecting.discard(identity)
            self.connecting.discard(key)

    async def _subscribe_gatt(self, identity: str, connection) -> ReportDescriptor:
        peer = Peer(connection)
        await peer.discover_services([HID_SERVICE])
        services = peer.get_services_by_uuid(HID_SERVICE)
        if not services:
            raise RuntimeError("no HID service")
        service = services[0]
        characteristics = await service.discover_characteristics()
        report_map = next((c for c in characteristics if c.uuid == REPORT_MAP), None)
        if report_map is None:
            raise RuntimeError("no HID report map")
        descriptor = ReportDescriptor(bytes(await report_map.read_value()))
        for c in characteristics:
            if c.uuid == PROTOCOL_MODE:
                try:
                    await c.write_value(bytes([1]))  # report protocol
                except Exception:
                    pass
        subscribed = 0
        for c in characteristics:
            if c.uuid != REPORT:
                continue
            await c.discover_descriptors()
            reference = next((d for d in c.descriptors if d.type == REPORT_REFERENCE), None)
            report_id, report_type = 0, REPORT_TYPE_INPUT
            if reference is not None:
                value = bytes(await reference.read_value())
                report_id, report_type = value[0], value[1]
            if report_type != REPORT_TYPE_INPUT:
                continue
            await c.subscribe(lambda value, rid=report_id: self._on_report(identity, bytes(value), rid))
            subscribed += 1
        if not subscribed:
            raise RuntimeError("no input reports")
        return descriptor

    # ------------------------------------------------------------------ Classic HID
    def _on_classic_channel(self, channel: l2cap.ClassicChannel) -> None:
        connection = channel.connection
        address = str(connection.peer_address).split("/")[0]
        if address not in self.known():
            return
        info = self.known()[address]
        if not connection.is_encrypted:
            # Anyone can claim a paired keyboard's address; only the bonded device has the link key.
            # Reports are dropped (_on_classic_pdu) until this succeeds.
            asyncio.ensure_future(self._secure_classic(connection, address))
        if channel.psm == HID_INTERRUPT_PSM:
            channel.sink = lambda pdu: self._on_classic_pdu(address, pdu)
            self._linked(address, connection, self._classic_descriptor(info))
        else:
            channel.sink = lambda pdu: None
            if not info.get("descriptor"):
                # Devices come back in report protocol; put them in the boot protocol we decode
                channel.on("open", lambda: channel.write(bytes([HIDP_SET_PROTOCOL_BOOT])))

    async def _secure_classic(self, connection, address: str) -> None:
        try:
            await connection.authenticate()
            await connection.encrypt()
        except Exception as error:
            logger.warning(f"classic HID {address} failed authentication ({error}); dropping it")
            try:
                await connection.disconnect()
            except Exception:
                pass

    def _classic_descriptor(self, info: dict) -> ReportDescriptor:
        if info.get("descriptor"):
            return ReportDescriptor(bytes.fromhex(info["descriptor"]))
        return ReportDescriptor(BOOT_KEYBOARD if info.get("keyboard") else BOOT_MOUSE)

    def _on_classic_pdu(self, address: str, pdu: bytes) -> None:
        link = self.links.get(address)
        if link is None or not link.connection.is_encrypted:
            return
        if pdu and pdu[0] == HIDP_DATA_INPUT:
            report = pdu[1:]
            if not self.known().get(address, {}).get("descriptor") and report[:1] in (b"\x01", b"\x02"):
                report = report[1:]  # Bluetooth boot reports carry a report ID (1 keyboard, 2 mouse)
            self._on_report(address, report, None)

    async def pair_classic(self, address: str, name: str, class_of_device: int) -> None:
        from .engine import classic_address
        device = self.device
        peer = classic_address(address)
        try:
            await device.keystore.delete(str(peer))
        except (KeyError, AttributeError):
            pass
        connection = await device.connect(peer, transport=PhysicalTransport.BR_EDR, timeout=15)
        try:
            await self._pair_classic_connected(connection, address, name, class_of_device)
        except Exception:
            self.known().pop(address, None)
            try:
                await connection.disconnect()
            except Exception:
                pass
            raise

    async def _pair_classic_connected(self, connection, address: str, name: str, class_of_device: int) -> None:
        await connection.authenticate()
        await connection.encrypt()
        descriptor_hex = None
        try:
            async with SdpClient(connection) as sdp:
                results = await sdp.search_attributes([CLASSIC_HID_SERVICE], [SDP_HID_DESCRIPTOR_LIST])
            for attributes in results:
                for attribute in attributes:
                    if attribute.id == SDP_HID_DESCRIPTOR_LIST:
                        # SEQUENCE { SEQUENCE { UINT8 type (0x22 = report), TEXT descriptor } }
                        for entry in attribute.value.value:
                            kind, data = entry.value[0].value, entry.value[1].value
                            if kind == 0x22:
                                descriptor_hex = bytes(data).hex()
        except Exception as error:
            logger.info(f"SDP for {address} failed ({error}); using boot protocol")
        keyboard = bool(class_of_device & 0x40)  # minor class: keyboard bit
        self.known()[address] = {"name": name, "le": False, "descriptor": descriptor_hex, "keyboard": keyboard}
        self.settings.save()
        control = await connection.create_l2cap_channel(l2cap.ClassicChannelSpec(HID_CONTROL_PSM))
        control.sink = lambda pdu: None
        if descriptor_hex is None:
            control.write(bytes([HIDP_SET_PROTOCOL_BOOT]))
        interrupt = await connection.create_l2cap_channel(l2cap.ClassicChannelSpec(HID_INTERRUPT_PSM))
        interrupt.sink = lambda pdu: self._on_classic_pdu(address, pdu)
        self._linked(address, connection, self._classic_descriptor(self.known()[address]))

    async def pair_le(self, identity: str, name: str) -> None:
        self.known()[identity] = {"name": name, "le": True}
        self.settings.save()
        try:
            await self.device.keystore.delete(identity)
        except (KeyError, AttributeError):
            pass
        await self.connect_le(identity, pairing=True)

    async def forget(self, address: str) -> None:
        link = self.links.get(address)
        if link:
            await link.connection.disconnect()
        self.known().pop(address, None)
        self.settings.save()
        for key in (address, address.split("/")[0] + "/P"):
            try:
                await self.device.keystore.delete(key)
            except (KeyError, AttributeError):
                pass

    # ------------------------------------------------------------------ shared
    def _linked(self, address: str, connection, descriptor: ReportDescriptor) -> None:
        if address in self.links and self.links[address].connection is connection:
            return
        self.links[address] = Link(address, connection, descriptor)
        name = self.known().get(address, {}).get("name", address)

        def on_disconnection(reason):
            if self.links.get(address) and self.links[address].connection is connection:
                del self.links[address]
                self.injector.release_all(address)
                self.engine._changed(f"{name} disconnected")
        connection.on("disconnection", on_disconnection)
        self.engine._changed(f"{name} connected")

    def _on_report(self, address: str, report: bytes, report_id: int | None) -> None:
        link = self.links.get(address)
        if link is None:
            return
        state = link.descriptor.decode(report, report_id)
        if state is not None:
            self.injector.apply(address, state)
