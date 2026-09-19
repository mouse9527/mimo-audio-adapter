# 4. Transcode formats MiMo cannot emit

Date: 2026-09-19

## Status

Accepted. Supersedes [ADR 0002](0002-reject-unsupported-audio-formats.md).

## Context

MiMo emits only wav and pcm16. Home Assistant's built-in OpenAI TTS requests
mp3, and three separate attempts to avoid converting it all failed:

1. **Refuse mp3.** Every Assist response failed with 400. The pipeline never
   forwards `preferred_format`, so nothing downstream could ask for wav.
2. **Return wav labeled audio/wav.** Fixed the 400; playback then failed on
   iOS. HA derives the token extension and `Content-Type` from the format it
   *requested*, and gates transcoding on `final_extension != extension` — which
   compares `"mp3"` against `"mp3"` and skips ffmpeg. RIFF bytes were served as
   `audio/mpeg`.
3. **Make HA request wav.** Not reachable: only `assist_satellite` passes
   `tts_audio_output` (ESPHome-class hardware), and the `websocket_api` path
   the iOS app uses never sets it.

Since the requested format is the only thing that reaches the player, it has to
be true.

## Decision

Convert wav to mp3, opus, aac, or flac with ffmpeg before responding, and
label each with its real media type. `wav` and `pcm` are returned untouched and
never invoke ffmpeg.

mp3 uses a constant bitrate: some hardware decoders reject variable bitrate and
play nothing, a failure indistinguishable from a broken response.

Transcoded formats are served non-streaming, because the container cannot be
framed before the whole signal is known.

## Consequences

The adapter is no longer purely a protocol translator, and the image carries
ffmpeg (roughly +100MB). That cost buys the only configuration that works from
the iPhone through to audible output.

The default path is unaffected: wav and pcm still pass through untouched, so
the common case pays nothing.

Conversion is per-request and bounded by `TRANSCODE_TIMEOUT`. `/ready` reports
whether ffmpeg is present, so a broken image is visible without waiting for a
request to fail.
