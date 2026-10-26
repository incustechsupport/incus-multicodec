"""ctypes binding for BlueZ libsbc (bin/sbc.dll): SBC encoding configured from A2DP config bytes."""

import ctypes
import pathlib
from ctypes import POINTER, byref, c_char_p, c_int, c_size_t, c_ssize_t, c_uint8, c_ulong, c_void_p

DLL_PATH = pathlib.Path(__file__).parent / "bin" / "sbc.dll"

SBC_LE = 0x00


class _Sbc(ctypes.Structure):
    _fields_ = [
        ("flags", c_ulong),
        ("frequency", c_uint8),
        ("blocks", c_uint8),
        ("subbands", c_uint8),
        ("mode", c_uint8),
        ("allocation", c_uint8),
        ("bitpool", c_uint8),
        ("endian", c_uint8),
        ("priv", c_void_p),
        ("priv_alloc_base", c_void_p),
    ]


_dll = None


def _lib():
    global _dll
    if _dll is None:
        if not DLL_PATH.is_file():
            raise RuntimeError(f"{DLL_PATH} is missing; run native\\build.cmd first")
        dll = ctypes.CDLL(str(DLL_PATH))
        dll.sbc_init_a2dp.argtypes = [POINTER(_Sbc), c_ulong, c_char_p, c_size_t]
        dll.sbc_finish.argtypes = [POINTER(_Sbc)]
        dll.sbc_encode.argtypes = [POINTER(_Sbc), c_void_p, c_size_t, c_void_p, c_size_t, POINTER(c_ssize_t)]
        dll.sbc_encode.restype = c_ssize_t
        dll.sbc_get_codesize.argtypes = [POINTER(_Sbc)]
        dll.sbc_get_codesize.restype = c_size_t
        dll.sbc_get_frame_length.argtypes = [POINTER(_Sbc)]
        dll.sbc_get_frame_length.restype = c_size_t
        _dll = dll
    return _dll


class Encoder:
    """Encodes s16le PCM, one codesize-sized block per SBC frame."""

    def __init__(self, a2dp_configuration: bytes, bitpool: int | None = None):
        self.lib = _lib()
        self.sbc = _Sbc()
        if self.lib.sbc_init_a2dp(byref(self.sbc), 0, a2dp_configuration, len(a2dp_configuration)):
            raise RuntimeError(f"sbc_init_a2dp rejected configuration {a2dp_configuration.hex()}")
        self.sbc.endian = SBC_LE
        if bitpool is not None:
            self.sbc.bitpool = bitpool
        self.codesize = self.lib.sbc_get_codesize(byref(self.sbc))
        self.frame_length = self.lib.sbc_get_frame_length(byref(self.sbc))
        self._out = ctypes.create_string_buffer(self.frame_length + 16)

    def encode_frame(self, pcm: bytes) -> bytes:
        written = c_ssize_t(0)
        used = self.lib.sbc_encode(byref(self.sbc), pcm, len(pcm), self._out, len(self._out), byref(written))
        if used != self.codesize:
            raise RuntimeError(f"sbc_encode consumed {used} of {len(pcm)} bytes")
        return self._out.raw[: written.value]

    def close(self) -> None:
        if self.sbc.priv:
            self.lib.sbc_finish(byref(self.sbc))
