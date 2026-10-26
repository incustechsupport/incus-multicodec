"""Audio capture sources. Both deliver fixed-size chunks of packed s24le stereo PCM to a callback.

SystemAudioCapture records whatever Windows plays on its default output (WASAPI loopback),
so no virtual sound card is needed. Loopback is taken before the endpoint's volume and mute,
which lets us mute the PC's own speakers while streaming and apply Windows' volume ourselves.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

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
        self._gain = 1.0

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
            stereo = block[:, :2] if in_ch >= 2 else np.repeat(block, 2, axis=1)
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
        self._stop.clear()
        threading.Thread(target=self._volume_follower, name="volume", daemon=True).start()
        logger.info(f"capturing system audio from {self.name} at {self.device_rate} Hz -> {self.rate} Hz")

    def _volume_follower(self) -> None:
        """Track Windows' volume as our gain and keep the PC speakers muted while we stream."""
        try:
            import comtypes
            from pycaw.pycaw import AudioUtilities
            comtypes.CoInitialize()
            endpoint = AudioUtilities.GetSpeakers().EndpointVolume
        except Exception as error:
            logger.warning(f"no endpoint volume control ({error}); streaming at full level")
            return
        was_muted = endpoint.GetMute()
        try:
            while not self._stop.wait(0.05):
                self._gain = 10 ** (endpoint.GetMasterVolumeLevel() / 20)
                if self.mute_speakers and not endpoint.GetMute():
                    endpoint.SetMute(True, None)
        finally:
            if self.mute_speakers:
                endpoint.SetMute(was_muted, None)

    def stop(self) -> None:
        self._stop.set()
        for stream in self.streams:
            stream.stop_stream()
            stream.close()
        self.streams = []

    def close(self) -> None:
        self.stop()
        self.audio.terminate()


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
