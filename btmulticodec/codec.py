"""A2DP codec information elements for the vendor codecs Incus Multicodec speaks: LDAC, aptX, aptX HD, aptX LL.

Byte layouts follow the codec capability structs in PipeWire's spa/plugins/bluez5/a2dp-codec-caps.h.
"""

from __future__ import annotations

import dataclasses
import enum
import struct
from typing import ClassVar

from bumble import a2dp


@dataclasses.dataclass
class LdacMediaCodecInformation(a2dp.VendorSpecificMediaCodecInformation):
    vendor_id: int = dataclasses.field(init=False, repr=False)
    codec_id: int = dataclasses.field(init=False, repr=False)
    value: bytes = dataclasses.field(init=False, repr=False)
    sampling_frequency: SamplingFrequency
    channel_mode: ChannelMode

    class SamplingFrequency(enum.IntFlag):
        SF_192000 = 0x01
        SF_176400 = 0x02
        SF_96000 = 0x04
        SF_88200 = 0x08
        SF_48000 = 0x10
        SF_44100 = 0x20

    class ChannelMode(enum.IntFlag):
        STEREO = 0x01
        DUAL_CHANNEL = 0x02
        MONO = 0x04

    NAME: ClassVar[str] = "LDAC"
    VENDOR_ID: ClassVar[int] = 0x0000012D  # Sony
    CODEC_ID: ClassVar[int] = 0x00AA

    HZ: ClassVar[dict[int, int]] = {
        0x20: 44100, 0x10: 48000, 0x08: 88200, 0x04: 96000, 0x02: 176400, 0x01: 192000
    }

    def __post_init__(self) -> None:
        self.vendor_id = self.VENDOR_ID
        self.codec_id = self.CODEC_ID
        self.value = bytes([self.sampling_frequency & 0x3F, self.channel_mode & 0x07])

    @classmethod
    def from_bytes(cls, data: bytes) -> LdacMediaCodecInformation:
        return cls(cls.SamplingFrequency(data[0] & 0x3F), cls.ChannelMode(data[1] & 0x07))

    @property
    def sample_rates(self) -> list[int]:
        return [hz for bit, hz in self.HZ.items() if self.sampling_frequency & bit]

    def configuration(self, hz: int) -> LdacMediaCodecInformation:
        bit = next(b for b, rate in self.HZ.items() if rate == hz)
        return type(self)(self.SamplingFrequency(bit), self.ChannelMode.STEREO)

    def __str__(self) -> str:
        return f"{self.NAME}({_rates(self.sample_rates)}, {self.channel_mode!r})"


@dataclasses.dataclass
class AptxMediaCodecInformation(a2dp.VendorSpecificMediaCodecInformation):
    """aptX: one byte, sampling frequency in the high nibble, channel mode in the low nibble."""

    vendor_id: int = dataclasses.field(init=False, repr=False)
    codec_id: int = dataclasses.field(init=False, repr=False)
    value: bytes = dataclasses.field(init=False, repr=False)
    frequency: int  # 4-bit flags, see HZ
    channel_mode: int  # 4-bit flags: 0x01 mono, 0x02 stereo

    NAME: ClassVar[str] = "aptX"
    VENDOR_ID: ClassVar[int] = 0x0000004F  # APT Ltd
    CODEC_ID: ClassVar[int] = 0x0001
    HZ: ClassVar[dict[int, int]] = {0x08: 16000, 0x04: 32000, 0x02: 44100, 0x01: 48000}
    STEREO: ClassVar[int] = 0x02

    def __post_init__(self) -> None:
        self.vendor_id = self.VENDOR_ID
        self.codec_id = self.CODEC_ID
        self.value = self._base_byte() + self._extra_bytes()

    def _base_byte(self) -> bytes:
        return bytes([(self.frequency & 0x0F) << 4 | (self.channel_mode & 0x0F)])

    def _extra_bytes(self) -> bytes:
        return b""

    @classmethod
    def from_bytes(cls, data: bytes):
        return cls(data[0] >> 4, data[0] & 0x0F)

    @property
    def sample_rates(self) -> list[int]:
        return [hz for bit, hz in self.HZ.items() if self.frequency & bit]

    def configuration(self, hz: int):
        bit = next(b for b, rate in self.HZ.items() if rate == hz)
        return type(self)(bit, self.STEREO)

    def __str__(self) -> str:
        return f"{self.NAME}({_rates(self.sample_rates)})"


@dataclasses.dataclass
class AptxHdMediaCodecInformation(AptxMediaCodecInformation):
    """aptX HD: the aptX byte followed by 4 reserved bytes."""

    NAME: ClassVar[str] = "aptX HD"
    VENDOR_ID: ClassVar[int] = 0x000000D7  # Qualcomm Technologies International
    CODEC_ID: ClassVar[int] = 0x0024

    def _extra_bytes(self) -> bytes:
        return bytes(4)


@dataclasses.dataclass
class AptxLlMediaCodecInformation(AptxMediaCodecInformation):
    """aptX Low Latency: aptX byte, a flags byte, and (when has_new_caps) buffer-level tuning."""

    bidirectional: bool = False
    new_caps: bytes | None = None  # the 9 extended bytes, when the sink advertises them

    NAME: ClassVar[str] = "aptX LL"
    VENDOR_ID: ClassVar[int] = 0x0000000A  # CSR
    CODEC_ID: ClassVar[int] = 0x0002

    # PipeWire's defaults for the extended configuration
    TARGET_CODEC_LEVEL: ClassVar[int] = 180
    INITIAL_CODEC_LEVEL: ClassVar[int] = 360
    SRA_MAX_RATE: ClassVar[int] = 50
    SRA_AVG_TIME: ClassVar[int] = 1
    GOOD_WORKING_LEVEL: ClassVar[int] = 180

    def _extra_bytes(self) -> bytes:
        flags = (1 if self.bidirectional else 0) | (0x02 if self.new_caps is not None else 0)
        return bytes([flags]) + (self.new_caps or b"")

    @classmethod
    def from_bytes(cls, data: bytes):
        flags = data[1] if len(data) > 1 else 0
        new_caps = data[2:11] if flags & 0x02 and len(data) >= 11 else None
        return cls(data[0] >> 4, data[0] & 0x0F, bool(flags & 0x01), new_caps)

    def configuration(self, hz: int):
        bit = next(b for b, rate in self.HZ.items() if rate == hz)
        new_caps = None
        if self.new_caps is not None:
            new_caps = struct.pack("<BHHBBH", 0, self.TARGET_CODEC_LEVEL, self.INITIAL_CODEC_LEVEL,
                                   self.SRA_MAX_RATE, self.SRA_AVG_TIME, self.GOOD_WORKING_LEVEL)
        # Source-only: never ask for the voice back-channel
        return type(self)(bit, self.STEREO, False, new_caps)


def _rates(rates: list[int]) -> str:
    return "/".join(f"{hz / 1000:g}k" for hz in rates)


ALL = [LdacMediaCodecInformation, AptxHdMediaCodecInformation, AptxMediaCodecInformation,
       AptxLlMediaCodecInformation]

# Let Bumble decode these capabilities when it discovers remote endpoints
for _cls in ALL:
    a2dp.A2DP_VENDOR_MEDIA_CODEC_INFORMATION_CLASSES.setdefault(_cls.VENDOR_ID, {})[_cls.CODEC_ID] = _cls
