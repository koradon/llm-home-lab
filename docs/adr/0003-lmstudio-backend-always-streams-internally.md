# LM Studio backend always streams internally; raise default timeouts

## Status

accepted

## Context and Problem Statement

`LMSTUDIO_TIMEOUT` (default 30s) is an httpx read timeout. For the non-streaming
`LMStudioBackend.complete()` path, LM Studio returns the whole completion as a single response
body — so the read timeout is effectively a hard cap on *total generation time*, not just on
backend responsiveness. A long-form generation (a long story, a large refactor) that is
legitimately still producing tokens gets killed at the same threshold as a genuinely hung
backend, and (until a companion fix in this same change) was retried from scratch on top of that,
compounding the wait.

For the client-facing streaming path (`request.stream=True`), the same httpx read timeout instead
resets on every received chunk (confirmed against httpx's read-timeout semantics: "the maximum
duration to wait for a chunk of data to be received"), so it behaves as a *max gap between
tokens* rather than a cap on total duration — a slow-but-steadily-progressing generation
effectively never times out. This was discussed with the operator after `LMSTUDIO_TIMEOUT=30`
caused mass `504`/`503` failures under a real long-generation load test (see prior conversation;
also see the `SchedulingQueue.cancel` fix and the read-timeout-no-retry fix in
`backends/lmstudio.py`, both landed just before this decision).

Today, only external clients that themselves pass `"stream": true` get this benefit.
`LMStudioBackend.complete()` (used whenever the external caller does *not* request streaming)
still talks to LM Studio via a single buffered POST, so it does not benefit at all, regardless of
`LMSTUDIO_TIMEOUT`'s value.

## Considered Options

- **A — Just raise `LMSTUDIO_TIMEOUT`.** Simplest, but is still a fixed ceiling on total
  generation time for non-streaming callers; any value chosen will eventually be wrong for some
  prompt/model/hardware combination. Does not fix the underlying mismatch, only moves the pain
  point further out.
- **B — Make `LMStudioBackend.complete()` internally use LM Studio's streaming protocol**
  (accumulate chunks into the same `BackendResponse` it returns today), while leaving the
  orchestrator's own public API contract unchanged (callers who don't pass `"stream": true` still
  get one JSON response, not SSE). This gives every caller — streaming or not — the
  gap-between-tokens timeout semantics, without changing `/v1/chat/completions`'s wire contract.
- **C — Switch the orchestrator's default `ChatCompletionRequest.stream` to `true`.** Rejected:
  breaks OpenAI-API compatibility expectations (the real API defaults to non-streaming) for any
  client that doesn't explicitly set `stream`, which conflicts with this project's core pitch
  ("point your agent at the orchestrator instead of directly at a model server ... no code
  changes").

## Decision Outcome

Chosen option: **B, combined with A** — raise `LMSTUDIO_TIMEOUT` and
`ORCHESTRATOR_DISPATCH_WAIT_TIMEOUT_S` defaults from `30` to `120` seconds as a conservative outer
safety margin, **and** make `LMStudioBackend.complete()` internally stream from LM Studio
(`stream_options.include_usage` requested so the accumulated `BackendResponse` still carries
accurate `prompt_tokens`/`completion_tokens`), retrying only a connection failure that happens
before any chunk has been received — never retrying a mid-stream read timeout, since by then the
backend has already started useful work and resending the prompt from scratch would waste it.

The public `/v1/chat/completions` contract is unchanged: a caller that doesn't pass `stream: true`
still gets one JSON response; a caller that does still gets SSE chunks. Only the *internal*
transport to LM Studio changes.

As a direct consequence of unifying `complete()` and `stream()` on the same underlying chunk
generator, the client-facing streaming path also now carries `usage` data (via
`stream_options.include_usage`) that it previously discarded — closing a gap flagged as a Risk in
the [monitoring-slos-and-alerting](../plans/20260719-monitoring-slos-and-alerting.md) plan
("token usage isn't recorded for streaming responses"). `chat_completions`'s streaming branch now
records token usage when the accumulated stream reports it, the same way the non-streaming branch
always has.

### Consequences

- Good, because non-streaming callers get the same "timeout is a gap, not a ceiling" robustness
  that streaming callers already had — no more guessing a "long enough" total-duration timeout.
- Good, because it closes the previously-flagged streaming token-usage gap as a side effect of the
  same refactor, rather than leaving it as separate follow-up work.
- Good, because the public API contract does not change — no client integration needs updating.
- Bad, because `LMStudioBackend` is now more complex: one internal streaming-chunk generator with
  connect-vs-timeout-aware retry semantics, instead of two independent code paths (a simple
  buffered POST and a separate stream loop).
- Bad, because if LM Studio does not honor `stream_options.include_usage`, `complete()` falls back
  to `prompt_tokens=0, completion_tokens=0` for that response rather than failing — token-usage
  metrics/dashboards would silently under-report for that backend rather than erroring loudly.
  Accepted as consistent with this codebase's existing "missing data degrades a metric, not a
  request" posture (e.g. `failover_success_rate=None` when not applicable); revisit if this proves
  to be the common case rather than the exception.
- Bad, because raising `ORCHESTRATOR_DISPATCH_WAIT_TIMEOUT_S` to 120s means a *queued* request
  (one that never even got a host slot) now also waits up to 2 minutes before failing with `503`,
  not just 30s — a deliberate trade-off given that individual generations can now legitimately run
  much longer and hold their slot for that whole time.
- Neither the concurrency-slot-held-for-the-whole-stream limitation, nor the "headers already sent
  as 200 before any chunk is produced" limitation of `StreamingResponse` (an error mid-stream
  still just drops the connection rather than surfacing a clean error frame) are addressed by this
  decision. Both remain open, tracked in the updated
  [lmstudio-backend-adapter spec](../specs/20260711-lmstudio-backend-adapter.md)'s Open Questions.

- **Revisit trigger**: if `LMSTUDIO_TIMEOUT=120` still proves too short for real workloads (very
  large context windows or very slow hardware), consider making it effectively unbounded for
  same-machine/trusted local backends rather than continuing to raise a fixed number — a local
  LM Studio instance either responds or is dead/hung, which `/health/ready` probing already
  detects independently.
