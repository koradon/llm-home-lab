# llama-server Backend Adapter

## Status

accepted

## Summary

A `ChatBackend` implementation that dispatches chat completion requests to a configured
llama.cpp `llama-server` host over HTTP. It fulfills the same `ChatBackend` port as
[LMStudioBackend](20260711-lmstudio-backend-adapter.md), so the gateway can route requests to a
llama-server node exactly like any other registered host — same routing, health checks, capacity
limits, and auth, with zero changes upstream of backend selection
(`BACKEND_FACTORIES[backend_type]`).

## User stories

- As an orchestrator operator, I want to point the gateway at my `llama-server` instance via
  configuration, so that agents get real completions from a llama.cpp-hosted model.
- As an orchestrator operator, I want transport failures (timeouts, connection errors)
  classified and retried a bounded number of times, so that transient network hiccups don't
  fail every request.
- As an orchestrator operator, I want to see whether a llama-server node is currently busy
  generating, so that I have the same load visibility LM Studio nodes already get.

## Requirements

- Implement the `ChatBackend` protocol (`complete`, `stream`, `check_health`, optional
  `list_models`) from `llm_home_lab.backends.base`, backed by an HTTP client calling a configured
  llama-server host's OpenAI-compatible `/v1/chat/completions` endpoint.
- Configuration: base URL and a request timeout are required inputs; a maximum retry count has
  a sane default. Mirrors `LMSTUDIO_*` env var naming: `LLAMASERVER_TIMEOUT` (default `120`),
  `LLAMASERVER_MAX_RETRIES` (default `2`), `LLAMASERVER_CONNECT_TIMEOUT` (default `10`).
  llama-server's standard default port is `8080` (vs. LM Studio's `1234`), though `base_url` is
  always explicit at node registration time.
- **`complete()` talks to llama-server via its streaming protocol internally**, accumulating
  chunks into the same `BackendResponse` shape it has always returned — the caller of
  `complete()` still gets one accumulated result, not a stream. This mirrors
  [ADR-0003](../adr/0003-lmstudio-backend-always-streams-internally.md)'s rationale for
  `LMStudioBackend`: httpx's read timeout resets on every received chunk, so a streaming
  transport turns the timeout into "max gap between tokens" instead of "max total generation
  time." Requests `stream_options: {"include_usage": true}`; usage falls back to `0`/`0` if the
  backend doesn't honor it, same as `LMStudioBackend`.
- **Health check uses llama-server's native `GET /health`**, not a proxy through a chat/model
  endpoint — unlike LM Studio, which has no dedicated liveness endpoint and so is checked via
  `GET /v1/models` instead.
- **Model listing has no on-demand-loading guard to build.** `GET /v1/models` always reports
  llama-server's one loaded model, because a llama-server process serves exactly one model for
  its entire lifetime — there is no LM-Studio-style JIT loading of additional models to guard
  against.
- **No `model_aliases` / in-backend round-robin.** LM Studio can hold several loaded instances of
  one model in a single process, so `LMStudioBackend` round-robins across them internally.
  llama-server is one-model-per-process/port, so the equivalent of "multiple instances of one
  model" is multiple separate llama-server processes, each registered as its own node —
  `RoutingEngine` already load-balances across separate `backend_id`s. `LlamaCPPServerBackend`
  simply forwards `request.model` unchanged.
- Classify failures into the existing `BackendError` hierarchy, identically to `LMStudioBackend`:
  - request timeout → `BackendTimeoutError`
  - connection failure (host unreachable, connection refused) → `BackendConnectionError`
  - non-2xx HTTP response from llama-server → `BackendResponseError` carrying the upstream
    status code
- Retry a connection failure that occurs before any chunk has been received, up to the
  configured maximum, before raising `BackendConnectionError`. Do not retry a read timeout or a
  non-2xx response, for the same reasons as `LMStudioBackend` (see its spec).
- Log each backend call with backend id/host, latency, and outcome (success / classified
  failure).

## Behavior

**Successful completion / streaming / timeout / connection failure / non-2xx response**:
identical contract to [LMStudioBackend](20260711-lmstudio-backend-adapter.md#behavior) — the
wire format (OpenAI-compatible SSE) and failure classification are the same, since both backends
implement the same `ChatBackend` port against an OpenAI-compatible chat endpoint.

**Health check**: `GET /health` returning a 2xx status is healthy; anything else (including a
`503` while the model is still loading, or a transport error) is unhealthy.

**Model listing**: `GET /v1/models` returns the single loaded model's id; a non-2xx response or
transport error returns `None` (same "can't verify, don't block routing on it" contract as
`LMStudioBackend.list_models()`).

**Edge cases**:

- Same as `LMStudioBackend`: retries apply only to the connection attempt before any data has
  been received; a mid-stream transport failure always surfaces as an error rather than
  silently restarting the stream. The adapter does not interpret or transform message content.

## Load visibility (`LlamaCPPServerLoadProbe`)

A companion component, `registry/llamaserver_load.py`, gives llama-server nodes the same
external-load signal LM Studio nodes get from
[`ExternalLoadProbe`](20260720-external-node-load-visibility.md) — but over HTTP instead of a
CLI subprocess, since llama-server exposes this natively:

- Reads `GET {base_url}/slots`, a per-slot status list llama-server ships enabled by default.
- Each slot reports `is_processing: bool`. Status is `"busy"` if any slot is processing, else
  `"idle"`.
- llama-server exposes no true queue-depth metric (unlike LM Studio's `queued` count from
  `lms ps`), so `queued` is **approximated** as the count of currently-processing slots — a
  proxy for "how backed up is this host," not a precise pending-request backlog.
- A `404` (an operator disabled `/slots` via a flag), any other non-2xx response, an unreachable
  host, or unparseable output all report `available: false` — the same "informational,
  never an error" degrade contract as `ExternalLoadProbe`. Verified against llama.cpp's own
  server documentation (`tools/server/README.md`) that `/slots` is on by default but can be
  turned off.
- Cached per host for a short TTL (default 2s, same default as `ExternalLoadProbe`), to avoid a
  fresh HTTP call on every `/health/ready` invocation.

### Wiring: `MultiBackendLoadProbe`

`create_app`'s `external_load_probe` hook was designed for exactly one prober shared by every
registered host, regardless of `backend_type` — it only ever calls
`probe(host_id, base_url, at)`. Introducing a second, backend-type-specific prober required a
small dispatcher, `registry/multi_backend_load_probe.py::MultiBackendLoadProbe`:

- Looks up each host's `backend_type` via the `HostRegistry` (not passed through the call site,
  to avoid changing `external_load_probe`'s signature and touching its ~15 existing test call
  sites).
- Routes to `ExternalLoadProbe` for `"lmstudio"` hosts, `LlamaCPPServerLoadProbe` for
  `"llamaserver"` hosts.
- Any host whose `backend_type` has no registered prober (or that gets deregistered mid-probe)
  reports `available: false` — never raises.
- `create_app`'s `external_load_probe` parameter type was relaxed from the concrete
  `ExternalLoadProbe` class to a new `LoadProbe` Protocol (`registry/external_load.py`) so this
  substitution type-checks; its runtime behavior for existing lmstudio-only callers is
  unchanged.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file:
`docs/specs/features/20260804-llamaserver-backend-adapter.feature`.

## Related

- Spec: [lmstudio-backend-adapter](20260711-lmstudio-backend-adapter.md) — the sibling adapter
  this one mirrors almost line-for-line for the OpenAI-compatible parts of the contract
- Spec: [openai-compatible-api-gateway](20260711-openai-compatible-api-gateway.md) — defines the
  `ChatBackend` port this adapter implements
- Spec: [external-node-load-visibility](20260720-external-node-load-visibility.md) — the LM
  Studio-specific prober this backend's load probe parallels over HTTP instead of a CLI
  subprocess
- ADR: [0003-lmstudio-backend-always-streams-internally](../adr/0003-lmstudio-backend-always-streams-internally.md)
- ADR: [0005-lms-cli-for-external-node-load-visibility](../adr/0005-lms-cli-for-external-node-load-visibility.md)
- ADR: [0007-llamaserver-backend-shape](../adr/0007-llamaserver-backend-shape.md) — the
  native-capabilities-over-workarounds precedent behind the health/load-visibility/aliasing
  decisions above
- Acceptance: `docs/specs/features/20260804-llamaserver-backend-adapter.feature`

## Open Questions

- No boot-time auto-registration: unlike LM Studio, `create_default_app()` does not
  auto-register a default llama-server host at startup — nodes register only via
  `POST /v1/nodes/register`. Left as-is deliberately; `create_default_app()`'s
  single-hardcoded-host approach will likely need a redesign once it has to default-register
  more than one backend type, and this is a known gap to revisit then, not now.
- Whether `/slots`' approximated `queued` (busy-slot count rather than a real backlog) is precise
  enough to be useful in the TUI, or whether it should instead be labeled more conservatively
  (e.g. `busy_slots`/`total_slots`) once there's real operator feedback on it.
- Same open questions as `LMStudioBackend`'s spec around retry backoff shape and concurrency-slot
  duration for long generations — unchanged by this addition.
