# MiMo Audio Adapter

Translates the **OpenAI Audio API** into **Xiaomi MiMo v2.5**'s chat/completions
calls, so Home Assistant and LiteLLM can use MiMo for STT/TTS through the
interface they already speak.

```
iPhone → HA Assist → ha-openai-compatible → LiteLLM → this adapter → MiMo
```

Protocol conversion only: no model, no ASR/TTS engine, no ffmpeg, no state.

## Interface mapping

### ASR — `POST /v1/audio/transcriptions`

| OpenAI | MiMo | Notes |
|---|---|---|
| `file` (multipart) | `input_audio.data` | base64 data URI: `data:audio/wav;base64,…` |
| `language` | `asr_options.language` | only `auto`/`zh`/`en`; anything else → `auto` |
| `model` | `model` | default `mimo-v2.5-asr` |
| `prompt` | — | ignored (no MiMo equivalent) |
| `temperature` | — | ignored |
| — | ← `choices[0].message.content` | the transcript |

`response_format`: `json` (default), `text`, `verbose_json`.
`verbose_json` returns empty `segments` and `duration: null` — MiMo emits no
timestamps, and none are fabricated. **LiteLLM injects
`response_format=verbose_json` on its own**, so it must be accepted.
`srt`/`vtt` are rejected with 400.

### TTS — `POST /v1/audio/speech`

| OpenAI | MiMo | Notes |
|---|---|---|
| `input` | `messages[assistant].content` | **must be the assistant message** — MiMo ignores text in the user message |
| `instructions` | `messages[user].content` | style direction; never spoken |
| `voice` | `audio.voice` | alias-mapped via `VOICE_ALIASES` |
| `response_format` | `audio.format` | `wav`→`wav`, `pcm`→`pcm16` |
| `speed` | — | ignored; MiMo has no rate control |
| `stream` | `stream` | see below |
| — | ← `choices[0].message.audio.data` | base64, decoded to binary |
| — | ← `choices[0].delta.audio.data` | streaming chunks |

Voices: `mimo_default`, 冰糖, 茉莉, 苏打, 白桦 (zh), Mia, Chloe, Milo, Dean (en).

## Compatibility range

**Supported:** `wav`, `pcm` (24kHz PCM16LE mono, headerless,
`application/octet-stream`).

**Rejected with 400:** `mp3`, `opus`, `aac`, `flac`.
MiMo emits only wav/pcm16 and this adapter does not transcode. The rejection is
deliberate rather than a silent downgrade: the HA OpenAI-compatible component
pairs the bytes with **the format name it requested** and never reads
`Content-Type`, so returning WAV labeled "mp3" would mislabel it downstream and
could skip a conversion HA would otherwise perform. Ask for `wav` and let HA's
own ffmpeg layer (`preferred_format`) handle mp3.

## Streaming

`stream: true` with `response_format: pcm` streams decoded PCM as it arrives —
the lowest-latency path.

`stream: true` with `wav` **buffers**, then frames the audio with a correct RIFF
header. A WAV header needs an accurate length, which is unknowable mid-stream;
a `0xFFFFFFFF` placeholder is not reliably playable, so correctness wins here.

**Streaming currently yields no end-to-end benefit through Home Assistant.**
Not because HA lacks streaming — HA core does stream (`TTSCache.async_stream_data`,
`/api/tts_proxy`) — but because the `ha-openai-compatible` component accumulates
every chunk into a `bytearray` and returns only when complete. Reducing
time-to-first-sound requires changing that component, not this adapter.

SSE handling: each event's `audio.data` is decoded **independently** (per-event
base64 carries its own padding, so concatenating the text would corrupt it).
An incremental parser buffers partial events, because a TCP read can split
mid-base64. Corrupt base64 raises an error rather than being "repaired".

## Credentials

**The adapter holds no API key.** `MIMO_BASE_URL` is configuration — an
endpoint, not a secret. The credential comes from the caller:

```
LiteLLM  ──decrypts MiMo key from its credential store──►  Authorization: Bearer …
   │
   └──►  adapter  ──relays the header verbatim──►  MiMo
```

The adapter only forwards. There is no key in its environment, no Secret, no
fallback credential — so the secret lives in exactly one place, and rotating it
in LiteLLM is the whole operation. A request without `Authorization` is
rejected with 401; the adapter has nothing to fall back to, by design.

Upstream `401/403` map to **502**, not 401: the caller's credential was
rejected by MiMo, which is an upstream condition rather than a failure to
authenticate against this adapter.

Logs never contain credentials, `Authorization` headers, or base64 audio.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env          # no credential to fill in
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Tests (no API key required — MiMo is mocked):

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Docker

```bash
docker compose up --build
```

## Kubernetes

Images are published to `ghcr.io/mouse9527/mimo-audio-adapter` by CI, tagged
`sha-<short>` per commit plus `latest` on `main`.

The orchestration (Deployment + ClusterIP Service) is kept with the rest of the
cluster's manifests, not here — this repository owns the source and the image.
No Ingress: the adapter is reachable only in-cluster, by LiteLLM, at

```
http://mimo-audio-adapter.app.svc.cluster.local:8000/v1
```

No Secret is required: the adapter stores no credential (see
[ADR 0001](docs/adr/0001-credentials-belong-to-the-caller.md)).

## LiteLLM configuration

Add two models pointing at the adapter (Admin UI, or `/model/new`). The MiMo key
goes in a LiteLLM **credential**, which LiteLLM encrypts and injects:

```yaml
model_list:
  - model_name: mimo-tts
    litellm_params:
      model: openai/mimo-v2.5-tts
      api_base: http://mimo-audio-adapter.app.svc.cluster.local:8000/v1
      api_key: os.environ/MIMO_API_KEY   # or a stored credential
    model_info:
      mode: audio_speech

  - model_name: mimo-asr
    litellm_params:
      model: openai/mimo-v2.5-asr
      api_base: http://mimo-audio-adapter.app.svc.cluster.local:8000/v1
      api_key: os.environ/MIMO_API_KEY
    model_info:
      mode: audio_transcription
```

Verified against LiteLLM 1.101.0: it rewrites the model alias to the bare MiMo
name, appends `/audio/speech` or `/audio/transcriptions` to `api_base`, and
injects the decrypted credential as `Authorization`.

## Home Assistant

HA core has no OpenAI STT/TTS with a configurable base URL, so use
[`ha-openai-compatible`](https://github.com/adamjs83/ha-openai-compatible),
pointing its base URL at LiteLLM (or directly at this adapter).

Two component-side adjustments are required:

1. **Format** — its default is `mp3`, which this adapter rejects. Configure it to
   request `wav`, or let HA convert via `preferred_format`.
2. **Voices** — its voice list is hardcoded to OpenAI names. Either set
   `VOICE_ALIASES` here (`alloy=冰糖`, …) or edit the component's list.

Conversation can keep using your existing LiteLLM endpoint; it needs
`/v1/responses`, which this audio-only adapter does not implement.

## Known limitations

- No mp3/opus/aac/flac output (no transcoding by design).
- No `speed` control; the parameter is accepted and ignored.
- No ASR timestamps, `srt`, or `vtt`.
- No `prompt`/`temperature` for ASR.
- Streaming gains nothing through the current HA component (see above).
- Audio capped at `MAX_AUDIO_BYTES` (default 7MB) — MiMo limits the
  base64-encoded payload to 10MB.
- Voice cloning and voice design models are not exposed.

## Verification status

Fully exercised against a mock MiMo: 42 unit tests, plus end-to-end runs with
curl and the official OpenAI Python SDK (`models.list`,
`audio.transcriptions.create`, `audio.speech.create`, and
`with_streaming_response` — the call HA's component uses).

LiteLLM routing was verified against the **live** LiteLLM 1.101.0 instance with a
probe backend: credential injection, path construction, model-alias rewriting,
and `response_format` passthrough all confirmed.

**Not yet verified:** any call against the real `api.xiaomimimo.com` — no
`MIMO_API_KEY` was used. The MiMo request/response shapes follow the official
docs, but real streaming chunk framing and error payloads remain unconfirmed.

## Design decisions

- [ADR 0001](docs/adr/0001-credentials-belong-to-the-caller.md) — the adapter holds no credential
- [ADR 0002](docs/adr/0002-reject-unsupported-audio-formats.md) — reject mp3 rather than downgrade
- [ADR 0003](docs/adr/0003-streaming-tts-boundaries.md) — stream PCM, buffer WAV
