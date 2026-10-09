# Forward generation parameters to backends

## Status

accepted

## Context

`ChatCompletionRequest` declared only `model`, `messages`, `stream`, `session_id` and `task_type`.
Pydantic dropped every other field, so a client's `temperature` and `max_tokens` never reached
the llama.cpp nodes, which ran with their own defaults (temperature 1.0, no token cap). Clients
could not switch Gemma-4 thinking off per call, cap a runaway generation, or ask for structured
output. A live audit of facts-service's generate pipeline traced slow, decode-bound requests to
exactly this.

## Decision

We will add four optional fields to `ChatCompletionRequest` — `temperature`, `max_tokens`,
`chat_template_kwargs`, `response_format` — and forward each unchanged in the backend payload when
(and only when) the client set it. An unset field is excluded from the payload rather than sent as
`null`, so a request that sets none of them produces the same payload as before this change
(including the existing `session_id: null` / `task_type: null` keys).

- **Validation**: `max_tokens` must be an integer > 0; `temperature` a number in `[0, 2]` (the
  OpenAI range, wide enough for everything facts-service sends: 0.2 today, 1.0 after its paired change); the two
  object fields must be JSON objects. Violations are rejected before dispatch with the gateway's
  usual `400 invalid_request_error`. There is deliberately no upper bound on `max_tokens`.
- **llama-server backend**: forwards all four.
- **LM Studio backend**: forwards only the standard OpenAI fields (`temperature`, `max_tokens`,
  `response_format`). `chat_template_kwargs` is a llama.cpp extension and is never forwarded:
  LM Studio's acceptance of it was not verified, and a silently ignored or rejected field would be
  worse than an explicit omission.
- **No other request fields** (`top_p`, `stop`, `seed`, …) are forwarded; they remain dropped.

## Consequences

Clients control sampling, the token cap, the Gemma-4 thinking switch and structured output per
request. Clients that send nothing see no change.

**Deployment ordering (important).** facts-service already sends `temperature: 0.2` and
`max_tokens: 8000` on every request today. Those values have been silently ignored; the moment an
orchestrator running this change is (re)started they take effect in production — lower
temperature and a hard 8000-token ceiling. The paired facts-service change (temperature 1.0 and a
20000-token cap) must therefore be deployed first, or both services restarted together. Merging
this change does not alter a running orchestrator; only its restart does.

**`finish_reason` finding (no change made).** The orchestrator's HTTP response already exposes the
backend's `finish_reason`: non-streaming responses carry it in `choices[0].finish_reason`, and
streaming responses carry it on the final delta chunk. A generation cut off by `max_tokens`
therefore surfaces to the client as `finish_reason: "length"`. Note that the orchestrator treats
any `finish_reason` other than `"stop"` as a degenerate completion for health and throughput
accounting, so a capped generation counts as an unhealthy completion for that host.
