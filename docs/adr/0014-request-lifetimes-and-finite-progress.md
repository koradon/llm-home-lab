# Own request lifetimes and bound backend silence

## Status

accepted — supersedes [ADR-0010](0010-unbounded-gap-timeout-by-default-for-lmstudio-and-llamaserver.md)

## Context and Problem Statement

On 2026-11-01 a shared LAN interruption was followed by three Mini nodes retaining all eight
orchestrator slots despite healthy control endpoints and zero reported processing slots. Offline
FastAPI/Uvicorn/HTTPX reproduction confirmed that non-streaming requests survived inbound client
disconnects. Their backend awaits never finished, so their slot-release `finally` never ran.

ADR-0010 correctly identified legitimate prefill exceeding 120 seconds, but assumed health checks
could detect hanging inference exchanges. Health is independent of progress on another socket;
marking a node unhealthy also does not cancel admitted calls. Its HTTPX timeout configuration
unintentionally left write and connection-pool waits unbounded alongside reads. Streaming had a
second ownership gap: closing a body iterator before its first iteration never entered its
slot-release `finally`.

## Considered Options

- Keep unbounded reads and cancel disconnected callers only. Connected callers could still
  exhaust all capacity indefinitely.
- Reinstate a short total request timeout. This would regress legitimate long prefill and long,
  progressing generations described by ADR-0010.
- Own the inbound request lifetime and use independent transport-phase bounds with a generous
  configurable first-byte/byte-gap ceiling. This preserves long generations while bounding
  silent exchanges.
- Reset `in_flight` from `/slots`. Rejected: processing slots omit backend HTTP queues and other
  admitted work. Resetting counters while calls survive causes oversubscription and late decrements.

## Decision Outcome

Use explicit request ownership and independent transport-phase bounds:

- A dedicated ASGI middleware is the sole raw receive owner for chat-completion requests. It
  forwards body messages with backpressure, observes disconnect, then cancels and awaits the
  request task, including its nested backend work. Existing queue `finally` cleanup removes a
  cancelled waiter. Lease-path rewriting runs outside this middleware.
- Non-streaming admission retains its `finally` release. Backend response cleanup completes
  before it runs. Streaming hands the acquired slot to an explicitly closeable iterator and a
  response that closes it even if sending headers fails before iteration. Closing twice is safe.
- Both adapters explicitly close nested iterators and shield HTTP response closure from AnyIO
  cancellation before returning capacity. Neither health results nor `/slots` decrement counters.
- `LMSTUDIO_TIMEOUT` and `LLAMASERVER_TIMEOUT` default to **1800 seconds of read silence**. This
  bounds waiting for the first response bytes and each subsequent byte gap; it is not a total
  inference deadline or semantic token-progress check. Thirty minutes deliberately permits
  multi-minute prefill; operators should tune it to their slowest legitimate workload.
- Explicit empty string, `0`, or `none` (case-insensitive) preserves unbounded reads. Unset now
  means 1800 seconds. Continuous bytes may sustain a response indefinitely by design.
- Independently configurable `*_CONNECT_TIMEOUT`, `*_POOL_TIMEOUT` and `*_WRITE_TIMEOUT` default
  to 10, 10 and 60 seconds, respectively. Health retains its independent 10-second bound. These
  phase bounds remain finite when reads are explicitly unbounded.
- Transport timeouts raise `BackendTimeoutError` immediately without retries. Before response
  headers the gateway returns HTTP 504 with `error.code=backend_timeout`; after streaming headers
  it closes the stream, retaining the existing streaming error contract.

### Consequences

- Disconnected callers no longer retain admission slots or response sockets; connected silent
  exchanges recover capacity within a finite configured gap budget.
- Long responses that keep delivering bytes can exceed 1800 seconds in total. Very large prefill
  exceeding 30 minutes needs a larger configured budget or an explicit unbounded opt-out.
- A healthy control endpoint remains useful for routing, but cannot prove an admitted request is
  making progress. Saturation alone is not proof of a stall.
- TCP keepalive, richer request logging, transport metrics and mismatch watchdogs remain follow-up
  work. This change does not alter production settings or reconcile counters from external load.

## Related

- Issue: [retained request lifetimes](https://github.com/koradon/llm-home-lab/issues/84)
- Spec: [API gateway](../specs/20260711-openai-compatible-api-gateway.md)
- Spec: [LM Studio adapter](../specs/20260711-lmstudio-backend-adapter.md)
- Spec: [llama-server adapter](../specs/20260804-llamaserver-backend-adapter.md)
- Runbook: [host saturation](../runbooks/host-saturation-threshold.md)
- Regression: `tests/test_request_lifetime.py`
