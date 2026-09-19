"""TTS: message placement, formats, base64 decode, streaming."""
import json

import httpx
import respx

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


@respx.mock
def test_mp3_downgrades_to_wav_for_home_assistant(client, auth):
    """HA's built-in OpenAI TTS requests mp3 with no way to configure it, and
    the Assist pipeline never forwards preferred_format, so refusing would
    leave it unusable. The response is still typed audio/wav, so the wire
    description stays truthful."""
    wav = wav_bytes()
    route = respx.post(MIMO_URL).mock(return_value=_audio_ok(wav))
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "mp3"}, headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content == wav
    assert json.loads(route.calls[0].request.content)["audio"]["format"] == "wav"


def test_mp3_rejected_when_downgrade_disabled(client, auth, monkeypatch):
    """The strict contract remains available for callers that read Content-Type."""
    from app import main
    monkeypatch.setattr(main.settings, "allow_format_downgrade", False)
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "mp3"}, headers=auth)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "unsupported_response_format"
    assert "does not transcode" in err["message"]


def test_unknown_format_still_rejected(client, auth):
    """Downgrade covers formats MiMo cannot emit, not typos."""
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "flac2"}, headers=auth)
    assert r.status_code == 400


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
