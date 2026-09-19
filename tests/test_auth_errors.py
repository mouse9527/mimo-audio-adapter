"""Authentication, key passthrough, upstream error mapping, health."""
import json

import httpx
import respx

from .conftest import MIMO_URL, b64, wav_bytes


def _audio_ok(data: bytes = b"\x00\x00"):
    return httpx.Response(200, json={"choices": [{"message": {"audio": {"data": b64(data)}}}]})


# ---- credential relay ----------------------------------------------------

@respx.mock
def test_inbound_authorization_forwarded_verbatim(client):
    """The adapter holds no key: it relays whatever the caller supplied."""
    route = respx.post(MIMO_URL).mock(return_value=_audio_ok())
    client.post("/v1/audio/speech", json={"input": "x", "response_format": "wav"},
                headers={"Authorization": "Bearer mimo-key-from-litellm"})
    assert route.calls[0].request.headers["authorization"] == "Bearer mimo-key-from-litellm"


@respx.mock
def test_non_bearer_scheme_relayed_unchanged(client):
    """Relay verbatim rather than reshaping: the caller owns the scheme."""
    route = respx.post(MIMO_URL).mock(return_value=_audio_ok())
    client.post("/v1/audio/transcriptions",
                files={"file": ("a.wav", wav_bytes(), "audio/wav")},
                headers={"Authorization": "Token abc123"})
    assert route.calls[0].request.headers["authorization"] == "Token abc123"


def test_missing_authorization_returns_401(client):
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "wav"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_authorization"


def test_adapter_holds_no_key_of_its_own():
    """No credential may be sourced from the adapter's own configuration."""
    from app.config import Settings
    fields = set(Settings.__dataclass_fields__)
    assert not {f for f in fields if "key" in f or "token" in f or "secret" in f}


# ---- upstream errors ------------------------------------------------------

@respx.mock
def test_upstream_429_maps_to_rate_limit(client, auth):
    respx.post(MIMO_URL).mock(return_value=httpx.Response(429, json={"error": {"message": "slow down"}}))
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "wav"}, headers=auth)
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "rate_limit_error"
    assert "slow down" in r.json()["error"]["message"]


@respx.mock
def test_upstream_401_becomes_502_not_reflected(client, auth):
    """The caller already authenticated; a bad upstream key is our config fault."""
    respx.post(MIMO_URL).mock(return_value=httpx.Response(401, json={"error": {"message": "bad key"}}))
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "wav"}, headers=auth)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_auth_failed"


@respx.mock
def test_upstream_timeout_maps_to_504(client, auth):
    respx.post(MIMO_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "wav"}, headers=auth)
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "upstream_timeout"


@respx.mock
def test_connection_error_maps_to_502(client, auth):
    respx.post(MIMO_URL).mock(side_effect=httpx.ConnectError("refused"))
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "wav"}, headers=auth)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_unreachable"


@respx.mock
def test_malformed_upstream_shape_maps_to_502(client, auth):
    respx.post(MIMO_URL).mock(return_value=httpx.Response(200, json={"choices": [{"message": {}}]}))
    r = client.post("/v1/audio/speech", json={"input": "x", "response_format": "wav"}, headers=auth)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_malformed"


@respx.mock
def test_asr_missing_content_maps_to_502(client, auth):
    respx.post(MIMO_URL).mock(return_value=httpx.Response(200, json={"choices": []}))
    r = client.post("/v1/audio/transcriptions",
                    files={"file": ("a.wav", wav_bytes(), "audio/wav")}, headers=auth)
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"


# ---- health / models ------------------------------------------------------

def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    body = client.get("/ready").json()
    assert body["status"] == "ready"


def test_models_endpoint_required_by_ha_component(client):
    """The HA OpenAI-compatible component calls models.list() during setup."""
    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["data"]}
    assert {"mimo-v2.5-asr", "mimo-v2.5-tts"} <= ids


def test_error_envelope_has_openai_shape(client):
    r = client.post("/v1/audio/speech", json={}, headers={"Authorization": "Bearer k"})
    err = r.json()["error"]
    assert set(err) == {"message", "type", "code", "param"}


@respx.mock
def test_api_key_never_logged(client, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    respx.post(MIMO_URL).mock(return_value=_audio_ok())
    secret = "super-secret-mimo-key-zzz"
    client.post("/v1/audio/speech", json={"input": "x", "response_format": "wav"},
                headers={"Authorization": f"Bearer {secret}"})
    assert secret not in caplog.text
    assert "Bearer" not in caplog.text
