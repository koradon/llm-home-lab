# Generation Parameter Forwarding

## Status

accepted

## Summary

`POST /v1/chat/completions` accepts four optional generation parameters — `temperature`,
`max_tokens`, `chat_template_kwargs`, `response_format` — and passes them through to the selected
backend so clients control generation per request. See
[ADR-0012](../adr/0012-forward-generation-parameters-to-backends.md).

## User stories

- As a client developer, I want my `temperature` and `max_tokens` honored, so that generation
  behaves as I asked instead of using the node's defaults.
- As a client developer, I want to pass `chat_template_kwargs` (for example
  `{"enable_thinking": false}`), so that I can switch Gemma-4 thinking off per call.
- As a client developer, I want `response_format` passed through, so that I can request JSON or
  schema-constrained output.

## Requirements

- Optional request fields: `temperature` (number, 0–2 inclusive), `max_tokens` (integer > 0, no
  upper bound), `chat_template_kwargs` (object), `response_format` (object).
- Invalid values are rejected before dispatch with `400` and the OpenAI-style error envelope
  (`invalid_request_error`).
- The llama-server backend forwards each field unchanged when the client set it.
- The LM Studio backend forwards only `temperature`, `max_tokens` and `response_format`;
  `chat_template_kwargs` is never forwarded to it.
- A field the client did not set (or set to `null`) is omitted from the backend payload, not sent
  as `null`. With none set the payload is identical to the pre-change payload.
- Streaming and non-streaming requests behave identically with respect to these fields.
- Other unrecognized fields remain ignored.

## Behavior

**Set**: `{"temperature": 1.0, "max_tokens": 20000, "chat_template_kwargs": {"enable_thinking":
false}}` reaches a llama-server node with those exact values.

**Unset**: a request without them sends `model`, `messages`, `session_id`, `task_type`, `stream`
and `stream_options` only.

**Invalid**: `max_tokens: 0`, `temperature: 3`, or `chat_template_kwargs: "x"` return `400`.

**Response**: the response already exposes the backend's `finish_reason` (`choices[0].finish_reason`;
the final delta chunk when streaming), so a `max_tokens` cut-off appears as `"length"`.

## Acceptance scenarios (BDD)

`docs/specs/features/20261009-generation-parameter-forwarding.feature`

## Related

- ADR: `docs/adr/0012-forward-generation-parameters-to-backends.md`
- Gateway spec: `docs/specs/20260711-openai-compatible-api-gateway.md`
- Acceptance: `docs/specs/features/20261009-generation-parameter-forwarding.feature`
