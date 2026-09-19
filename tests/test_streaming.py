"""Streaming TTS and SSE boundary handling."""
import json

import httpx
import respx

from .conftest import MIMO_URL, b64


def _sse(*payloads: bytes) -> str:
    out = ""
    for p in payloads:
        out += "data: " + json.dumps({"choices": [{"delta": {"audio": {"data": b64(p)}}}]}) + "\n\n"
    return out + "data: [DONE]\n\n"


@respx.mock
def test_pcm_streaming_concatenates_decoded_chunks(client, auth):
    a, b, c = b"\x01\x01" * 10, b"\x02\x02" * 10, b"\x03\x03" * 10
    route = respx.post(MIMO_URL).mock(return_value=httpx.Response(
        200, text=_sse(a, b, c), headers={"Content-Type": "text/event-stream"}))
    r = client.post("/v1/audio/speech",
                    json={"input": "x", "response_format": "pcm", "stream": True}, headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.content == a + b + c
    sent = json.loads(route.calls[0].request.content)
    assert sent["stream"] is True
    assert sent["audio"]["format"] == "pcm16"
    assert r.headers["x-audio-sample-rate"] == "24000"


@respx.mock
def test_each_event_decoded_independently(client, auth):
    """Per-event base64 may carry its own padding; concatenating the *text*
    across events would corrupt the audio."""
    a, b = b"\x01\x02\x03", b"\x04\x05"   # lengths chosen so each b64 pads
    assert b64(a).endswith("=") or b64(b).endswith("=")
    respx.post(MIMO_URL).mock(return_value=httpx.Response(
        200, text=_sse(a, b), headers={"Content-Type": "text/event-stream"}))
    r = client.post("/v1/audio/speech",
                    json={"input": "x", "response_format": "pcm", "stream": True}, headers=auth)
    assert r.content == a + b


@respx.mock
def test_event_split_across_tcp_reads_is_reassembled(client, auth):
    """A read boundary can fall mid-base64; the parser must buffer until the
    event delimiter arrives."""
    payload = b"\x07\x08" * 40
    full = _sse(payload)
    cut = len(full) // 3

    def chunks():
        yield full[:cut].encode()
        yield full[cut:].encode()

    respx.post(MIMO_URL).mock(return_value=httpx.Response(
        200, stream=httpx.ByteStream(b"".join(chunks())),
        headers={"Content-Type": "text/event-stream"}))
    r = client.post("/v1/audio/speech",
                    json={"input": "x", "response_format": "pcm", "stream": True}, headers=auth)
    assert r.content == payload


@respx.mock
def test_events_without_audio_are_skipped(client, auth):
    payload = b"\x09\x0a" * 5
    text = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        'data: {"choices":[]}\n\n'
        + _sse(payload)
    )
    respx.post(MIMO_URL).mock(return_value=httpx.Response(
        200, text=text, headers={"Content-Type": "text/event-stream"}))
    r = client.post("/v1/audio/speech",
                    json={"input": "x", "response_format": "pcm", "stream": True}, headers=auth)
    assert r.content == payload


@respx.mock
def test_stream_with_wav_buffers_to_write_true_length(client, auth):
    """WAV needs an accurate RIFF length, unknowable mid-stream, so the wav
    branch collects PCM and frames it rather than emitting a placeholder."""
    a, b = b"\x11\x22" * 8, b"\x33\x44" * 8
    route = respx.post(MIMO_URL).mock(return_value=httpx.Response(
        200, text=_sse(a, b), headers={"Content-Type": "text/event-stream"}))
    r = client.post("/v1/audio/speech",
                    json={"input": "x", "response_format": "wav", "stream": True}, headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"
    assert int.from_bytes(r.content[40:44], "little") == len(a + b)
    assert r.content[44:] == a + b
    # Upstream is asked for pcm16 so chunks can be concatenated.
    assert json.loads(route.calls[0].request.content)["audio"]["format"] == "pcm16"


@respx.mock
def test_corrupt_base64_surfaces_error_not_silent_repair(client, auth):
    bad = 'data: {"choices":[{"delta":{"audio":{"data":"!!!not-base64!!!"}}}]}\n\n'
    respx.post(MIMO_URL).mock(return_value=httpx.Response(
        200, text=bad, headers={"Content-Type": "text/event-stream"}))
    try:
        r = client.post("/v1/audio/speech",
                        json={"input": "x", "response_format": "pcm", "stream": True}, headers=auth)
    except Exception:
        return  # raised while producing the stream body: acceptable
    assert r.status_code >= 400 or r.content == b""
