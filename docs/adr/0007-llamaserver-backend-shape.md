# Prefer a new backend's native capabilities over replicating another backend's workarounds

## Status

accepted

## Context and Problem Statement

`LMStudioBackend` was the only `ChatBackend` implementation until now. Adding a second one,
`LlamaCPPServerBackend` (for llama.cpp's `llama-server`), raised a recurring question for each
piece of non-chat-completion behavior: mirror the shape LM Studio's adapter already uses, or use
the new backend's own capabilities where they differ? Several of `LMStudioBackend`'s design
choices are workarounds for LM Studio-specific gaps, not general truths about "how a backend
should behave":

- `check_health()` proxies through `GET /v1/models` because LM Studio has no dedicated health
  endpoint.
- `ExternalLoadProbe` shells out to the `lms ps` CLI (ADR-0005) because LM Studio's REST API
  exposes no load/queue endpoint.
- `model_aliases` round-robins across model identifiers inside one `LMStudioBackend` instance
  because LM Studio can hold several loaded instances of one model in a single process.

llama-server does not share these constraints: it ships a native `GET /health`, a native
`GET /slots` load endpoint, and runs exactly one model per process/port. Copying LM Studio's
adapter shape verbatim would mean re-implementing workarounds for problems llama-server doesn't
have, and a `MultiBackendLoadProbe` dispatcher was needed regardless once a second load-visibility
strategy existed at all — `create_app`'s `external_load_probe` hook was designed for exactly one
prober shared by every host.

## Considered Options

- **A — Mirror `LMStudioBackend`'s shape as closely as possible** for every piece of behavior,
  including the CLI-based load probe and in-backend model aliasing, for consistency between
  backends.
- **B — Use each backend's own native capabilities where they're better-suited**, and only share
  code/shape where the underlying wire contract is genuinely the same (chat completions,
  failure classification, retry policy — all OpenAI-compatible SSE, ported near-verbatim).
- **C — Build a shared abstract base class** for anything "backend-like" up front, forcing both
  adapters into one common shape regardless of which capabilities each backend actually has.

## Decision Outcome

Chosen option: **B**, applied to three sub-decisions:

1. **Health check** — `LlamaCPPServerBackend.check_health()` calls llama-server's native
   `GET /health` directly, rather than proxying through a chat/model endpoint.
2. **Load/queue visibility** — a new `LlamaCPPServerLoadProbe` reads `GET {base_url}/slots` over
   plain HTTP (no subprocess), rather than mirroring `ExternalLoadProbe`'s CLI-shelling approach.
   Wiring two backend-type-specific probers into one app required a new
   `MultiBackendLoadProbe` dispatcher (`registry/multi_backend_load_probe.py`), which looks up
   each host's `backend_type` via `HostRegistry` and routes to the right prober — defaulting to
   `available: false` (never an error) for any `backend_type` with none registered. This kept
   `create_app`'s existing `external_load_probe` parameter and its ~15 existing test call sites
   unchanged; only the type hint was relaxed from the concrete `ExternalLoadProbe` class to a new
   `LoadProbe` Protocol (`registry/external_load.py`).
3. **Multi-instance handling** — no `model_aliases` equivalent. llama-server is
   one-model-per-process/port, so "multiple instances of one model" is just multiple llama-server
   processes, each registered as its own node; `RoutingEngine` already load-balances across
   separate `backend_id`s with no new code required.

The general precedent: **when adding a new backend, prefer its own native, well-supported
capabilities over replicating a workaround that exists specifically to compensate for a
*different* backend's limitations.** The parts of `LMStudioBackend` that genuinely reflect the
shared wire contract (OpenAI-compatible chat completions, SSE parsing, failure classification,
retry policy, `stream_options.include_usage`) are exactly the parts that ported over
near-verbatim — confirmed against llama.cpp's own server documentation
(`tools/server/README.md`) that `/health`, `/v1/models`, `/slots`, and
`stream_options.include_usage` all behave as assumed, and that `/slots` is on by default but can
be disabled by an operator flag (handled by treating a `404` as `available: false`, never an
error).

### Consequences

- Good, because each backend's adapter stays as simple as its own backend allows — no code exists
  to work around a limitation that backend doesn't have.
- Good, because it avoids over-abstracting after only two data points (option C): a shared base
  class built now would be guessing at what's actually common across backends before a third
  implementation exists to confirm it.
- Good, because per-host load-visibility dispatch is now a reusable pattern
  (`MultiBackendLoadProbe`) for a third backend, rather than something to invent again from
  scratch.
- Bad, because there is no shared base class enforcing consistency — a future backend author must
  read this ADR (and the two adapters) to know which parts are "shared contract" (port near
  verbatim) versus "LM Studio-specific workaround" (do not copy) versus "possibly worth factoring
  out" (revisit once a third backend exists).
- Neutral: `MultiBackendLoadProbe` only handles per-host dispatch by `backend_type`; it does not
  change `_eligible_candidates`/routing — external load remains a read-only, informational signal
  for every backend, consistent with ADR-0005.

- **Revisit trigger**: once a third `ChatBackend` implementation exists, re-examine whether enough
  genuinely shared logic has accumulated (beyond the OpenAI-compatible completion path) to justify
  extracting a shared base or protocol for health/load-visibility/multi-instance handling, instead
  of continuing to decide per-backend as in this ADR.

## Related

- Spec: [llamaserver-backend-adapter](../specs/20260804-llamaserver-backend-adapter.md)
- Spec: [lmstudio-backend-adapter](../specs/20260711-lmstudio-backend-adapter.md)
- ADR: [0003-lmstudio-backend-always-streams-internally](0003-lmstudio-backend-always-streams-internally.md) — the part of `LMStudioBackend` that *did* port over verbatim, because it reflects a shared wire-protocol/httpx-timeout property, not an LM Studio-specific workaround
- ADR: [0005-lms-cli-for-external-node-load-visibility](0005-lms-cli-for-external-node-load-visibility.md) — the workaround this ADR explicitly declines to replicate for llama-server
