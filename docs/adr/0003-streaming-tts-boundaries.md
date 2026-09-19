# 3. Stream PCM, buffer WAV

Date: 2026-09-19

## Status

Accepted

## Context

The goal was reducing time-to-first-sound for Home Assistant Assist. MiMo
supports `stream=True`, delivering base64 audio deltas over SSE at
`choices[0].delta.audio.data`, and recommends `pcm16` for streaming so chunks
concatenate cleanly.

Two problems surfaced.

First, WAV needs a 44-byte RIFF header whose length fields describe the payload
— unknowable before generation completes. A placeholder such as `0xFFFFFFFF` is
sometimes tolerated, but tolerance across iOS Safari, the HA companion app, and
arbitrary `media_player` targets is unverified. Betting playability on it was
not justifiable.

Second, and decisively: streaming yields no end-to-end benefit through the
intended client. Home Assistant core *does* stream — `TTSCache.async_stream_data`
hands chunks to consumers as they arrive, and `/api/tts_proxy` writes the
response incrementally. But `ha-openai-compatible` collects every chunk into a
`bytearray` and returns only once complete. The bottleneck is the component,
not the adapter and not HA.

Labeling raw PCM as `audio/L16` was also considered and rejected: RFC 2586
specifies network (big-endian) byte order, while MiMo emits little-endian.

## Decision

`stream: true` with `response_format: pcm` streams decoded PCM through as it
arrives, typed `application/octet-stream`, with sample rate and channel count
in response headers. The contract (24 kHz, 16-bit little-endian, mono,
headerless) is documented rather than encoded in a media type that would
misdescribe it.

`stream: true` with `wav` requests `pcm16` upstream, collects it, and frames it
with a truthful RIFF length. Correctness is preferred over a placeholder whose
playability is unverified.

SSE decoding follows three distinct boundaries:

- **Transport reads** carry no application meaning and may split mid-base64, so
  an incremental parser buffers until a complete event delimiter arrives.
- **Events** each carry independently-encoded base64, decoded separately;
  concatenating the base64 *text* across events would corrupt it via padding.
- **PCM samples** are not guaranteed to align to 2-byte boundaries per chunk,
  so alignment is a consumer-side concern, not assumed here.

Undecodable base64 raises an error. It is never padded or spliced to "repair"
it, which would mask an upstream fault.

## Consequences

The low-latency path exists and is correct for clients that can consume raw PCM.

Reducing HA's time-to-first-sound requires changing `ha-openai-compatible` to
forward chunks, not changing this adapter. The README states this so the
optimization is not attempted in the wrong place.

If an error occurs after the response body has begun, the stream can only be
terminated — error JSON must never be mixed into audio bytes.
