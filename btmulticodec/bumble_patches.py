"""Small, targeted fixes to Bumble 0.0.236 behaviour. Imported once, before any adapter is opened."""

from __future__ import annotations

import dataclasses
import logging

from bumble.drivers import intel

logger = logging.getLogger(__name__)

# Intel TLV-generation controllers that Linux's btintel loads with the same TLV firmware download
# (btintel_bootloader_setup_tlv): 0x17 Typhoon Peak (AX210), 0x18 Solar (AX211/AX201 CNVi on 11th+
# gen), 0x19 Solar-F, 0x1B Magnetor, 0x1C Gale Peak (BE200), 0x1D/0x1E Blazar. Bumble's driver only
# lets 0x17/0x19/0x1C through ("a partial list"), although the firmware it ships names for, e.g.
# ibt-0040-* for Solar, are picked by CNVi/CNVR, not by variant. The variant is only used for that
# gate, so present the other TLV variants as Typhoon Peak to it.
TLV_VARIANTS = {0x17, 0x18, 0x19, 0x1B, 0x1C, 0x1D, 0x1E}
_original_read_device_info = intel.Driver.read_device_info


async def _read_device_info(self):
    info = await _original_read_device_info(self)
    hardware = info.get(intel.ValueType.HARDWARE_INFO)
    if hardware is not None and int(hardware.variant) in TLV_VARIANTS:
        logger.info(f"Intel controller: platform 0x{int(hardware.platform):02X}, variant "
                    f"0x{int(hardware.variant):02X}, CNVi 0x{info.get(intel.ValueType.CNVI, 0):04X}, "
                    f"CNVR 0x{info.get(intel.ValueType.CNVR, 0):04X}")
        if int(hardware.variant) not in (intel.HardwareVariant.TYPHOON_PEAK, intel.HardwareVariant.GARFIELD_PEAK,
                                         intel.HardwareVariant.GALE_PEAK):
            info[intel.ValueType.HARDWARE_INFO] = dataclasses.replace(
                hardware, variant=intel.HardwareVariant.TYPHOON_PEAK)
    return info


intel.Driver.read_device_info = _read_device_info


# Intel bootloaders take the Secure Send command (0xFC09, used to stream firmware) on the BULK OUT
# endpoint, not the control endpoint other HCI commands use. Linux's btusb_send_frame_intel does
# exactly this ("needs to be sent down the bulk endpoint, but is otherwise handled as a command").
# Bumble's USB transport always uses the control endpoint, so newer Intel controllers (e.g. Magnetor
# 0x1B in AX211-class CNVi parts) never answer the first firmware chunk and the load hangs forever.
# Re-tag those commands so the transport's bulk path sends the identical bytes (opcode, length,
# parameters); the controller's replies already come back on bulk IN, which Bumble's driver expects.
from bumble import hci  # noqa: E402
from bumble.transport import usb as _usb  # noqa: E402

_INTEL_SECURE_SEND = bytes([hci.HCI_COMMAND_PACKET, 0x09, 0xFC])
_original_on_packet = _usb.UsbPacketSink.on_packet


def _on_packet(self, packet):
    if packet[:3] == _INTEL_SECURE_SEND:
        packet = bytes([hci.HCI_ACL_DATA_PACKET]) + bytes(packet[1:])
    return _original_on_packet(self, packet)


_usb.UsbPacketSink.on_packet = _on_packet


# The bootloader also answers those commands with HCI *events* on the BULK IN endpoint. Bumble feeds
# all bulk IN data to its ACL splitter, which reads e.g. Command Complete `0e 04 01 09 fc 00` as an ACL
# header with a 0x0901-byte length and waits for data that never comes. Linux's btusb_recv_bulk_intel
# treats bulk IN as events while the controller is in bootloader mode; do the same for the duration
# of the Intel firmware load. Bumble's Intel driver already parses these with HCI_Event.from_bytes,
# which ignores the leading packet-type byte, so they can travel as "ACL" packets.
import asyncio  # noqa: E402

_intel_firmware_loading = False
_EVENT_CODES = {0x0E, 0x0F, 0xFF}  # Command Complete, Command Status, vendor
_original_acl_feed = _usb.AclPacketSplitter.feed


def _acl_feed(self, data: bytes) -> None:
    if (_intel_firmware_loading and not self.packet and len(data) >= 2
            and data[0] in _EVENT_CODES and len(data) == 2 + data[1]):
        self.emit(bytes(data))
        return
    _original_acl_feed(self, data)


_usb.AclPacketSplitter.feed = _acl_feed

INTEL_FIRMWARE_TIMEOUT = 60  # seconds; Bumble waits forever for each step otherwise
_original_load_firmware = intel.Driver.load_firmware


async def _load_firmware(self):
    global _intel_firmware_loading
    _intel_firmware_loading = True
    try:
        return await asyncio.wait_for(_original_load_firmware(self), INTEL_FIRMWARE_TIMEOUT)
    except asyncio.TimeoutError:
        # Leave the controller where the next attempt can start cleanly
        try:
            await self.reboot_bootloader()
        except Exception:
            pass
        raise intel.DriverError("Intel firmware load timed out")
    finally:
        _intel_firmware_loading = False


intel.Driver.load_firmware = _load_firmware
