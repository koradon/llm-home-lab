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
- Classify failures into the existing `BackendError` hierarchy:
  - request timeout → `BackendTimeoutError`
  - connection failure (host unreachable, connection refused) → a new
    `BackendConnectionError`
  - non-2xx HTTP response from LM Studio → a new `BackendResponseError` carrying the upstream
    status code
- Retry transient failures (timeout, connection failure) up to the configured maximum before
  raising; do not retry on a non-2xx HTTP response, since that reflects a real backend-reported
  error rather than a transport hiccup.
- Log each backend call with backend id/host, latency, and outcome (success / classified
  failure), so requests are traceable per the [program plan](../plans/20260711-orchestrator-program.md)'s
  M1 telemetry goal.

## Behavior

**Successful completion**: adapter POSTs the mapped request to LM Studio's
`/v1/chat/completions`, parses the OpenAI-shaped response, and returns a `BackendResponse` with
content, finish reason, and token usage.

**Successful streaming**: adapter opens a streaming HTTP request to LM Studio, parses each SSE
chunk from LM Studio's response, and yields a `BackendChunk` per chunk, terminating when LM
Studio sends its own `[DONE]` marker.

**Timeout**: if the request (or an individual streaming read) exceeds the configured timeout,
retry up to the configured maximum; if retries are exhausted, raise `BackendTimeoutError`.

**Connection failure**: if the adapter cannot establish or maintain a connection to the
configured host, retry up to the configured maximum; if retries are exhausted, raise
`BackendConnectionError`.

**Non-2xx response**: if LM Studio returns a non-2xx status, raise `BackendResponseError`
immediately (no retry) carrying the status code and response body for diagnostics.

**Edge cases**:

- Retries apply per logical request, not per already-yielded streaming chunk — a mid-stream
  transport failure surfaces as an error rather than silently restarting the stream.
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
- Acceptance: `docs/specs/features/20260711-lmstudio-backend-adapter.feature`

## Open Questions

- Default timeout and max-retry values — start conservative (e.g. 30s timeout, 2 retries) and
  tune once real LM Studio latency is observed.
- Whether retry backoff should be fixed or exponential; fixed is simpler and likely sufficient
  for a single local host.
