"""Audio format contract and WAV framing (stdlib only, no ffmpeg)."""
from __future__ import annotations

import struct

from .errors import invalid_request

# MiMo streams 24kHz PCM16LE mono.
PCM_SAMPLE_RATE = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2

# What MiMo's audio.format accepts.
MIMO_FORMATS = {"wav", "pcm16"}

# OpenAI response_format -> (mimo format, content type)
# mp3/opus/aac/flac are deliberately absent: MiMo cannot produce them and this
# adapter does not transcode. See README "Known limitations".
# OpenAI response_format -> (MiMo audio.format, content type, transcode target)
# MiMo emits only wav/pcm16; anything else is transcoded from wav on the way out.
SUPPORTED_SPEECH_FORMATS: dict[str, tuple[str, str, str | None]] = {
    "wav": ("wav", "audio/wav", None),
    "pcm": ("pcm16", "application/octet-stream", None),
    "mp3": ("wav", "audio/mpeg", "mp3"),
    "opus": ("wav", "audio/ogg", "opus"),
    "aac": ("wav", "audio/aac", "aac"),
    "flac": ("wav", "audio/flac", "flac"),
}


def resolve_speech_format(response_format: str) -> tuple[str, str, str | None]:
    """Map an OpenAI response_format to what MiMo is asked for and what we return.

    Returns (mimo_format, content_type, transcode_to). A transcode target means
    MiMo produces wav and the adapter converts before responding.

    Transcoding exists for one reason: Home Assistant requests mp3, offers no
    way to configure that, and labels the bytes with the format it asked for
    rather than reading Content-Type. Returning wav under an mp3 label made HA
    skip its own ffmpeg pass and hand undecodable audio to the player. Since
    only the caller's requested format can be honoured here, honour it.
    """
    fmt = (response_format or "wav").lower()
    if fmt in SUPPORTED_SPEECH_FORMATS:
        return SUPPORTED_SPEECH_FORMATS[fmt]
    raise invalid_request(
        f"Unknown response_format={fmt!r}. Supported: {sorted(SUPPORTED_SPEECH_FORMATS)}.",
        code="unsupported_response_format",
        param="response_format",
    )


def wav_header(data_len: int, sample_rate: int = PCM_SAMPLE_RATE, channels: int = PCM_CHANNELS,
               sample_width: int = PCM_SAMPLE_WIDTH) -> bytes:
    """Build a 44-byte RIFF/WAVE header for a known payload length."""
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    return (
        b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, sample_width * 8)
        + b"data" + struct.pack("<I", data_len)
    )


def pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap raw PCM in a WAV container using its true length."""
    return wav_header(len(pcm)) + pcm


def looks_like_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def ensure_wav(data: bytes) -> bytes:
    """Return WAV bytes, wrapping raw PCM only if not already a RIFF container."""
    return data if looks_like_wav(data) else pcm_to_wav(data)


# ---- ASR input validation -------------------------------------------------

_MIME_BY_EXT = {
    "wav": "audio/wav",
    "wave": "audio/wav",
    "mp3": "audio/mpeg",
    "mpeg": "audio/mpeg",
    "mpga": "audio/mpeg",
}

SUPPORTED_ASR_EXTENSIONS = sorted(_MIME_BY_EXT)


def detect_audio_mime(filename: str | None, content_type: str | None, data: bytes) -> str:
    """Resolve the MIME type for MiMo's data URI.

    MiMo accepts only wav and mp3, so anything else is rejected up front rather
    than sent upstream to fail less clearly.
    """
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "audio/mpeg"

    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}:
        return "audio/wav"
    if ct in {"audio/mpeg", "audio/mp3", "audio/mpeg3", "audio/x-mpeg-3"}:
        return "audio/mpeg"

    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]

    raise invalid_request(
        "Unsupported audio format: MiMo accepts only WAV and MP3. "
        f"Got filename={filename!r}, content_type={content_type!r}.",
        code="unsupported_audio_format",
        param="file",
    )
