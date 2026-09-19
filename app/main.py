"""OpenAI Audio API -> MiMo chat/completions protocol adapter."""
from __future__ import annotations

import json
import logging
import time
from typing import Annotated, Any

from fastapi import FastAPI, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import mimo, transcode
from .audio import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    detect_audio_mime,
    ensure_wav,
    pcm_to_wav,
    resolve_speech_format,
)
from .config import settings
from .errors import AdapterError, error_response, invalid_request

logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mimo-adapter")

app = FastAPI(title="MiMo Audio Adapter", version="1.0.0", docs_url=None, redoc_url=None)

# MiMo accepts only these; anything else degrades to auto rather than failing.
_ASR_LANGUAGES = {"auto", "zh", "en"}
_JSON_ASR_FORMATS = {"json", "verbose_json"}
_TEXT_ASR_FORMATS = {"text"}


@app.exception_handler(AdapterError)
async def _adapter_error_handler(_: Request, exc: AdapterError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())


@app.exception_handler(Exception)
async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error: %s", type(exc).__name__)
    return error_response(500, "Internal adapter error", "api_error", "internal_error")


def _caller_credential(authorization: str | None) -> str | None:
    """The inbound Authorization IS the upstream credential; relay it as-is."""
    return authorization


# ---- health ---------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    """Readiness reflects configuration validity, not upstream reachability.

    Probing MiMo here would tie pod readiness to a third party and let an
    upstream blip take every replica out of the Service.
    """
    return {
        "status": "ready",
        "upstream": settings.mimo_base_url,
        "transcoding": transcode.ffmpeg_available(),
    }


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """Required by the HA OpenAI-compatible component, which calls models.list()."""
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": settings.asr_model, "object": "model", "created": now, "owned_by": "xiaomi-mimo"},
            {"id": settings.tts_model, "object": "model", "created": now, "owned_by": "xiaomi-mimo"},
        ],
    }


# ---- ASR ------------------------------------------------------------------

@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: Annotated[UploadFile, Form()],
    model: Annotated[str | None, Form()] = None,
    language: Annotated[str | None, Form()] = None,
    prompt: Annotated[str | None, Form()] = None,
    response_format: Annotated[str | None, Form()] = None,
    temperature: Annotated[str | None, Form()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    fmt = (response_format or "json").lower()
    if fmt not in _JSON_ASR_FORMATS | _TEXT_ASR_FORMATS:
        raise invalid_request(
            f"response_format={fmt!r} is not supported. MiMo returns plain text without timestamps; "
            f"supported: ['json', 'text', 'verbose_json'].",
            code="unsupported_response_format",
            param="response_format",
        )

    data = await file.read()
    if not data:
        raise invalid_request("Uploaded audio file is empty", code="empty_audio", param="file")
    if len(data) > settings.max_audio_bytes:
        raise AdapterError(
            413,
            f"Audio is {len(data)} bytes; limit is {settings.max_audio_bytes} "
            "(MiMo caps the base64-encoded payload at 10MB).",
            "invalid_request_error",
            "audio_too_large",
        )

    mime = detect_audio_mime(file.filename, file.content_type, data)
    import base64 as _b64
    b64 = _b64.b64encode(data).decode("ascii")

    lang = (language or settings.asr_language or "auto").lower()
    if lang not in _ASR_LANGUAGES:
        log.info("language=%r unsupported by MiMo; falling back to auto", lang)
        lang = "auto"

    if prompt:
        log.info("Ignoring 'prompt': MiMo ASR has no equivalent parameter")
    if temperature:
        log.info("Ignoring 'temperature': MiMo ASR has no equivalent parameter")

    payload = {
        "model": model or settings.asr_model,
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": f"data:{mime};base64,{b64}"}}
        ]}],
        "asr_options": {"language": lang},
    }
    log.info("ASR: %d bytes, mime=%s, language=%s, format=%s", len(data), mime, lang, fmt)

    result = await mimo.post_json(payload, _caller_credential(authorization))
    text = mimo.extract_text(result)

    if fmt in _TEXT_ASR_FORMATS:
        return Response(content=text, media_type="text/plain; charset=utf-8")
    if fmt == "verbose_json":
        # LiteLLM injects response_format=verbose_json on its own, so this must
        # succeed. MiMo provides no timestamps, so segments/words are omitted
        # rather than fabricated.
        return JSONResponse({"task": "transcribe", "language": lang, "duration": None, "text": text, "segments": []})
    return JSONResponse({"text": text})


# ---- TTS ------------------------------------------------------------------

@app.post("/v1/audio/speech")
async def speech(request: Request, authorization: Annotated[str | None, Header()] = None) -> Response:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise invalid_request("Request body must be valid JSON", code="invalid_json") from exc
    if not isinstance(body, dict):
        raise invalid_request("Request body must be a JSON object", code="invalid_json")

    text = body.get("input")
    if not isinstance(text, str) or not text.strip():
        raise invalid_request("'input' is required and must be a non-empty string", code="missing_input", param="input")

    requested_format = body.get("response_format") or settings.tts_format
    mimo_format, content_type, transcode_to = resolve_speech_format(requested_format)
    voice = body.get("voice") or settings.tts_voice
    voice = settings.voice_aliases.get(voice, voice)
    stream = bool(body.get("stream", False))

    if body.get("speed") not in (None, 1, 1.0):
        # MiMo exposes no rate control; a style hint would not be an equivalent.
        log.info("Ignoring 'speed'=%r: MiMo has no speed parameter", body.get("speed"))

    messages: list[dict[str, str]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "user", "content": instructions})
    # The text to synthesize must be the assistant message; MiMo ignores it in
    # the user message, which carries style direction instead.
    messages.append({"role": "assistant", "content": text})

    payload: dict[str, Any] = {
        "model": body.get("model") or settings.tts_model,
        "messages": messages,
        "audio": {"format": mimo_format, "voice": voice},
    }
    log.info("TTS: %d chars, voice=%s, format=%s, stream=%s", len(text), voice, mimo_format, stream)

    auth = _caller_credential(authorization)

    if stream and transcode_to:
        # A transcoded container needs the whole stream before it can be framed.
        log.info("stream=true with %s: buffering, transcoding is not incremental", transcode_to)
        stream = False

    if stream and mimo_format == "pcm16":
        return StreamingResponse(
            mimo.stream_audio_chunks({**payload, "stream": True}, auth),
            media_type=content_type,
            headers={"X-Audio-Sample-Rate": str(PCM_SAMPLE_RATE), "X-Audio-Channels": str(PCM_CHANNELS)},
        )
    if stream:
        # WAV needs a length-accurate RIFF header, which is unknowable mid-stream;
        # emitting a placeholder length is not reliably playable. Collect, then frame.
        log.info("stream=true with wav: buffering to emit a correct RIFF header")
        pcm = bytearray()
        async for chunk in mimo.stream_audio_chunks({**payload, "stream": True, "audio": {**payload["audio"], "format": "pcm16"}}, auth):
            pcm.extend(chunk)
        if not pcm:
            raise AdapterError(502, "MiMo streamed no audio", "api_error", "upstream_empty_audio")
        return Response(content=pcm_to_wav(bytes(pcm)), media_type="audio/wav")

    result = await mimo.post_json(payload, auth)
    audio = mimo.extract_audio(result)
    if mimo_format == "wav":
        audio = ensure_wav(audio)
    if transcode_to:
        audio = await transcode.transcode(audio, transcode_to)
    return Response(content=audio, media_type=content_type)
