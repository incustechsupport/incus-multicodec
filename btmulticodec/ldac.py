"""ctypes binding for Sony's libldac encoder and its adaptive-bitrate helper (bin/ldac.dll)."""

import ctypes
import pathlib
from ctypes import POINTER, byref, c_int, c_uint, c_void_p

DLL_PATH = pathlib.Path(__file__).parent / "bin" / "ldac.dll"

# Samples per channel consumed by one ldacBT_encode() call
LSU = 128

# Encode quality modes (EQMID)
EQMID_HQ = 0  # 990/909 kbps
EQMID_SQ = 1  # 660/606 kbps
EQMID_MQ = 2  # 330/303 kbps
EQMID_NAMES = {EQMID_HQ: "HQ", EQMID_SQ: "SQ", EQMID_MQ: "MQ"}

CHANNEL_MODE_MONO = 0x04
CHANNEL_MODE_DUAL = 0x02
CHANNEL_MODE_STEREO = 0x01

SMPL_FMT_S16 = 0x2
SMPL_FMT_S24 = 0x3
SMPL_FMT_S32 = 0x4

_dll = None


def _lib():
    global _dll
    if _dll is None:
        if not DLL_PATH.is_file():
            raise RuntimeError(f"{DLL_PATH} is missing; run native\\build.cmd first")
        dll = ctypes.CDLL(str(DLL_PATH))
        dll.ldacBT_get_handle.restype = c_void_p
        dll.ldacBT_free_handle.argtypes = [c_void_p]
        dll.ldacBT_close_handle.argtypes = [c_void_p]
        dll.ldacBT_get_version.restype = c_int
        dll.ldacBT_get_bitrate.argtypes = [c_void_p]
        dll.ldacBT_get_eqmid.argtypes = [c_void_p]
        dll.ldacBT_set_eqmid.argtypes = [c_void_p, c_int]
        dll.ldacBT_get_error_code.argtypes = [c_void_p]
        dll.ldacBT_init_handle_encode.argtypes = [c_void_p, c_int, c_int, c_int, c_int, c_int]
        dll.ldacBT_encode.argtypes = [
            c_void_p, c_void_p, POINTER(c_int), c_void_p, POINTER(c_int), POINTER(c_int)
        ]
        dll.ldac_ABR_get_handle.restype = c_void_p
        dll.ldac_ABR_free_handle.argtypes = [c_void_p]
        dll.ldac_ABR_Init.argtypes = [c_void_p, c_uint]
        dll.ldac_ABR_set_thresholds.argtypes = [c_void_p, c_uint, c_uint, c_uint]
        dll.ldac_ABR_Proc.argtypes = [c_void_p, c_void_p, c_uint, c_uint]
        _dll = dll
    return _dll


def version() -> str:
    v = _lib().ldacBT_get_version()
    return f"{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"


class LdacError(RuntimeError):
    pass


class Encoder:
    """Stateful LDAC encoder. Feed interleaved PCM in LSU-sample chunks, get packed frames out."""

    def __init__(self, mtu: int, eqmid: int, sample_rate: int, channel_mode: int = CHANNEL_MODE_STEREO,
                 abr: bool = False):
        self.lib = _lib()
        self.abr = None
        self.handle = self.lib.ldacBT_get_handle()
        if not self.handle:
            raise LdacError("ldacBT_get_handle failed")
        self.channels = 1 if channel_mode == CHANNEL_MODE_MONO else 2
        self.sample_rate = sample_rate
        if self.lib.ldacBT_init_handle_encode(self.handle, mtu, eqmid, channel_mode, SMPL_FMT_S24, sample_rate):
            err = self.lib.ldacBT_get_error_code(self.handle)
            self.close()
            raise LdacError(f"ldacBT_init_handle_encode failed (mtu={mtu} eqmid={eqmid} sf={sample_rate}): 0x{err:08X}")
        self.chunk_bytes = LSU * self.channels * 3  # packed s24le
        self._out = ctypes.create_string_buffer(1024)
        self.abr = None
        if abr:
            self.abr = self.lib.ldac_ABR_get_handle()
            # 20ms ticks, matching AOSP's encoder timer
            self.lib.ldac_ABR_Init(self.abr, 20)

    def encode(self, pcm: bytes) -> tuple[bytes, int]:
        """Encode exactly one LSU chunk. Returns (packed frames, frame count); empty until a packet fills."""
        used, size, frames = c_int(0), c_int(0), c_int(0)
        buf = ctypes.create_string_buffer(pcm, len(pcm))
        if self.lib.ldacBT_encode(self.handle, buf, byref(used), self._out, byref(size), byref(frames)):
            raise LdacError(f"ldacBT_encode failed: 0x{self.lib.ldacBT_get_error_code(self.handle):08X}")
        return self._out.raw[: size.value], frames.value

    def adapt(self, tx_queue_depth: int) -> int:
        """Run one ABR tick against the current transmit backlog; returns the eqmid now in use."""
        if self.abr:
            self.lib.ldac_ABR_Proc(self.handle, self.abr, tx_queue_depth, 1)
        return self.eqmid

    @property
    def eqmid(self) -> int:
        return self.lib.ldacBT_get_eqmid(self.handle)

    @property
    def bitrate(self) -> int:
        return self.lib.ldacBT_get_bitrate(self.handle)

    def close(self) -> None:
        if self.abr:
            self.lib.ldac_ABR_free_handle(self.abr)
            self.abr = None
        if self.handle:
            self.lib.ldacBT_close_handle(self.handle)
            self.lib.ldacBT_free_handle(self.handle)
            self.handle = None
