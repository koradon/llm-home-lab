# OpenAI-Compatible API Gateway

## Status

draft

## Summary

A single HTTP endpoint, `/v1/chat/completions`, that is wire-compatible with the OpenAI Chat
Completions API. Agents integrate against this one endpoint and never need to know which local
model backend actually serves the request.

## User stories

- As an agent developer, I want to point my existing OpenAI-compatible client at the
  orchestrator, so that I don't have to write a new integration for local models.
- As an agent developer, I want validation errors in a consistent, machine-readable format, so
  that my client can handle them the same way it handles OpenAI API errors.
- As an orchestrator operator, I want incoming requests validated and normalized before
  dispatch, so that backend adapters only ever see well-formed internal requests.

## Requirements

- Expose `POST /v1/chat/completions` accepting the OpenAI Chat Completions request schema
  (`model`, `messages`, and common sampling parameters such as `temperature`, `max_tokens`,
  `stream`).
- Validate the request body against the schema before dispatching to any backend.
- Map a valid request to the orchestrator's internal request representation (decoupled from the
  wire format so backend adapters do not depend on OpenAI's schema directly).
- Return responses in the OpenAI Chat Completions response shape, including `id`, `object`,
  `created`, `model`, `choices`, and `usage`.
- Return errors in OpenAI's error envelope shape (`error.message`, `error.type`, `error.code`)
  for both validation failures and backend failures.
- Support both streaming (`stream: true`, Server-Sent Events chunks) and non-streaming
  responses.

## Behavior

**Valid request, non-streaming**: client posts a well-formed request; gateway validates it,
forwards to the backend selected for this milestone (single configured LM Studio host — see
[LM Studio backend adapter](../plans/20260711-orchestrator-program.md)), and returns a single JSON
response in OpenAI's response shape.

**Valid request, streaming**: same as above, but the gateway relays backend output as
incremental SSE chunks in OpenAI's streaming delta shape, terminated by `data: [DONE]`.

**Invalid request** (missing `messages`, empty `messages`, unknown `model`, malformed JSON):
gateway returns HTTP 400 with the OpenAI-style error envelope; the request is never forwarded to
a backend.

**Backend failure** (timeout, connection error, backend-reported error): gateway maps the
failure to an appropriate HTTP status (e.g. 502/504) and returns it in the same error envelope
shape, per the backend adapter's error classification.

**Edge cases**:

- Empty `messages` array → validation error, not forwarded.
- `stream` omitted → defaults to non-streaming (`false`), matching OpenAI's default.
- Unsupported/unrecognized top-level fields → ignored rather than rejected, to tolerate
  forward-compatible OpenAI clients.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/features/20260711-openai-compatible-api-gateway.feature`.

## Related

- Plan: [orchestrator-program](../plans/20260711-orchestrator-program.md) (M1 — OpenAI-compatible API
  gateway)
- ADR: [0001-python-as-implementation-language](../adr/0001-python-as-implementation-language.md)
- Acceptance: `docs/specs/features/20260711-openai-compatible-api-gateway.feature`

## Open Questions

- Which OpenAI Chat Completions fields are in scope for M1 (e.g. function/tool calling,
  `response_format`) versus deferred to a later milestone?
- Exact HTTP status code mapping for each backend failure classification (depends on the LM
  Studio adapter's error taxonomy, not yet defined).
