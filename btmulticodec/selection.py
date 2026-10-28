"""Which codecs a sink offers, how to configure each one, and which encoder session streams it."""

from __future__ import annotations

from dataclasses import dataclass

from bumble.a2dp import (
    A2DP_MPEG_2_4_AAC_CODEC_TYPE,
    A2DP_NON_A2DP_CODEC_TYPE,
    A2DP_SBC_CODEC_TYPE,
    AacMediaCodecInformation,
    SbcMediaCodecInformation,
)
from bumble.avdtp import AVDTP_AUDIO_MEDIA_TYPE, AVDTP_MEDIA_CODEC_SERVICE_CATEGORY, MediaCodecCapabilities

from . import aac, codec, ldac
from .stream import AacSession, AptxSession, LdacSession, SbcSession

LDAC_QUALITY = {"auto": None, "hq": ldac.EQMID_HQ, "sq": ldac.EQMID_SQ, "mq": ldac.EQMID_MQ}
SBC_MAX_BITPOOL = 53  # the A2DP "high quality" joint-stereo setting


@dataclass(frozen=True)
class Codec:
    key: str
    label: str
    info: type | None  # vendor codec info class; None for the standard codecs (SBC, AAC)


# Best fidelity first; "auto" takes the first one the sink offers
CODECS = [
    Codec("ldac", "LDAC", codec.LdacMediaCodecInformation),
    Codec("aptx-hd", "aptX HD", codec.AptxHdMediaCodecInformation),
    Codec("aptx", "aptX", codec.AptxMediaCodecInformation),
    Codec("aac", "AAC", None),
    Codec("sbc", "SBC", None),
]
BY_KEY = {c.key: c for c in CODECS}


def codec_info(endpoint):
    for capability in endpoint.capabilities:
        if capability.service_category == AVDTP_MEDIA_CODEC_SERVICE_CATEGORY:
            return capability.media_codec_information
    return None


def find_sinks(protocol) -> dict[str, object]:
    """{codec key: remote sink endpoint} for every codec we can send to this device."""
    sinks = {}
    for c in CODECS:
        if c.key == "aac":
            if not _aac_available():
                continue
            sink = protocol.find_remote_sink_by_codec(AVDTP_AUDIO_MEDIA_TYPE, A2DP_MPEG_2_4_AAC_CODEC_TYPE)
        elif c.key == "sbc":
            sink = protocol.find_remote_sink_by_codec(AVDTP_AUDIO_MEDIA_TYPE, A2DP_SBC_CODEC_TYPE)
        else:
            sink = protocol.find_remote_sink_by_codec(
                AVDTP_AUDIO_MEDIA_TYPE, A2DP_NON_A2DP_CODEC_TYPE, c.info.VENDOR_ID, c.info.CODEC_ID)
        if sink:
            sinks[c.key] = sink
    return sinks


_aac_ok = None


def _aac_available() -> bool:
    global _aac_ok
    if _aac_ok is None:
        _aac_ok = aac.available()
    return _aac_ok


def choose(sinks: dict, preference: str) -> str | None:
    if preference != "auto":
        return preference if preference in sinks else None
    return next((c.key for c in CODECS if c.key in sinks), None)


def sample_rates(key: str, caps) -> list[int]:
    if key == "sbc":
        return [hz for hz in (48000, 44100, 32000, 16000)
                if caps.sampling_frequency & SbcMediaCodecInformation.SamplingFrequency.from_int(hz)]
    if key == "aac":
        # Media Foundation's AAC encoder only takes 44.1/48 kHz
        return [hz for hz in (48000, 44100)
                if caps.sampling_frequency & AacMediaCodecInformation.SamplingFrequency.from_int(hz)]
    return caps.sample_rates


def _aac_configuration(caps: AacMediaCodecInformation, rate: int) -> AacMediaCodecInformation:
    a = AacMediaCodecInformation
    object_type = (a.ObjectType.MPEG_2_AAC_LC if caps.object_type & a.ObjectType.MPEG_2_AAC_LC
                   else a.ObjectType.MPEG_4_AAC_LC)
    # bitrate 0 means the sink states no limit
    cap = caps.bitrate or aac.BITRATES[0]
    bitrate = next((b for b in aac.BITRATES if b <= cap), aac.BITRATES[-1])
    channels = a.Channels.STEREO if caps.channels & a.Channels.STEREO else a.Channels.MONO
    return a(object_type, a.SamplingFrequency.from_int(rate), channels, 0, bitrate)


def _sbc_configuration(caps: SbcMediaCodecInformation, rate: int) -> SbcMediaCodecInformation:
    s = SbcMediaCodecInformation

    def first(flags, options):
        return next(o for o in options if flags & o)

    return s(
        sampling_frequency=s.SamplingFrequency.from_int(rate),
        channel_mode=first(caps.channel_mode, [s.ChannelMode.JOINT_STEREO, s.ChannelMode.STEREO,
                                               s.ChannelMode.DUAL_CHANNEL, s.ChannelMode.MONO]),
        block_length=first(caps.block_length, [s.BlockLength.BL_16, s.BlockLength.BL_12,
                                               s.BlockLength.BL_8, s.BlockLength.BL_4]),
        subbands=first(caps.subbands, [s.Subbands.S_8, s.Subbands.S_4]),
        allocation_method=first(caps.allocation_method, [s.AllocationMethod.LOUDNESS, s.AllocationMethod.SNR]),
        minimum_bitpool_value=max(2, caps.minimum_bitpool_value),
        maximum_bitpool_value=min(SBC_MAX_BITPOOL, caps.maximum_bitpool_value),
    )


def configure(key: str, caps, rate: int) -> MediaCodecCapabilities:
    if key == "aac":
        return MediaCodecCapabilities(media_type=AVDTP_AUDIO_MEDIA_TYPE, media_codec_type=A2DP_MPEG_2_4_AAC_CODEC_TYPE,
                                      media_codec_information=_aac_configuration(caps, rate))
    if key == "sbc":
        return MediaCodecCapabilities(media_type=AVDTP_AUDIO_MEDIA_TYPE, media_codec_type=A2DP_SBC_CODEC_TYPE,
                                      media_codec_information=_sbc_configuration(caps, rate))
    return MediaCodecCapabilities(media_type=AVDTP_AUDIO_MEDIA_TYPE, media_codec_type=A2DP_NON_A2DP_CODEC_TYPE,
                                  media_codec_information=caps.configuration(rate))


def session_factory(key: str, configuration: MediaCodecCapabilities, rate: int, ldac_quality: str = "auto"):
    """Returns (peer_mtu) -> Session for the negotiated configuration."""
    if key == "ldac":
        return lambda mtu: LdacSession(mtu, rate, LDAC_QUALITY[ldac_quality])
    if key == "aptx-hd":
        return lambda mtu: AptxSession(mtu, rate, hd=True, packet_ms=20, name="aptX HD")
    if key == "aptx":
        return lambda mtu: AptxSession(mtu, rate, hd=False, packet_ms=20, name="aptX")
    if key == "aac":
        return lambda mtu: AacSession(mtu, rate, configuration.media_codec_information.bitrate)
    config_bytes = bytes(configuration.media_codec_information)
    return lambda mtu: SbcSession(mtu, config_bytes)
