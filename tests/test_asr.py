"""ASR: mapping, upload handling, response formats."""
import json

import httpx
import respx

from .conftest import MIMO_URL, b64, mp3_bytes, wav_bytes


def _ok(text="打开客厅空调"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


@respx.mock
def test_transcription_maps_to_mimo_input_audio(client, auth):
    route = respx.post(MIMO_URL).mock(return_value=_ok())
    r = client.post("/v1/audio/transcriptions",
                    files={"file": ("a.wav", wav_bytes(), "audio/wav")},
                    data={"model": "mimo-v2.5-asr", "language": "zh"}, headers=auth)
    assert r.status_code == 200
    assert r.json() == {"text": "打开客厅空调"}

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "mimo-v2.5-asr"
    assert sent["asr_options"] == {"language": "zh"}
    part = sent["messages"][0]["content"][0]
    assert part["type"] == "input_audio"
    # data URI with the correct MIME, per MiMo's documented format
    assert part["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert part["input_audio"]["data"].endswith(b64(wav_bytes()))
    assert "format" not in part["input_audio"]


@respx.mock
def test_mp3_detected_by_magic_bytes(client, auth):
    route = respx.post(MIMO_URL).mock(return_value=_ok("测试"))
    r = client.post("/v1/audio/transcriptions",
                    files={"file": ("a.mp3", mp3_bytes(), "application/octet-stream")}, headers=auth)
    assert r.status_code == 200
    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/mpeg;base64,")


@respx.mock
def test_response_format_text_returns_plain_text(client, auth):
    respx.post(MIMO_URL).mock(return_value=_ok("你好"))
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav_bytes(), "audio/wav")},
                    data={"response_format": "text"}, headers=auth)
    assert r.status_code == 200
    assert r.text == "你好"
    assert r.headers["content-type"].startswith("text/plain")


@respx.mock
def test_verbose_json_accepted_because_litellm_injects_it(client, auth):
    """LiteLLM adds response_format=verbose_json itself; rejecting it would
    break every transcription request arriving through the proxy."""
    respx.post(MIMO_URL).mock(return_value=_ok("空调已打开"))
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav_bytes(), "audio/wav")},
                    data={"response_format": "verbose_json", "language": "zh"}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "空调已打开"
    assert body["task"] == "transcribe"
    # No fabricated timestamps: MiMo does not provide them.
    assert body["segments"] == []
    assert body["duration"] is None


@respx.mock
def test_unsupported_language_falls_back_to_auto(client, auth):
    route = respx.post(MIMO_URL).mock(return_value=_ok())
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav_bytes(), "audio/wav")},
                    data={"language": "fr"}, headers=auth)
    assert r.status_code == 200
    assert json.loads(route.calls[0].request.content)["asr_options"]["language"] == "auto"


@respx.mock
def test_prompt_is_ignored_not_forwarded(client, auth):
    route = respx.post(MIMO_URL).mock(return_value=_ok())
    client.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav_bytes(), "audio/wav")},
                data={"prompt": "smart home"}, headers=auth)
    assert "prompt" not in json.loads(route.calls[0].request.content)


def test_srt_rejected_with_openai_error_shape(client, auth):
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav_bytes(), "audio/wav")},
                    data={"response_format": "srt"}, headers=auth)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "unsupported_response_format"
    assert err["param"] == "response_format"


def test_unsupported_audio_format_rejected(client, auth):
    r = client.post("/v1/audio/transcriptions",
                    files={"file": ("a.flac", b"fLaC\x00\x00\x00\x22", "audio/flac")}, headers=auth)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unsupported_audio_format"


def test_empty_file_rejected(client, auth):
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"", "audio/wav")}, headers=auth)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "empty_audio"


def test_oversized_audio_returns_413(client, auth, monkeypatch):
    from app import config, main
    monkeypatch.setattr(config.settings, "max_audio_bytes", 1024)
    monkeypatch.setattr(main.settings, "max_audio_bytes", 1024)
    r = client.post("/v1/audio/transcriptions",
                    files={"file": ("big.wav", wav_bytes(4096), "audio/wav")}, headers=auth)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "audio_too_large"
