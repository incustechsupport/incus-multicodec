"""Audio capture sources. Both deliver fixed-size chunks of packed s24le stereo PCM to a callback.

SystemAudioCapture records whatever Windows plays on its default output (WASAPI loopback),
so no virtual sound card is needed. Loopback is taken before the endpoint's volume and mute,
which lets us mute the PC's own speakers while streaming and apply Windows' volume ourselves.
"""

from __future__ import annotations

import ctypes
import logging
import os
import pathlib
import threading

import numpy as np

from .realtime import audio_thread

logger = logging.getLogger(__name__)

S24_MAX = 8388607


def float_to_s24(samples: np.ndarray) -> bytes:
    """float32 [-1, 1] -> packed little-endian 24-bit."""
    ints = np.clip(np.rint(samples * S24_MAX), -S24_MAX - 1, S24_MAX).astype("<i4")
    return ints.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()


class HalfBand:
    """Stateful 2:1 decimator (windowed-sinc low-pass), for 88.2/96/176.4/192 kHz mixes feeding 44.1/48 kHz codecs."""

    def __init__(self, channels: int, taps: int = 63):
        n = np.arange(taps) - (taps - 1) / 2
        h = np.sinc(n / 2) / 2 * np.blackman(taps)
        self.h = (h / h.sum()).astype(np.float32)
        self.history = np.zeros((taps - 1, channels), np.float32)
        self.phase = 0

    def __call__(self, block: np.ndarray) -> np.ndarray:
        x = np.concatenate([self.history, block])
        self.history = x[-(len(self.h) - 1):]
        out = np.stack([np.convolve(x[:, c], self.h, mode="valid") for c in range(x.shape[1])], axis=1)
        out = out[self.phase::2]
        self.phase = (self.phase + len(block)) % 2
        return out


def downmix_to_stereo(block: np.ndarray) -> np.ndarray:
    """(frames, channels) float32 in Windows' channel order -> (frames, 2).

    Windows' layouts (KSAUDIO_SPEAKER_*): 2.1 = FL FR LFE; quad = FL FR BL BR; 5.0 = FL FR FC SL SR;
    5.1 = FL FR FC LFE BL BR (or SL SR); 7.1 = FL FR FC LFE BL BR SL SR. Centre and surrounds are
    folded in at -3 dB and LFE is dropped (the ITU stereo downmix). Front left/right stay at full
    level, so stereo content on a surround output sounds as before; peaks above full scale are
    clipped later (float_to_s24), as Windows' own downmix does.
    """
    channels = block.shape[1]
    if channels == 1:
        return np.repeat(block, 2, axis=1)
    if channels == 2:
        return block
    k = 0.7071
    left, right = block[:, 0].copy(), block[:, 1].copy()
    if channels == 3:  # 2.1: the third channel is the LFE
        surrounds, centre = (), None
    elif channels == 4:  # quad
        surrounds, centre = ((2, 3),), None
    elif channels == 5:  # 5.0
        surrounds, centre = ((3, 4),), 2
    else:  # 5.1, 7.1 and larger: FC at 2, LFE at 3
        surrounds, centre = tuple(pair for pair in ((4, 5), (6, 7)) if channels > pair[1]), 2
    if centre is not None:
        left += k * block[:, centre]
        right += k * block[:, centre]
    for l_index, r_index in surrounds:
        left += k * block[:, l_index]
        right += k * block[:, r_index]
    return np.stack([left, right], axis=1)


class _Rechunker:
    """Collects float32 stereo frames and emits exact chunk_frames-sized s24 chunks."""

    def __init__(self, chunk_frames: int, on_chunk):
        self.chunk_frames = chunk_frames
        self.on_chunk = on_chunk
        self.pending = np.zeros((0, 2), np.float32)

    def push(self, stereo: np.ndarray) -> None:
        self.pending = np.concatenate([self.pending, stereo]) if len(self.pending) else stereo
        n = self.chunk_frames
        while len(self.pending) >= n:
            self.on_chunk(float_to_s24(self.pending[:n].reshape(-1)))
            self.pending = self.pending[n:]


class SystemAudioCapture:
    """Loopback of the default Windows output, with a silent keep-alive stream and optional speaker muting."""

    def __init__(self, mute_speakers: bool = True):
        import pyaudiowpatch as pa

        self.pa = pa
        self.audio = pa.PyAudio()
        self.loopback = self.audio.get_default_wasapi_loopback()
        wasapi = self.audio.get_host_api_info_by_type(pa.paWASAPI)
        self.render = self.audio.get_device_info_by_index(wasapi["defaultOutputDevice"])
        self.device_rate = int(self.loopback["defaultSampleRate"])
        self.mute_speakers = mute_speakers
        self.name = self.render["name"]
        self.streams = []
        self._stop = threading.Event()
        self._follower: threading.Thread | None = None
        self._gain = 1.0
        # Set (from any thread) when capture can no longer deliver the right audio; the streaming
        # loop checks it and restarts capture
        self.failure: str | None = None
        self._follow_device_changes = True  # the speaker loopback is bound to one device

    def output_rate(self, supported: list[int]) -> int:
        """Pick the codec rate: the mix rate if the sink takes it, otherwise the mix rate halved (decimated)."""
        rate = self.device_rate
        while rate not in supported and rate > 48000:
            rate //= 2
        if rate not in supported:
            raise RuntimeError(f"Windows output runs at {self.device_rate} Hz, which the headphones can't take; "
                               f"set it to 48000 Hz in Sound settings")
        self.rate = rate
        return rate

    def start(self, chunk_frames: int, on_chunk) -> None:
        pa = self.pa
        in_ch = self.loopback["maxInputChannels"]
        out_ch = self.render["maxOutputChannels"]
        rechunk = _Rechunker(chunk_frames, on_chunk)
        stages = []
        rate = self.device_rate
        while rate > self.rate:
            stages.append(HalfBand(2))
            rate //= 2

        def on_loopback(data, frames, time_info, status):
            block = np.frombuffer(data, np.float32).reshape(-1, in_ch)
            stereo = downmix_to_stereo(block)  # 5.1/7.1 outputs: keep the centre (dialogue) and surrounds
            for stage in stages:
                stereo = stage(stereo)
            rechunk.push(stereo * self._gain)
            return None, pa.paContinue

        def on_keepalive(data, frames, time_info, status):
            return bytes(4 * frames * out_ch), pa.paContinue

        self.streams = [
            self.audio.open(format=pa.paFloat32, channels=out_ch, rate=int(self.render["defaultSampleRate"]),
                            output=True, output_device_index=self.render["index"], frames_per_buffer=480,
                            stream_callback=on_keepalive),
            self.audio.open(format=pa.paFloat32, channels=in_ch, rate=self.device_rate, input=True,
                            input_device_index=self.loopback["index"], frames_per_buffer=chunk_frames,
                            stream_callback=on_loopback),
        ]
        for stream in self.streams:
            stream.start_stream()
        self._start_follower()
        logger.info(f"capturing system audio from {self.name} at {self.device_rate} Hz -> {self.rate} Hz")

    def _start_follower(self) -> None:
        # A fresh stop flag per run: with one shared Event, a quick stop() + start() (AVDTP suspend
        # then start) cleared the flag before the old thread saw it, leaving two followers that
        # each restored a different "was muted" state
        self._stop_follower()
        self.failure = None
        self._stop = threading.Event()
        self._follower = threading.Thread(target=self._volume_follower, args=(self._stop,), name="volume",
                                          daemon=True)
        self._follower.start()

    def _stop_follower(self) -> None:
        self._stop.set()
        follower, self._follower = self._follower, None
        if follower is not None and follower is not threading.current_thread():
            follower.join(timeout=2)  # it restores the speakers' mute state on its way out

    def _volume_follower(self, stop: threading.Event) -> None:
        """Track Windows' volume as our gain and keep the PC speakers muted while we stream.

        Follows the default output device: if Windows switches to other speakers, the old ones get
        their mute state back and the new ones are muted (and the speaker loopback, which is bound to
        the old device, is flagged so the stream restarts capture on the new one).
        """
        try:
            import comtypes
            from pycaw.pycaw import AudioUtilities
            comtypes.CoInitialize()
        except Exception as error:
            logger.warning(f"no endpoint volume control ({error}); streaming at full level")
            return
        speakers = endpoint = None
        was_muted = False

        def release() -> None:
            if endpoint is not None and self.mute_speakers:
                try:
                    endpoint.SetMute(was_muted, None)
                except Exception as error:
                    logger.warning(f"couldn't restore the speakers' mute state: {error}")

        try:
            ticks = 0
            while True:
                if endpoint is None or ticks % 20 == 0:  # check for a new default device about once a second
                    try:
                        current = AudioUtilities.GetSpeakers()
                    except Exception:
                        current = None
                    if current is not None and (speakers is None or current.id != speakers.id):
                        if speakers is not None:
                            logger.info(f"default output changed to {current.FriendlyName}")
                            if self._follow_device_changes:
                                self.failure = "the Windows output device changed"
                        release()
                        speakers, endpoint = current, current.EndpointVolume
                        was_muted = bool(endpoint.GetMute())
                if endpoint is not None:
                    self._gain = 10 ** (endpoint.GetMasterVolumeLevel() / 20)
                    if self.mute_speakers and not endpoint.GetMute():
                        endpoint.SetMute(True, None)
                ticks += 1
                if stop.wait(0.05):
                    break
        except Exception as error:
            logger.warning(f"volume follower stopped: {error}")
        finally:
            release()

    def stop(self) -> None:
        for stream in self.streams:
            try:
                stream.stop_stream()
                stream.close()
            except Exception as error:
                logger.warning(f"closing an audio stream: {error}")
        self.streams = []
        self._stop_follower()

    def close(self) -> None:
        self.stop()
        self.audio.terminate()


class AppAudioCapture(SystemAudioCapture):
    """Everything apps play, captured *before* the output device's effects (Windows process loopback).

    The speaker loopback above is taken after the laptop speaker's processing, which on many laptops
    includes a steep bass cut (measured here: -19 dB at 100 Hz, -47 dB at 41 Hz). Process loopback with
    our own process excluded gets the apps' mix unprocessed: full-range audio, no driver, no install.
    Needs Windows 10 2004+; use SystemAudioCapture where it can't start.
    """

    DLL_PATH = pathlib.Path(__file__).parent / "bin" / "procloop.dll"
    _CALLBACK = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_float), ctypes.c_int)

    def __init__(self, mute_speakers: bool = True):
        super().__init__(mute_speakers)
        self.dll = ctypes.CDLL(str(self.DLL_PATH))
        self.dll.procloop_start.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int, self._CALLBACK]
        self.dll.procloop_start.restype = ctypes.c_int
        # procloop_error (newer DLLs): the HRESULT that stopped the capture thread, or 0
        self._dll_error = getattr(self.dll, "procloop_error", None)
        if self._dll_error is not None:
            self._dll_error.argtypes = []
            self._dll_error.restype = ctypes.c_int
        self.name = f"apps' audio (before {self.render['name']} effects)"
        self._callback = None
        self._fallback = False

    @property
    def failure(self) -> str | None:
        if self._failure:
            return self._failure
        if not self._fallback and self._dll_error is not None:
            hr = self._dll_error()
            if hr:
                return f"Windows audio capture failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})"
        return None

    @failure.setter
    def failure(self, value: str | None) -> None:
        self._failure = value

    def start(self, chunk_frames: int, on_chunk) -> None:
        rechunk = _Rechunker(chunk_frames, on_chunk)
        stages = []
        rate = self.device_rate
        while rate > self.rate:
            stages.append(HalfBand(2))
            rate //= 2

        def on_audio(data, frames):
            audio_thread("Pro Audio")  # procloop's capture thread; a no-op after the first call
            stereo = np.ctypeslib.as_array(data, shape=(frames * 2,)).reshape(-1, 2).copy()
            for stage in stages:
                stereo = stage(stereo)
            rechunk.push(stereo * self._gain)

        # Keep the ctypes callback alive for as long as the DLL may call it
        self._callback = self._CALLBACK(on_audio)
        # Capture at the Windows mix rate (decimated above if the codec needs less); the DLL emits
        # silence while nothing plays, so no keep-alive stream is needed
        hr = self.dll.procloop_start(os.getpid(), self.device_rate, 2, self._callback)
        if hr:
            # e.g. Windows older than 10 2004: fall back to the speaker loopback (after its effects)
            logger.warning(f"process loopback unavailable (HRESULT 0x{hr & 0xFFFFFFFF:08X}); "
                           "capturing after the speaker's effects instead")
            self._fallback = True
            self._follow_device_changes = True
            super().start(chunk_frames, on_chunk)
            return
        self._fallback = False
        # Process loopback isn't tied to an output device, so a device switch only moves the muting
        self._follow_device_changes = False
        self._start_follower()
        logger.info(f"capturing apps' audio (process loopback) at {self.device_rate} Hz -> {self.rate} Hz")

    def stop(self) -> None:
        if self._fallback:
            super().stop()
            return
        self.dll.procloop_stop()
        self._stop_follower()


def open_system_audio(mute_speakers: bool = True):
    """The best way to capture "everything Windows plays" on this PC."""
    try:
        return AppAudioCapture(mute_speakers)
    except OSError as error:
        logger.warning(f"process loopback not available ({error}); using speaker loopback")
        return SystemAudioCapture(mute_speakers)


class DeviceCapture:
    """A named WASAPI recording device (e.g. VB-Cable's "CABLE Output"), resampled by Windows as needed."""

    def __init__(self, name: str):
        import sounddevice as sd

        self.sd = sd
        wasapi = next(i for i, h in enumerate(sd.query_hostapis()) if "WASAPI" in h["name"])
        for index, device in enumerate(sd.query_devices()):
            if device["hostapi"] == wasapi and device["max_input_channels"] > 0 and name.lower() in device["name"].lower():
                self.index, self.name = index, device["name"]
                break
        else:
            raise RuntimeError(f"no recording device matching {name!r}")
        self.stream = None

    def output_rate(self, supported: list[int]) -> int:
        self.rate = 48000 if 48000 in supported else max(r for r in supported if r <= 96000)
        return self.rate

    def start(self, chunk_frames: int, on_chunk) -> None:
        self.stream = self.sd.RawInputStream(
            device=self.index, samplerate=self.rate, channels=2, dtype="int24", blocksize=chunk_frames,
            latency="low", extra_settings=self.sd.WasapiSettings(auto_convert=True),
            callback=lambda indata, frames, t, status: on_chunk(bytes(indata)),
        )
        self.stream.start()
        logger.info(f"capturing {self.name} at {self.rate} Hz")

    def stop(self) -> None:
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def close(self) -> None:
        self.stop()


def list_recording_devices() -> list[str]:
    import sounddevice as sd
    wasapi = next(i for i, h in enumerate(sd.query_hostapis()) if "WASAPI" in h["name"])
    return [d["name"] for d in sd.query_devices() if d["hostapi"] == wasapi and d["max_input_channels"] > 0]
