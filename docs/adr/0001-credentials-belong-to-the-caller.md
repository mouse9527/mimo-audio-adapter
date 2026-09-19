# 1. Credentials belong to the caller

Date: 2026-09-19

## Status

Accepted

## Context

The adapter sits between LiteLLM and MiMo. LiteLLM already stores provider
credentials in an encrypted table and injects them as an `Authorization` header
when routing to a model's `api_base` — verified against the live LiteLLM 1.101.0
instance with a probe backend.

The obvious design gives the adapter its own `MIMO_API_KEY`, and an early draft
did exactly that, with a `MIMO_KEY_MODE` switch between forwarding the inbound
header and using a configured key, plus an `ADAPTER_API_KEY` gate.

That draft had a real flaw: with a gate key configured *and* passthrough active,
the inbound header is simultaneously "the credential to check" and "the
credential to forward". The two meanings conflict, and the code needed an
explicit error for the ambiguity — a sign the model was wrong.

## Decision

The adapter holds no credential. `MIMO_BASE_URL` is configuration — an endpoint,
not a secret. The caller supplies the MiMo credential, and the adapter relays
the `Authorization` header upstream verbatim.

No `MIMO_API_KEY`, no `MIMO_KEY_MODE`, no `ADAPTER_API_KEY`, no Kubernetes
Secret, and no fallback credential. A request without `Authorization` is
refused with 401.

A unit test asserts that no field of the settings object has a name containing
`key`, `token`, or `secret`, so reintroducing one fails the build.

## Consequences

The secret exists in exactly one place, LiteLLM's credential store. Rotating it
there is the entire operation: no image rebuild, no Secret update, no rollout.

The adapter cannot be used standalone without a caller that supplies a
credential. That is acceptable — it is a protocol translator, not a gateway.

Because the pod stores nothing sensitive, CI needs no secrets to build or
publish it, and the container image is safe to publish publicly.

Upstream `401`/`403` map to `502`, not `401`: MiMo rejected the caller's
credential, which is an upstream condition rather than a failure to
authenticate against this adapter. Reflecting `401` would wrongly tell the
caller to re-authenticate here.
