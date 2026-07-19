# Policy-Based Routing Engine Plan

## Status

completed

## Related

- Spec: [policy-routing-engine](../specs/20260717-policy-routing-engine.md)
- Depends on: [session-manager-core](../specs/20260717-session-manager-core.md) (`session_id` semantics
  sticky routing keys off)
- Depends on: [`backends/base.py`](../../src/llm_home_lab/backends/base.py) (the `ChatBackend`
  protocol routing selects among)
- First M3 module — no prior routing/scoring code exists to mirror.
- Issue: #6 (Implement policy-based routing engine, M3)

## Scope

Build a `RoutingEngine` that scores registered `ChatBackend`s per request against a declared
`RoutingPolicy` (task type, token budget, latency), wire it into `create_app` in place of today's
single fixed backend, and add an optional sticky-session mode keyed by a new `session_id` field
on `ChatCompletionRequest`.

Out of scope for this plan (per the spec's Open Questions):

- Real health/latency probing — issue #8 owns the health-score model; this engine takes a
  latency estimate as a caller-supplied input per candidate, not something it measures itself.
- Hot-reloadable routing policy configuration — the initial `RoutingPolicy` is assembled in code
  at app startup.
- Persisting sticky-session assignments across process restarts — sticky state is in-memory,
  process-lifetime routing state, not orchestrator history like sessions/workspace/tool state;
  it does not belong in `SessionStore`.
- Configuring more than one real backend host in `main.py` — `LMSTUDIO_BASE_URL` still names one
  host today; multi-host registration is M4's concern (multi-node registry). This plan wires
  `main.py`'s single configured backend through as a one-candidate routing setup, preserving
  current behavior, so the routing path is exercised end-to-end without inventing host config
  that isn't needed yet.

## Steps

1. **Routing package** (`src/llm_home_lab/routing/`, new package sibling to `backends/` and
   `state/`) — `__init__.py` exporting the public surface.
2. **Routing models** (`src/llm_home_lab/routing/models.py`) — `PolicyRule` (a predicate over
   task type/token budget/latency plus a score contribution), `RoutingPolicy` (ordered
   `PolicyRule`s), `RoutingDecision` (chosen `backend_id`, per-candidate scores, matched rules),
   `NoAvailableBackendError`.
3. **`RoutingEngine`** (`src/llm_home_lab/routing/engine.py`) — `select_backend(request,
   candidates, session_id=None)`: excludes candidates whose context window is smaller than the
   request's estimated token budget, scores the remainder against the policy, breaks ties by
   `backend_id`, and raises `NoAvailableBackendError` if nothing remains. Sticky-session state
   (`session_id -> backend_id`) lives as a plain in-memory dict on the engine instance, guarded
   by the `sticky_sessions_enabled` constructor flag (default `True`); reads/writes to it happen
   only within `select_backend`, so the pure-scoring path stays trivially testable without it.
4. **Request model** (`src/llm_home_lab/api/models.py`) — add `session_id: str | None = None`
   and `task_type: str | None = None` to `ChatCompletionRequest`.
5. **App wiring** (`src/llm_home_lab/api/app.py`) — `create_app` takes `candidates:
   Sequence[RoutingCandidate]` and `router: RoutingEngine` instead of a single `backend:
   ChatBackend` (`RoutingCandidate` wraps a `ChatBackend` with the `latency_ms`/`context_window`
   metadata the engine needs — routing candidates, not bare backends, since that metadata has
   nowhere else to live until #8's health model exists); `chat_completions` resolves the backend
   via `router.select_backend(request, candidates, session_id=request.session_id)` before
   proceeding exactly as today. `health/ready` reports every candidate, not just one.
6. **Entry point** (`src/llm_home_lab/main.py`) — `create_default_app` wraps the existing
   `LMStudioBackend` in a single `RoutingCandidate` (context window from a new
   `LMSTUDIO_CONTEXT_WINDOW` env var, default `8192`), constructs a default `RoutingPolicy` (a
   single rule that scores by latency, since there is only one real candidate today), and passes
   both to `create_app`. No behavior change for the single-backend deployment case.
7. **Verification** — full test suite (`uv run pytest --cov=llm_home_lab`), `uv run ruff check
   .`, `uv run ruff format --check .`, `uv run mypy src`.

Implemented test-first (`tdd` skill): one test from the spec's behavior/acceptance scenarios at a
time, red → green, then a refactor pass once all scenarios pass.

## Risks

- **Tie-breaking nondeterminism**: if scoring ties aren't broken by an explicit, stable key
  (`backend_id`) rather than dict/set iteration order, the "reproducible decisions" acceptance
  criterion silently breaks under Python's hash randomization. Cover this with an explicit
  same-score, different-`backend_id` test, not just a same-input-twice test.
- **Sticky/health interaction gap**: since sticky state is in-memory and the health-score model
  doesn't exist yet (#8), a sticky backend that goes unhealthy today can only be detected by it
  being absent from `candidates` (whatever excludes it upstream). Getting this wrong either wedges
  sessions on a dead backend or thrashes them off a briefly-flaky one — the spec's fallback
  behavior (re-score for that one request, don't overwrite the record) needs its own test rather
  than being inferred from the general sticky tests.
- **`app.py` signature change is breaking**: every caller of `create_app` (currently just
  `main.py`, but also tests) needs updating in the same change; a partial migration would leave
  some tests constructing the app with the old single-`backend` signature and failing at import
  time rather than at a useful assertion.

## Open Questions

- Same as the spec's: latency-probing ownership, hot-reload of policy, and sticky/failover
  interaction are deferred, not blocking this plan.
