# 2. Reject unsupported audio formats instead of downgrading

Date: 2026-09-19

## Status

Superseded by the transcoding decision recorded below (2026-09-19)

## Context

OpenAI's speech API offers `mp3`, `opus`, `aac`, `flac`, `wav`, and `pcm`, and
defaults to `mp3`. MiMo v2.5 emits only `wav` and `pcm16`.

The tempting fix is to accept `mp3` and return WAV with an honest
`Content-Type: audio/wav`, on the reasoning that a correct header is not a lie.

Reading the intended client disproves that. `ha-openai-compatible` accumulates
the response body and returns `(response_format, bytes)` — where
`response_format` is **the format it asked for**. It never inspects
`Content-Type`. So WAV bytes requested as mp3 are handed to Home Assistant
labeled mp3, and HA may then skip a conversion it would otherwise perform,
because expectation and report agree while the bytes disagree.

Transcoding in the adapter was rejected separately: it would mean bundling
ffmpeg into a service whose entire purpose is protocol translation.

## Decision

Support `wav` (→ MiMo `wav`) and `pcm` (→ MiMo `pcm16`, returned headerless as
`application/octet-stream`).

Reject `mp3`, `opus`, `aac`, and `flac` with HTTP 400, naming the parameter and
the supported values. Use 400 rather than 415: the problem is an unsupported
*parameter value*, not an unsupported request media type.

Callers needing mp3 should request `wav` and convert. Home Assistant already
has that path via `preferred_format`, which uses its own ffmpeg.

## Consequences

A caller configured for mp3 gets a clear, immediate error instead of silently
mislabeled audio. This surfaces the mismatch at configuration time, which is
where it can be fixed.

The `ha-openai-compatible` component must be configured to request `wav`; its
default is `mp3`. This is documented in the README.

The adapter needs no audio libraries. WAV framing uses `struct` from the
standard library.


## Update: superseded

Refusing mp3 broke Home Assistant outright — its Assist pipeline never forwards
`preferred_format`, so every spoken response failed with 400. The refusal was
then relaxed to return wav instead, which fixed the 400 and broke playback on
iOS instead.

The reason is the mislabeling this ADR predicted, arriving by a different
route. `tts/__init__.py` builds the stream token, file extension and
`Content-Type` from `options[preferred_format]` — the *requested* name — and
decides whether to transcode with `final_extension != extension`. With wav
bytes labeled mp3, that comparison is `"mp3" != "mp3"`, so HA skipped its own
ffmpeg pass and served RIFF data as `audio/mpeg`.

`preferred_format` cannot be set for this path: only `assist_satellite` passes
`tts_audio_output`, and `websocket_api` — which the iOS app uses — never does.

So the caller's requested format has to be honoured for real. See
[ADR 0004](0004-transcode-formats-mimo-cannot-emit.md).
