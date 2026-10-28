"""A2DP codec information elements for the vendor codecs Incus Multicodec speaks: LDAC, aptX, aptX HD.

Byte layouts follow the codec capability structs in PipeWire's spa/plugins/bluez5/a2dp-codec-caps.h.
"""

from __future__ import annotations

import dataclasses
import enum
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


def _rates(rates: list[int]) -> str:
    return "/".join(f"{hz / 1000:g}k" for hz in rates)


ALL = [LdacMediaCodecInformation, AptxHdMediaCodecInformation, AptxMediaCodecInformation]

# Let Bumble decode these capabilities when it discovers remote endpoints
for _cls in ALL:
    a2dp.A2DP_VENDOR_MEDIA_CODEC_INFORMATION_CLASSES.setdefault(_cls.VENDOR_ID, {})[_cls.CODEC_ID] = _cls
