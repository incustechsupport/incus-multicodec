"""AAC-LC via Windows' Media Foundation encoder (bin/aacmf.dll), framed as LATM for A2DP.

A2DP carries AAC as MPEG-4 LATM AudioMuxElements (RFC 3016 style) inside RTP. Each element here
carries its StreamMuxConfig in-band (muxConfigPresent=1, useSameStreamMux=0), which every sink
accepts at the cost of ~6 bytes per frame.
"""

import ctypes
import pathlib
from ctypes import POINTER, byref, c_int, c_long, c_short, c_ubyte, c_void_p

DLL_PATH = pathlib.Path(__file__).parent / "bin" / "aacmf.dll"

FRAME_SAMPLES = 1024
# Media Foundation's AAC encoder only offers these constant bitrates
BITRATES = (192000, 160000, 128000, 96000)
SAMPLING_INDEX = {96000: 0, 88200: 1, 64000: 2, 48000: 3, 44100: 4, 32000: 5, 24000: 6, 22050: 7, 16000: 8}

_dll = None


def _lib():
    global _dll
    if _dll is None:
        if not DLL_PATH.is_file():
            raise RuntimeError(f"{DLL_PATH} is missing; run native\\build.cmd first")
        dll = ctypes.CDLL(str(DLL_PATH))
        dll.aacmf_open.argtypes = [c_int, c_int, c_int, POINTER(c_long)]
        dll.aacmf_open.restype = c_void_p
        dll.aacmf_encode.argtypes = [c_void_p, c_void_p, c_int, c_void_p, c_int, POINTER(c_int), c_int]
        dll.aacmf_encode.restype = c_int
        dll.aacmf_close.argtypes = [c_void_p]
        _dll = dll
    return _dll


def available() -> bool:
    """False on Windows editions without Media Foundation's AAC encoder (e.g. "N" without the Media Feature Pack)."""
    try:
        Encoder(48000, 128000).close()
        return True
    except Exception:
        return False


class Encoder:
    def __init__(self, sample_rate: int, bitrate: int, channels: int = 2):
        if bitrate not in BITRATES:
            raise ValueError(f"AAC bitrate must be one of {BITRATES}")
        self.lib = _lib()
        hr = c_long(0)
        self.handle = self.lib.aacmf_open(sample_rate, channels, bitrate // 8, byref(hr))
        if not self.handle:
            raise RuntimeError(f"Windows AAC encoder unavailable (HRESULT 0x{hr.value & 0xFFFFFFFF:08X})")
        self.sample_rate = sample_rate
        self.channels = channels
        self._out = ctypes.create_string_buffer(64 * 1024)
        self._sizes = (c_int * 64)()

    def encode(self, pcm_s16: bytes) -> list[bytes]:
        """Feed interleaved s16le PCM (any length); returns the raw AAC access units now available."""
        frames = len(pcm_s16) // (2 * self.channels)
        count = self.lib.aacmf_encode(self.handle, pcm_s16, frames, self._out, len(self._out), self._sizes, 64)
        if count < 0:
            raise RuntimeError("AAC encode failed")
        units, offset = [], 0
        for i in range(count):
            units.append(self._out.raw[offset: offset + self._sizes[i]])
            offset += self._sizes[i]
        return units

    def close(self) -> None:
        if self.handle:
            self.lib.aacmf_close(self.handle)
            self.handle = None


def latm_element(access_unit: bytes, sample_rate: int, channels: int = 2) -> bytes:
    """Wrap one raw AAC-LC access unit in an LATM AudioMuxElement with in-band StreamMuxConfig."""
    fields = [
        (0, 1),  # useSameStreamMux
        # StreamMuxConfig
        (0, 1),  # audioMuxVersion
        (1, 1),  # allStreamsSameTimeFraming
        (0, 6),  # numSubFrames
        (0, 4),  # numProgram
        (0, 3),  # numLayer
        # AudioSpecificConfig
        (2, 5),  # audioObjectType: AAC LC
        (SAMPLING_INDEX[sample_rate], 4),
        (channels, 4),  # channelConfiguration
        (0, 1), (0, 1), (0, 1),  # GASpecificConfig: frameLengthFlag, dependsOnCoreCoder, extensionFlag
        (0, 3),  # frameLengthType: variable payload length
        (0xFF, 8),  # latmBufferFullness
        (0, 1),  # otherDataPresent
        (0, 1),  # crcCheckPresent
    ]
    # PayloadLengthInfo: 255-valued bytes then the remainder
    length = len(access_unit)
    fields += [(255, 8)] * (length // 255) + [(length % 255, 8)]

    value, bits = 0, 0
    for field, width in fields:
        value = (value << width) | field
        bits += width
    value = (value << (8 * length)) | int.from_bytes(access_unit, "big")
    bits += 8 * length
    pad = -bits % 8  # AudioMuxElement is byte-aligned at the end
    return (value << pad).to_bytes((bits + pad) // 8, "big")


def parse_latm_element(data: bytes) -> tuple[dict, bytes]:
    """Inverse of latm_element (for self-tests): returns (config fields, access unit)."""
    value, total = int.from_bytes(data, "big"), len(data) * 8
    pos = 0

    def read(width):
        nonlocal pos
        pos += width
        return (value >> (total - pos)) & ((1 << width) - 1)

    config = {}
    if read(1) == 0:
        config["audioMuxVersion"] = read(1)
        config["allStreamsSameTimeFraming"] = read(1)
        config["numSubFrames"], config["numProgram"], config["numLayer"] = read(6), read(4), read(3)
        config["audioObjectType"], config["samplingFrequencyIndex"], config["channels"] = read(5), read(4), read(4)
        read(3)
        config["frameLengthType"], config["latmBufferFullness"] = read(3), read(8)
        read(1), read(1)
    length = 0
    while True:
        byte = read(8)
        length += byte
        if byte != 255:
            break
    unit = bytes(read(8) for _ in range(length))
    return config, unit
