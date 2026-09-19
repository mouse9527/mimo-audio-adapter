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
SUPPORTED_SPEECH_FORMATS: dict[str, tuple[str, str]] = {
    "wav": ("wav", "audio/wav"),
    "pcm": ("pcm16", "application/octet-stream"),
}

UNSUPPORTED_SPEECH_FORMATS = {"mp3", "opus", "aac", "flac"}


def resolve_speech_format(response_format: str, allow_downgrade: bool = False) -> tuple[str, str]:
    """Map an OpenAI response_format to MiMo's, or reject it explicitly.

    MiMo emits only wav/pcm16 and this adapter does not transcode, so the
    formats it cannot produce are normally refused rather than silently
    substituted: a caller that pairs the bytes with the format name it
    requested would mislabel them downstream.

    allow_downgrade exists because Home Assistant's built-in OpenAI TTS asks
    for mp3 with no way to configure otherwise — the Assist pipeline never
    passes preferred_format through — so refusing would leave it unusable.
    When enabled, an unsupported format yields wav, and the response is still
    typed audio/wav so the wire description stays truthful; HA transcodes with
    its own ffmpeg when it needs a different container.
    """
    fmt = (response_format or "wav").lower()
    if fmt in SUPPORTED_SPEECH_FORMATS:
        return SUPPORTED_SPEECH_FORMATS[fmt]
    if fmt in UNSUPPORTED_SPEECH_FORMATS:
        if allow_downgrade:
            return SUPPORTED_SPEECH_FORMATS["wav"]
        raise invalid_request(
            f"response_format={fmt!r} is not supported: MiMo emits only wav/pcm and this adapter does not transcode. "
            f"Request 'wav' and let the caller convert. Supported: {sorted(SUPPORTED_SPEECH_FORMATS)}.",
            code="unsupported_response_format",
            param="response_format",
        )
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
