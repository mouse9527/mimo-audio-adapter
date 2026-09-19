"""MiMo chat/completions client plus incremental SSE decoding."""
from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx

from .config import settings
from .errors import AdapterError, map_upstream_status

log = logging.getLogger("mimo-adapter.mimo")

_DONE = "[DONE]"


def upstream_headers(client_authorization: str | None) -> dict[str, str]:
    """Forward the caller's credential upstream verbatim.

    The adapter holds no key of its own: the caller (LiteLLM, which decrypts it
    from its credential store) supplies it, and this only relays it. That keeps
    the secret in exactly one place.
    """
    if not client_authorization:
        raise AdapterError(
            401,
            "Missing Authorization header: the caller must supply the upstream credential.",
            "invalid_request_error",
            "missing_authorization",
        )
    return {"Authorization": client_authorization, "Content-Type": "application/json"}


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(settings.request_timeout, connect=settings.connect_timeout)


async def _raise_for_upstream(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    raw = await resp.aread()
    status, err_type, code = map_upstream_status(resp.status_code)
    detail = ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            err = parsed.get("error")
            detail = (err.get("message") if isinstance(err, dict) else None) or parsed.get("message") or ""
    except (ValueError, AttributeError):
        detail = raw[:200].decode("utf-8", "replace")
    msg = f"MiMo upstream returned {resp.status_code}"
    if detail:
        msg = f"{msg}: {detail}"
    raise AdapterError(status, msg, err_type, code)


async def post_json(payload: dict[str, Any], authorization: str | None) -> dict[str, Any]:
    """Non-streaming call to MiMo."""
    url = f"{settings.mimo_base_url}/chat/completions"
    headers = upstream_headers(authorization)
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.post(url, json=payload, headers=headers)
            await _raise_for_upstream(resp)
            return resp.json()
    except httpx.TimeoutException as exc:
        raise AdapterError(504, "MiMo upstream timed out", "api_error", "upstream_timeout") from exc
    except httpx.RequestError as exc:
        raise AdapterError(502, f"Cannot reach MiMo upstream: {type(exc).__name__}", "api_error", "upstream_unreachable") from exc
    except ValueError as exc:
        raise AdapterError(502, "MiMo returned a non-JSON response", "api_error", "upstream_malformed") from exc


async def stream_audio_chunks(payload: dict[str, Any], authorization: str | None) -> AsyncIterator[bytes]:
    """Yield decoded audio bytes from MiMo's SSE stream."""
    url = f"{settings.mimo_base_url}/chat/completions"
    headers = upstream_headers(authorization)
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                await _raise_for_upstream(resp)
                buffer = ""
                async for raw in resp.aiter_text():
                    buffer += raw
                    # Split only on completed event delimiters; a partial trailing
                    # event stays in the buffer until the rest of it arrives.
                    events, buffer = _split_events(buffer)
                    for event in events:
                        for audio in _audio_from_event(event):
                            yield audio
                for event in _flush(buffer):
                    for audio in _audio_from_event(event):
                        yield audio
    except httpx.TimeoutException as exc:
        raise AdapterError(504, "MiMo upstream timed out during streaming", "api_error", "upstream_timeout") from exc
    except httpx.RequestError as exc:
        raise AdapterError(502, f"Cannot reach MiMo upstream: {type(exc).__name__}", "api_error", "upstream_unreachable") from exc


def _split_events(buffer: str) -> tuple[list[str], str]:
    """Return complete SSE events plus the unconsumed remainder.

    A TCP read can split anywhere, including mid-base64, so only text up to the
    last blank-line delimiter is safe to parse.
    """
    normalized = buffer.replace("\r\n", "\n")
    parts = normalized.split("\n\n")
    return parts[:-1], parts[-1]


def _flush(buffer: str) -> list[str]:
    tail = buffer.strip()
    return [tail] if tail else []


def _audio_from_event(event: str) -> Iterator[bytes]:
    """Extract and decode audio from one complete SSE event.

    Each event's data is independently base64-encoded, so it is decoded on its
    own; concatenating base64 text across events would corrupt it via padding.
    """
    for line in event.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == _DONE:
            continue
        try:
            obj = json.loads(data)
        except ValueError:
            log.warning("Skipping SSE event with non-JSON data (%d chars)", len(data))
            continue
        b64 = _extract_b64(obj)
        if not b64:
            continue
        try:
            yield base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            # Do not pad or splice to "repair" it; that would mask upstream faults.
            raise AdapterError(502, "MiMo returned undecodable base64 audio", "api_error", "upstream_malformed") from exc


def _extract_b64(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        for key in ("delta", "message"):
            node = choice.get(key)
            if isinstance(node, dict):
                audio = node.get("audio")
                if isinstance(audio, dict) and audio.get("data"):
                    return audio["data"]
    return None


def extract_text(payload: dict[str, Any]) -> str:
    """Pull the ASR transcript out of choices[0].message.content."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AdapterError(502, "MiMo response contained no choices", "api_error", "upstream_malformed")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise AdapterError(502, "MiMo response contained no message", "api_error", "upstream_malformed")
    content = message.get("content")
    if isinstance(content, list):  # tolerate structured content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if content is None:
        raise AdapterError(502, "MiMo response contained no transcript text", "api_error", "upstream_malformed")
    return str(content).strip()


def extract_audio(payload: dict[str, Any]) -> bytes:
    """Pull non-streaming audio from choices[0].message.audio.data."""
    b64 = _extract_b64(payload)
    if not b64:
        raise AdapterError(502, "MiMo response contained no audio data", "api_error", "upstream_malformed")
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AdapterError(502, "MiMo returned undecodable base64 audio", "api_error", "upstream_malformed") from exc
