"""Configuration, sourced entirely from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _csv(name: str, default: str) -> list[str]:
    return [p.strip() for p in _env(name, default).split(",") if p.strip()]


@dataclass
class Settings:
    # Upstream endpoint. Configuration, not a credential.
    mimo_base_url: str = field(default_factory=lambda: _env("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1").rstrip("/"))

    asr_model: str = field(default_factory=lambda: _env("DEFAULT_ASR_MODEL", "mimo-v2.5-asr"))
    tts_model: str = field(default_factory=lambda: _env("DEFAULT_TTS_MODEL", "mimo-v2.5-tts"))
    asr_language: str = field(default_factory=lambda: _env("DEFAULT_ASR_LANGUAGE", "zh"))
    tts_voice: str = field(default_factory=lambda: _env("DEFAULT_TTS_VOICE", "冰糖"))
    tts_format: str = field(default_factory=lambda: _env("DEFAULT_TTS_FORMAT", "wav"))

    # Home Assistant's built-in OpenAI TTS requests mp3 and offers no way to
    # change it, so allow returning wav instead of refusing. See resolve_speech_format.
    allow_format_downgrade: bool = field(
        default_factory=lambda: _env("ALLOW_FORMAT_DOWNGRADE", "true").lower() in ("1", "true", "yes")
    )

    port: int = field(default_factory=lambda: int(_env("PORT", "8000")))
    request_timeout: float = field(default_factory=lambda: float(_env("REQUEST_TIMEOUT", "120")))
    connect_timeout: float = field(default_factory=lambda: float(_env("CONNECT_TIMEOUT", "10")))
    # MiMo caps the base64-encoded payload at 10MB; raw bytes therefore ~7.5MB.
    max_audio_bytes: int = field(default_factory=lambda: int(_env("MAX_AUDIO_BYTES", str(7 * 1024 * 1024))))
    voice_aliases: dict[str, str] = field(default_factory=lambda: _parse_aliases(_env("VOICE_ALIASES")))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())

    @property
    def preset_voices(self) -> list[str]:
        return _csv(
            "MIMO_VOICES",
            "mimo_default,冰糖,茉莉,苏打,白桦,Mia,Chloe,Milo,Dean",
        )


def _parse_aliases(raw: str) -> dict[str, str]:
    """Parse 'alloy=冰糖,nova=茉莉' into a mapping."""
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return out


settings = Settings()
