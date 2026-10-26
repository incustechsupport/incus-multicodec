"""Live capture -> codec encode -> A2DP media packets, for a Bumble AVDTP source endpoint.

Each codec is a Session that turns fixed-size chunks of packed s24le stereo PCM into
ready-to-send L2CAP SDUs (with or without an RTP header, depending on the codec).
"""

from __future__ import annotations

import asyncio
import logging
import math

import numpy as np
from bumble import l2cap
from bumble.host import Host
from bumble.rtp import MediaPacket

from . import aac, aptx, ldac, sbc

logger = logging.getLogger(__name__)

RTP_HEADER = 12
RTP_PAYLOAD_TYPE = 96
PCM_FRAME_BYTES = 2 * 3  # stereo, packed 24-bit
MAX_BACKLOG_SECONDS = 0.15  # captured audio older than this is dropped to bound latency


def _rtp(sequence: int, timestamp: int, payload: bytes) -> bytes:
    return bytes(MediaPacket(2, 0, 0, 0, sequence, timestamp & 0xFFFFFFFF, 0, [], RTP_PAYLOAD_TYPE, payload))


# ----------------------------------------------------------------------------- sessions
class Session:
    chunk_frames: int  # PCM frames per encode() call
    mode = ""

    def encode(self, pcm: bytes) -> list[bytes]:
        raise NotImplementedError

    def adapt(self, tx_queue_packets: int) -> None:
        pass

    def close(self) -> None:
        pass


class LdacSession(Session):
    chunk_frames = ldac.LSU

    def __init__(self, mtu: int, sample_rate: int, eqmid: int | None) -> None:
        # libldac takes the whole AVDTP channel MTU and reserves room for RTP + frame-count header itself
        self.encoder = ldac.Encoder(
            mtu=mtu,
            eqmid=ldac.EQMID_HQ if eqmid is None else eqmid,
            sample_rate=sample_rate,
            abr=eqmid is None,
        )
        self.adaptive = eqmid is None
        self.sequence = 0
        self.samples = 0

    @property
    def mode(self) -> str:
        name = ldac.EQMID_NAMES.get(self.encoder.eqmid, str(self.encoder.eqmid))
        return f"LDAC {name}{' (auto)' if self.adaptive else ''}"

    def encode(self, pcm: bytes) -> list[bytes]:
        payload, frames = self.encoder.encode(pcm)
        self.samples += self.chunk_frames
        if not payload:
            return []
        packet = _rtp(self.sequence, self.samples, bytes([frames & 0x0F]) + payload)
        self.sequence = (self.sequence + 1) & 0xFFFF
        return [packet]

    def adapt(self, tx_queue_packets: int) -> None:
        self.encoder.adapt(tx_queue_packets)

    def close(self) -> None:
        self.encoder.close()


class AptxSession(Session):
    """aptX family. Classic aptX and aptX LL send raw codewords; aptX HD wraps them in RTP."""

    chunk_frames = 32  # multiple of aptx.GROUP_FRAMES

    def __init__(self, mtu: int, sample_rate: int, hd: bool, packet_ms: float, name: str) -> None:
        self.encoder = aptx.Encoder(hd)
        self.rtp = hd
        self.mode = name
        room = mtu - (RTP_HEADER if self.rtp else 0)
        bytes_per_group = 6 if hd else 4
        # Fill packets up to the MTU, but no more than packet_ms of audio (aptX LL wants small ones)
        cap = int(sample_rate * packet_ms / 1000 * self.encoder.bytes_per_frame)
        self.packet_bytes = max(bytes_per_group, min(room, cap) // bytes_per_group * bytes_per_group)
        self.pending = bytearray()
        self.sequence = 0
        self.samples = 0

    def encode(self, pcm: bytes) -> list[bytes]:
        self.pending += self.encoder.encode(pcm)
        packets = []
        while len(self.pending) >= self.packet_bytes:
            payload = bytes(self.pending[: self.packet_bytes])
            del self.pending[: self.packet_bytes]
            if self.rtp:
                payload = _rtp(self.sequence, self.samples, payload)
                self.sequence = (self.sequence + 1) & 0xFFFF
            self.samples += int(self.packet_bytes / self.encoder.bytes_per_frame)
            packets.append(payload)
        return packets

    def close(self) -> None:
        self.encoder.close()


class SbcSession(Session):
    """SBC, configured from the negotiated A2DP bytes. Frames are packed up to the MTU (max 15 per packet)."""

    def __init__(self, mtu: int, configuration: bytes) -> None:
        # sbc_init_a2dp sets everything but the bitpool, so pass the negotiated maximum explicitly
        self.encoder = sbc.Encoder(configuration, bitpool=configuration[3])
        self.chunk_frames = self.encoder.codesize // 4  # s16 stereo
        self.max_frames = min(15, (mtu - RTP_HEADER - 1) // self.encoder.frame_length)
        bitpool = configuration[3]
        self.mode = f"SBC bitpool {bitpool}"
        self.frames: list[bytes] = []
        self.sequence = 0
        self.samples = 0

    def encode(self, pcm: bytes) -> list[bytes]:
        # s24 -> s16: keep the top two bytes of each sample
        s16 = np.frombuffer(pcm, np.uint8).reshape(-1, 3)[:, 1:].tobytes()
        self.frames.append(self.encoder.encode_frame(s16))
        if len(self.frames) < self.max_frames:
            return []
        packet = _rtp(self.sequence, self.samples, bytes([len(self.frames)]) + b"".join(self.frames))
        self.sequence = (self.sequence + 1) & 0xFFFF
        self.samples += len(self.frames) * self.chunk_frames
        self.frames = []
        return [packet]

    def close(self) -> None:
        self.encoder.close()


class AacSession(Session):
    """AAC-LC from Windows' encoder, one LATM AudioMuxElement per RTP packet."""

    chunk_frames = aac.FRAME_SAMPLES

    def __init__(self, mtu: int, sample_rate: int, max_bitrate: int) -> None:
        # Media Foundation is strictly CBR, so every frame is the same size: pick the highest
        # bitrate the sink allows whose frames (+ LATM + RTP headers) fit the channel MTU
        def frame_bytes(bitrate):
            return bitrate * aac.FRAME_SAMPLES // sample_rate // 8 + 16 + RTP_HEADER
        bitrate = next((b for b in aac.BITRATES if b <= max_bitrate and frame_bytes(b) <= mtu), aac.BITRATES[-1])
        self.encoder = aac.Encoder(sample_rate, bitrate)
        self.sample_rate = sample_rate
        self.mtu = mtu
        self.mode = f"AAC {bitrate // 1000} kbps"
        self.sequence = 0
        self.samples = 0
        self.oversize = 0

    def encode(self, pcm: bytes) -> list[bytes]:
        s16 = np.frombuffer(pcm, np.uint8).reshape(-1, 3)[:, 1:].tobytes()
        packets = []
        for unit in self.encoder.encode(s16):
            packet = _rtp(self.sequence, self.samples, aac.latm_element(unit, self.sample_rate))
            self.samples += aac.FRAME_SAMPLES
            if len(packet) > self.mtu:
                self.oversize += 1
                if self.oversize == 1:
                    logger.warning(f"AAC frame of {len(packet)} B exceeds MTU {self.mtu}; dropping such frames")
                continue
            self.sequence = (self.sequence + 1) & 0xFFFF
            packets.append(packet)
        return packets

    def close(self) -> None:
        self.encoder.close()


# ----------------------------------------------------------------------------- pump
class LivePump:
    """Drop-in for bumble.avdtp.MediaPacketPump that streams live audio as soon as it is encoded.

    Capture is the clock: packets go out when the encoder fills one, so there is no
    timestamp pacing (which would drift against the sound card).
    """

    def __init__(self, host: Host, capture, sample_rate: int, make_session, on_stats=None) -> None:
        self.host = host
        self.capture = capture  # capture.SystemAudioCapture / DeviceCapture, output_rate() already chosen
        self.sample_rate = sample_rate
        self.make_session = make_session  # (peer_mtu) -> Session
        self.on_stats = on_stats
        self.task: asyncio.Task | None = None

    async def start(self, rtp_channel: l2cap.ClassicChannel) -> None:
        self.task = asyncio.create_task(self._run(rtp_channel))

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def _run(self, rtp_channel: l2cap.ClassicChannel) -> None:
        session: Session = self.make_session(rtp_channel.peer_mtu)
        chunk_bytes = session.chunk_frames * PCM_FRAME_BYTES
        loop = asyncio.get_running_loop()
        chunks: asyncio.Queue[bytes] = asyncio.Queue()
        max_chunks = max(2, int(MAX_BACKLOG_SECONDS * self.sample_rate / session.chunk_frames))
        dropped = 0

        acl = self.host.acl_packet_queue
        acl_size = acl.max_packet_size if acl else 1021
        frags_per_packet = max(1, math.ceil((rtp_channel.peer_mtu + 4) / acl_size))
        adapt_every = max(1, round(0.020 * self.sample_rate / session.chunk_frames))

        logger.info(f"streaming {session.mode} at {self.sample_rate} Hz, L2CAP MTU {rtp_channel.peer_mtu}")
        chunk_count = 0
        sent_bytes = 0
        last_report = loop.time()
        try:
            self.capture.start(session.chunk_frames, lambda pcm: loop.call_soon_threadsafe(chunks.put_nowait, pcm))
            while True:
                pcm = await chunks.get()
                # Bound latency: if the radio or encoder fell behind, skip stale audio
                while chunks.qsize() > max_chunks:
                    chunks.get_nowait()
                    dropped += 1
                if len(pcm) != chunk_bytes:
                    pcm = pcm.ljust(chunk_bytes, b"\0")[:chunk_bytes]

                for packet in session.encode(pcm):
                    rtp_channel.write(packet)
                    sent_bytes += len(packet)

                chunk_count += 1
                if chunk_count % adapt_every == 0:
                    # Rate control wants the backlog in media packets; the host counts ACL fragments
                    session.adapt((acl.pending if acl else 0) // frags_per_packet)

                now = loop.time()
                if now - last_report >= 1.0:
                    if self.on_stats:
                        self.on_stats(
                            mode=session.mode,
                            kbps=sent_bytes * 8 / (now - last_report) / 1000,
                            backlog_ms=chunks.qsize() * session.chunk_frames * 1000 / self.sample_rate,
                            dropped=dropped,
                        )
                    sent_bytes = 0
                    last_report = now
        finally:
            self.capture.stop()
            session.close()
