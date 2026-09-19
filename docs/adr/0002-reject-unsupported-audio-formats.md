# 2. Reject unsupported audio formats instead of downgrading

Date: 2026-09-19

## Status

Accepted

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
