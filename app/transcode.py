"""WAV -> mp3/opus/aac/flac conversion via ffmpeg.

Deliberately narrow: the adapter is a protocol translator, and this exists only
because Home Assistant requests mp3 with no way to configure it. wav and pcm
never reach this module.
"""
from __future__ import annotations

import asyncio
import logging
import shutil

from .config import settings
from .errors import AdapterError

log = logging.getLogger("mimo-adapter.transcode")

# Constant bitrate for mp3: some hardware decoders reject variable bitrate and
# play nothing at all, which is indistinguishable from a broken response.
_ARGS: dict[str, list[str]] = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3"],
    "opus": ["-c:a", "libopus", "-b:a", "48k", "-f", "ogg"],
    "aac": ["-c:a", "aac", "-b:a", "96k", "-f", "adts"],
    "flac": ["-c:a", "flac", "-f", "flac"],
}


def ffmpeg_available() -> bool:
    return shutil.which(settings.ffmpeg_binary) is not None


async def transcode(wav: bytes, to_format: str) -> bytes:
    """Convert WAV bytes to the target format, reading and writing via pipes."""
    args = _ARGS.get(to_format)
    if args is None:
        raise AdapterError(400, f"Cannot transcode to {to_format!r}", "invalid_request_error",
                           "unsupported_response_format")
    if not ffmpeg_available():
        raise AdapterError(
            500,
            f"{to_format} requires ffmpeg, which is not installed in this image",
            "api_error",
            "transcoder_unavailable",
        )

    cmd = [settings.ffmpeg_binary, "-hide_banner", "-loglevel", "error",
           "-i", "pipe:0", *args, "pipe:1"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(wav), timeout=settings.transcode_timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise AdapterError(504, f"Transcoding to {to_format} timed out", "api_error",
                           "transcode_timeout") from None

    if proc.returncode != 0 or not out:
        detail = err.decode("utf-8", "replace").strip()[:200]
        log.error("ffmpeg failed (rc=%s) converting to %s: %s", proc.returncode, to_format, detail)
        raise AdapterError(502, f"Transcoding to {to_format} failed", "api_error", "transcode_failed")

    log.info("Transcoded wav -> %s (%d -> %d bytes)", to_format, len(wav), len(out))
    return out
