"""TTS: message placement, formats, base64 decode, streaming."""
import json
import shutil

import httpx
import pytest
import respx

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed in this environment"
)

from .conftest import MIMO_URL, b64, wav_bytes


def _audio_ok(data: bytes):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "audio": {"data": b64(data)}}}]})


@respx.mock
def test_text_goes_in_assistant_message(client, auth):
    """MiMo ignores synthesis text placed in the user message."""
    route = respx.post(MIMO_URL).mock(return_value=_audio_ok(wav_bytes()))
    r = client.post("/v1/audio/speech",
                    json={"model": "mimo-v2.5-tts", "input": "客厅空调已经打开", "voice": "冰糖",
                          "response_format": "wav"}, headers=auth)
    assert r.status_code == 200
    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"][-1] == {"role": "assistant", "content": "客厅空调已经打开"}
    assert sent["audio"] == {"format": "wav", "voice": "冰糖"}
    assert "stream" not in sent


@respx.mock
def test_returns_decoded_binary_not_base64_json(client, auth):
    wav = wav_bytes()
    respx.post(MIMO_URL).mock(return_value=_audio_ok(wav))
    r = client.post("/v1/audio/speech", json={"input": "hi", "response_format": "wav"}, headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content == wav
    assert r.content[:4] == b"RIFF"


@respx.mock
def test_instructions_become_user_message(client, auth):
    route = respx.post(MIMO_URL).mock(return_value=_audio_ok(wav_bytes()))
    client.post("/v1/audio/speech",
                json={"input": "好的", "instructions": "轻快一些", "response_format": "wav"}, headers=auth)
    msgs = json.loads(route.calls[0].request.content)["messages"]
    assert msgs[0] == {"role": "user", "content": "轻快一些"}
    assert msgs[1]["role"] == "assistant"


@respx.mock
def test_voice_alias_mapped(client, auth, monkeypatch):
    from app import main
    monkeypatch.setattr(main.settings, "voice_aliases", {"alloy": "冰糖"})
    route = respx.post(MIMO_URL).mock(return_value=_audio_ok(wav_bytes()))
    client.post("/v1/audio/speech", json={"input": "x", "voice": "alloy", "response_format": "wav"}, headers=auth)
    assert json.loads(route.calls[0].request.content)["audio"]["voice"] == "冰糖"


@respx.mock
def test_raw_pcm_from_mimo_is_wrapped_in_wav(client, auth):
    pcm = b"\x01\x02" * 100
    respx.post(MIMO_URL).mock(return_value=_audio_ok(pcm))
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "wav"}, headers=auth)
    assert r.content[:4] == b"RIFF"
    # True length in the header, not a placeholder.
    assert int.from_bytes(r.content[40:44], "little") == len(pcm)
    assert r.content[44:] == pcm


@respx.mock
def test_pcm_format_returns_headerless_octet_stream(client, auth):
    pcm = b"\x03\x04" * 50
    route = respx.post(MIMO_URL).mock(return_value=_audio_ok(pcm))
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "pcm"}, headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.content == pcm
    assert json.loads(route.calls[0].request.content)["audio"]["format"] == "pcm16"


@requires_ffmpeg
@respx.mock
def test_mp3_is_transcoded_and_labelled_mp3(client, auth):
    """HA requests mp3, cannot be configured otherwise, and labels the bytes
    with the format it asked for rather than reading Content-Type. Returning
    wav under an mp3 label made HA skip its own ffmpeg pass and hand
    undecodable audio to the player, so the request is honoured for real."""
    route = respx.post(MIMO_URL).mock(return_value=_audio_ok(wav_bytes()))
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "mp3"}, headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    # Real MP3: ID3 tag or a frame sync, never a RIFF header.
    assert r.content[:3] == b"ID3" or r.content[0] == 0xFF
    assert r.content[:4] != b"RIFF"
    # MiMo is still asked for wav; the conversion happens here.
    assert json.loads(route.calls[0].request.content)["audio"]["format"] == "wav"


@requires_ffmpeg
@respx.mock
def test_flac_is_transcoded(client, auth):
    respx.post(MIMO_URL).mock(return_value=_audio_ok(wav_bytes()))
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "flac"}, headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/flac"
    assert r.content[:4] == b"fLaC"


@respx.mock
def test_wav_is_not_transcoded(client, auth):
    """The default path stays free of ffmpeg."""
    wav = wav_bytes()
    respx.post(MIMO_URL).mock(return_value=_audio_ok(wav))
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "wav"}, headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content == wav


def test_unknown_format_still_rejected(client, auth):
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "flac2"}, headers=auth)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unsupported_response_format"


def test_missing_input_rejected(client, auth):
    r = client.post("/v1/audio/speech", json={"voice": "冰糖"}, headers=auth)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_input"


def test_invalid_json_rejected(client, auth):
    r = client.post("/v1/audio/speech", content=b"not json",
                    headers={**auth, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_json"


@respx.mock
def test_speed_ignored_not_forwarded(client, auth):
    route = respx.post(MIMO_URL).mock(return_value=_audio_ok(wav_bytes()))
    client.post("/v1/audio/speech", json={"input": "x", "speed": 1.5, "response_format": "wav"}, headers=auth)
    assert "speed" not in json.loads(route.calls[0].request.content)
