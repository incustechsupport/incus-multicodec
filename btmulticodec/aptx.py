"""ctypes binding for the aptX (bin/aptx.dll) and aptX HD (bin/aptxhd.dll) encoders.

Both DLLs are Qualcomm's encoders from the Android Open Source Project (Apache-2.0) behind the small
C API in native/aptx_aosp.c. Their output is byte-for-byte what libfreeaptx, used up to 0.1.5, produced.
"""

import ctypes
import pathlib
from ctypes import POINTER, byref, c_size_t, c_void_p

BIN = pathlib.Path(__file__).parent / "bin"

# One aptX "sample group": 4 stereo frames of packed 24-bit PCM in, 4 (aptX) or 6 (HD) bytes out
GROUP_FRAMES = 4
GROUP_PCM_BYTES = GROUP_FRAMES * 2 * 3

_dlls: dict[bool, ctypes.CDLL] = {}


def _lib(hd: bool) -> ctypes.CDLL:
    if hd not in _dlls:
        path = BIN / ("aptxhd.dll" if hd else "aptx.dll")
        if not path.is_file():
            raise RuntimeError(f"{path} is missing; run native\\build.cmd first")
        dll = ctypes.CDLL(str(path))
        dll.aptx_init.argtypes = []
        dll.aptx_init.restype = c_void_p
        dll.aptx_reset.argtypes = [c_void_p]
        dll.aptx_finish.argtypes = [c_void_p]
        dll.aptx_encode.argtypes = [c_void_p, c_void_p, c_size_t, c_void_p, c_size_t, POINTER(c_size_t)]
        dll.aptx_encode.restype = c_size_t
        _dlls[hd] = dll
    return _dlls[hd]


class Encoder:
    def __init__(self, hd: bool):
        self.lib = _lib(hd)
        self.hd = hd
        self.handle = self.lib.aptx_init()
        if not self.handle:
            raise RuntimeError("aptx_init failed")
        # Encoded bytes per stereo PCM frame
        self.bytes_per_frame = (6 if hd else 4) / GROUP_FRAMES

    def encode(self, pcm: bytes) -> bytes:
        """Encode packed s24le stereo PCM; len(pcm) must be a multiple of GROUP_PCM_BYTES."""
        groups = len(pcm) // GROUP_PCM_BYTES
        out = ctypes.create_string_buffer(groups * (6 if self.hd else 4))
        written = c_size_t(0)
        used = self.lib.aptx_encode(self.handle, pcm, groups * GROUP_PCM_BYTES, out, len(out), byref(written))
        if used != groups * GROUP_PCM_BYTES:
            raise RuntimeError(f"aptx_encode consumed {used} of {len(pcm)} bytes")
        return out.raw[: written.value]

    def close(self) -> None:
        if self.handle:
            self.lib.aptx_finish(self.handle)
            self.handle = None
