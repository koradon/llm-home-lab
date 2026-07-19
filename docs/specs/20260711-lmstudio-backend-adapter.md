# LM Studio Backend Adapter

## Status

accepted

## Summary

A `ChatBackend` implementation that dispatches chat completion requests to a configured LM
Studio host over HTTP. It fulfills the `ChatBackend` port defined for the
[OpenAI-compatible API gateway](20260711-openai-compatible-api-gateway.md), so the gateway can serve
real requests instead of a test fake.

## User stories

- As an orchestrator operator, I want to point the gateway at my LM Studio instance via
  configuration, so that agents get real completions from my local model.
- As an orchestrator operator, I want transport failures (timeouts, connection errors)
  classified and retried a bounded number of times, so that transient network hiccups don't
  fail every request.
- As an orchestrator operator, I want backend failures logged with enough detail to diagnose
  them, so that I can tell a misbehaving backend from a misconfigured one.

## Requirements

- Implement the `ChatBackend` protocol (`complete`, `stream`) from
  `orchestrator.backends.base`, backed by an HTTP client calling a configured LM Studio host's
  own OpenAI-compatible `/v1/chat/completions` endpoint.
- Configuration: base URL and a request timeout are required inputs; a maximum retry count has
  a sane default.
- **`complete()` talks to LM Studio via its streaming protocol internally**, accumulating chunks
  into the same `BackendResponse` shape it has always returned — the caller of `complete()` still
  gets one accumulated result, not a stream. This is deliberate (see
  [ADR-0003](../adr/0003-lmstudio-backend-always-streams-internally.md)): httpx's read timeout
  resets on every received chunk, so a streaming transport turns the timeout into "max gap
  between tokens" instead of "max total generation time" — for both `complete()` and `stream()`.
  Requests `stream_options: {"include_usage": true}` so the accumulated response still carries
  accurate `prompt_tokens`/`completion_tokens`; if the backend doesn't honor that option, usage
  falls back to `0`/`0` for that response rather than failing the request.
- Classify failures into the existing `BackendError` hierarchy:
  - request timeout → `BackendTimeoutError`
  - connection failure (host unreachable, connection refused) → a new
    `BackendConnectionError`
  - non-2xx HTTP response from LM Studio → a new `BackendResponseError` carrying the upstream
    status code
- Retry a connection failure that occurs before any chunk has been received, up to the configured
  maximum, before raising `BackendConnectionError`. **Do not retry a read timeout, ever** — once a
  request has reached the backend, a timeout means "still generating," not "transient failure";
  resending the same prompt from scratch only compounds the wait instead of helping. Do not retry
  a non-2xx HTTP response either, since that reflects a real backend-reported error.
- Log each backend call with backend id/host, latency, and outcome (success / classified
  failure), so requests are traceable per the [program plan](../plans/20260711-orchestrator-program.md)'s
  M1 telemetry goal.

## Behavior

**Successful completion**: adapter opens a streaming request to LM Studio's
`/v1/chat/completions`, accumulates the SSE chunks into content/finish-reason/usage, and returns
a `BackendResponse`.

**Successful streaming**: adapter opens a streaming HTTP request to LM Studio, parses each SSE
chunk from LM Studio's response, and yields a `BackendChunk` per chunk (including a `usage` field
when the backend supplies one), terminating when LM Studio sends its own `[DONE]` marker.

**Timeout**: if an individual streaming read exceeds the configured timeout, raise
`BackendTimeoutError` immediately — no retry, regardless of whether any chunks were already
received.

**Connection failure before any chunk arrives**: retry up to the configured maximum; if retries
are exhausted, raise `BackendConnectionError`.

**Connection failure after at least one chunk arrived**: raise `BackendConnectionError`
immediately — some output was already in flight (for `stream()`, already sent to the external
caller), so silently restarting from scratch would either duplicate or misrepresent output.

**Non-2xx response**: if LM Studio returns a non-2xx status, raise `BackendResponseError`
immediately (no retry) carrying the status code and response body for diagnostics.

**Edge cases**:

- Retries apply only to the connection attempt before any data has been received — a mid-stream
  transport failure (of any kind) always surfaces as an error rather than silently restarting the
  stream.
- The adapter does not interpret or transform message content; it only maps between the
  orchestrator's internal request/response shapes and LM Studio's wire format.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/features/20260711-lmstudio-backend-adapter.feature`.

## Related

- Plan: [orchestrator-program](../plans/20260711-orchestrator-program.md) (M1 — LM Studio backend
  adapter)
- Spec: [openai-compatible-api-gateway](20260711-openai-compatible-api-gateway.md) (defines the
  `ChatBackend` port this adapter implements)
- ADR: [0001-python-as-implementation-language](../adr/0001-python-as-implementation-language.md)
- ADR: [0003-lmstudio-backend-always-streams-internally](../adr/0003-lmstudio-backend-always-streams-internally.md)
- Spec: [monitoring-slos-and-alerting](20260719-monitoring-slos-and-alerting.md) — token usage
  recording, now also populated for streaming responses as a consequence of ADR-0003
- Acceptance: `docs/specs/features/20260711-lmstudio-backend-adapter.feature`

## Open Questions

- Default timeout and max-retry values — raised to 120s / 2 retries per ADR-0003 after real-world
  long-generation load exposed the original 30s default as too short; revisit again if 120s still
  proves insufficient (see ADR-0003's revisit trigger).
- Whether retry backoff should be fixed or exponential; fixed is simpler and likely sufficient
  for a single local host.
- The concurrency slot is held for a request's entire streaming duration — a very long generation
  still occupies capacity other requests are queued behind; not addressed here (see ADR-0003's
  Consequences).
- Once `StreamingResponse` has sent its `200` status (before any chunk is produced — confirmed
  against Starlette's `stream_response`), a later error can only surface to the external client as
  a dropped connection, not a clean error payload. Not addressed here; a future change could emit
  an in-band SSE `error` event before closing the stream.
