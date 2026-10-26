"""Live capture -> codec encode -> A2DP media packets, for a Bumble AVDTP source endpoint.

Each codec is a Session that turns fixed-size chunks of packed s24le stereo PCM into
ready-to-send L2CAP SDUs (with or without an RTP header, depending on the codec).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import math
import threading

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
# Media packets still waiting in the host for the radio. Past this much audio the headphones have
# already run dry, so later packets only add delay: drop new ones until the radio catches up.
MAX_RADIO_BACKLOG_SECONDS = 0.5
AAC_PEAK_RATIO = 1.75  # largest AAC frame / mean frame, with margin (measured up to 1.68)
HICCUP_REPORT_SECONDS = 10  # log a summary of drops/stalls at most this often
STALL_SECONDS = 0.06  # capture normally delivers every ~10 ms; a gap this long is a stall


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
        return f"LDAC {name}{' (best quality)' if self.adaptive else ''}"

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

    # Multiple of aptx.GROUP_FRAMES. Packets are cut by size below, so this only sets how often the
    # encoder runs (375 times a second here; 32 frames meant 1500 trips through Python)
    chunk_frames = 128

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
    """SBC, configured from the negotiated A2DP bytes. Frames are packed up to the MTU (max 15 per packet).

    The bitpool (bitrate) adapts like LDAC's ABR: it steps down while media packets queue up in the
    host (the radio can't keep up) and creeps back to the negotiated maximum once the queue stays
    empty. Every SBC frame carries its own bitpool, and libsbc rereads it before each frame.
    """

    BACKLOG_HIGH = 2  # queued media packets that count as congestion (libldac's HQ threshold)
    STEP_DOWN, STEP_UP = 6, 2
    DOWN_COOLDOWN_TICKS = 10  # ~200 ms between steps down, so one stall isn't punished repeatedly
    UP_AFTER_TICKS = 50  # ~1 s of an empty queue before each step back up (full quality again in ~12 s)
    MIN_USEFUL_BITPOOL = 30

    def __init__(self, mtu: int, configuration: bytes) -> None:
        self.max_bitpool = configuration[3]
        self.min_bitpool = min(self.max_bitpool, max(configuration[2], self.MIN_USEFUL_BITPOOL))
        # sbc_init_a2dp sets everything but the bitpool, so pass the negotiated maximum explicitly
        self.encoder = sbc.Encoder(configuration, bitpool=self.max_bitpool)
        self.chunk_frames = self.encoder.codesize // 4  # s16 stereo
        # Sized for the largest frames; at a lower bitpool packets just come out a little short
        self.max_frames = min(15, (mtu - RTP_HEADER - 1) // self.encoder.frame_length)
        self.frames: list[bytes] = []
        self.sequence = 0
        self.samples = 0
        self.calm_ticks = 0
        self.cooldown = 0

    @property
    def mode(self) -> str:
        return f"SBC bitpool {self.encoder.sbc.bitpool}"

    def adapt(self, tx_queue_packets: int) -> None:
        bitpool = self.encoder.sbc.bitpool
        self.cooldown = max(0, self.cooldown - 1)
        if tx_queue_packets >= self.BACKLOG_HIGH:
            self.calm_ticks = 0
            if self.cooldown == 0 and bitpool > self.min_bitpool:
                self.encoder.sbc.bitpool = max(self.min_bitpool, bitpool - self.STEP_DOWN)
                self.cooldown = self.DOWN_COOLDOWN_TICKS
        elif tx_queue_packets == 0:
            self.calm_ticks += 1
            if self.calm_ticks >= self.UP_AFTER_TICKS and bitpool < self.max_bitpool:
                self.encoder.sbc.bitpool = min(self.max_bitpool, bitpool + self.STEP_UP)
                self.calm_ticks = 0

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
        # Media Foundation's AAC is constant bitrate only on average: transients borrow bits, and the
        # biggest frames measured ~1.6x the mean (838 B at 192 kbps). A frame over the MTU can't be
        # sent, which is an audible gap, so pick the highest bitrate whose *peak* frames fit
        def frame_bytes(bitrate):
            return int(bitrate * aac.FRAME_SAMPLES / sample_rate / 8 * AAC_PEAK_RATIO) + 16 + RTP_HEADER
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
        from .realtime import audio_thread
        audio_thread()  # the engine's thread: encodes and hands packets to the radio
        session: Session = self.make_session(rtp_channel.peer_mtu)
        chunk_bytes = session.chunk_frames * PCM_FRAME_BYTES
        chunk_seconds = session.chunk_frames / self.sample_rate
        loop = asyncio.get_running_loop()
        max_chunks = max(2, int(MAX_BACKLOG_SECONDS / chunk_seconds))

        # Capture hands over a few chunks per ~10 ms buffer. Wake this loop once per batch rather
        # than once per chunk (375 times a second for LDAC): each wake-up is a socket write plus a
        # trip through the event loop, all under the GIL
        ready: collections.deque[bytes] = collections.deque()
        arrived = asyncio.Event()
        wake_pending = threading.Event()

        def on_chunk(pcm: bytes) -> None:  # capture thread
            ready.append(pcm)
            if not wake_pending.is_set():
                wake_pending.set()
                loop.call_soon_threadsafe(arrived.set)

        acl = self.host.acl_packet_queue
        acl_size = acl.max_packet_size if acl else 1021
        frags_per_packet = max(1, math.ceil((rtp_channel.peer_mtu + 4) / acl_size))
        adapt_every = max(1, round(0.020 / chunk_seconds))

        def radio_backlog() -> int:
            """Media packets queued in the host, not yet handed to the controller. Bumble 0.0.236's
            DataPacketQueue._packets counts ACL fragments; `pending` would also count packets the
            controller already holds (several on any healthy link)."""
            return len(acl._packets) // frags_per_packet if acl is not None and hasattr(acl, "_packets") else 0

        logger.info(f"streaming {session.mode} at {self.sample_rate} Hz, L2CAP MTU {rtp_channel.peer_mtu}")
        chunk_count = 0
        sent_bytes = 0
        encoded_packets = encoded_chunks = 0  # this second, for audio_per_packet
        audio_per_packet = None  # seconds of audio per media packet, measured
        last_report = last_hiccup_report = last_arrival = loop.time()
        mode = session.mode
        # hiccups since the last summary
        dropped = radio_dropped = stalls = 0
        worst_stall = worst_backlog = 0.0
        try:
            self.capture.start(session.chunk_frames, on_chunk)
            while True:
                await arrived.wait()
                arrived.clear()
                wake_pending.clear()  # before draining: a chunk appended from here on wakes us again
                now = loop.time()
                gap = now - last_arrival
                last_arrival = now
                if gap > STALL_SECONDS:
                    stalls += 1
                    worst_stall = max(worst_stall, gap)
                # Bound latency: if this loop fell behind capture, skip stale audio
                while len(ready) > max_chunks:
                    ready.popleft()
                    dropped += 1

                while ready:
                    pcm = ready.popleft()
                    if len(pcm) != chunk_bytes:
                        pcm = pcm.ljust(chunk_bytes, b"\0")[:chunk_bytes]
                    packets = session.encode(pcm)
                    encoded_chunks += 1
                    encoded_packets += len(packets)
                    if packets:
                        backlog = radio_backlog()
                        backlog_seconds = backlog * audio_per_packet if audio_per_packet else 0.0
                        worst_backlog = max(worst_backlog, backlog_seconds)
                        if backlog_seconds > MAX_RADIO_BACKLOG_SECONDS:
                            # Still encoded (encoders keep state) and numbered, so the sink sees a gap
                            # in the RTP sequence and conceals it instead of playing stale audio late
                            radio_dropped += len(packets)
                        else:
                            for packet in packets:
                                rtp_channel.write(packet)
                                sent_bytes += len(packet)

                    chunk_count += 1
                    if chunk_count % adapt_every == 0:
                        # Rate control wants the host-side transmit backlog in media packets, as
                        # Android measures it (see radio_backlog). libldac's HQ/SQ safety threshold is 2
                        session.adapt(radio_backlog())

                now = loop.time()
                if now - last_report >= 1.0:
                    elapsed = now - last_report
                    if encoded_packets:
                        audio_per_packet = encoded_chunks * chunk_seconds / encoded_packets
                    if session.mode != mode:
                        logger.info(f"{mode} -> {session.mode} (radio backlog {radio_backlog()} packets)")
                        mode = session.mode
                    if self.on_stats:
                        self.on_stats(mode=session.mode, kbps=sent_bytes * 8 / elapsed / 1000,
                                      backlog_ms=len(ready) * chunk_seconds * 1000, dropped=dropped)
                    sent_bytes = encoded_packets = encoded_chunks = 0
                    last_report = now
                if (dropped or radio_dropped or stalls) and now - last_hiccup_report >= HICCUP_REPORT_SECONDS:
                    logger.warning(
                        f"audio hiccups in the last {now - last_hiccup_report:.0f} s: "
                        f"{stalls} stall(s), longest {worst_stall * 1000:.0f} ms; "
                        f"{dropped * chunk_seconds * 1000:.0f} ms of captured audio skipped; "
                        f"{radio_dropped} packet(s) dropped, radio backlog peaked at {worst_backlog * 1000:.0f} ms")
                    dropped = radio_dropped = stalls = 0
                    worst_stall = worst_backlog = 0.0
                    last_hiccup_report = now
                elif not (dropped or radio_dropped or stalls):
                    last_hiccup_report = now
        finally:
            self.capture.stop()
            session.close()
