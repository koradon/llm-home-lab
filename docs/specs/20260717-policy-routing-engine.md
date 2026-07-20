# Policy-Based Routing Engine

## Status

draft

## Summary

A pluggable routing layer that picks which configured backend serves each chat completion
request. Selection is driven by declared policy rules over task type, token budget, and
observed latency, with an optional sticky-session mode so a session's requests keep landing on
the backend it started with. Per the [orchestrator concept](../../Local_LLM_Orchestrator_Concept.md),
the orchestrator owns this decision so agents never have to pick a backend themselves.

## User stories

- As the orchestrator, I want to route each request to the backend our policy scores highest,
  so that requests land where they are best served.
- As an operator, I want to declare routing policy rules over task type, token budget, and
  latency, so that I can shape backend selection without code changes to the request path.
- As the orchestrator, I want a session's requests to prefer the backend they started on when
  sticky routing is enabled, so that a conversation's model behavior stays consistent across
  turns.
- As a test author, I want routing decisions to be reproducible given the same request and
  candidate state, so I can assert on routing behavior deterministically.

## Requirements

- Provide a `RoutingEngine` with a synchronous, side-effect-free `select_backend(request,
  candidates, session_id=None) -> RoutingDecision` — a pure function over its inputs (no clock,
  no randomness, no I/O), so identical inputs always produce identical output.
- `candidates` is the set of registered `ChatBackend`s (see
  [`backends/base.py`](../../src/llm_home_lab/backends/base.py)) currently considered healthy,
  together with each one's current latency estimate and advertised context window.
- A `RoutingPolicy` is an ordered list of `PolicyRule`s. Each rule contributes a score to a
  candidate based on one or more of: `task_type` (declared per request, default `"general"`),
  `token_budget` (estimated prompt tokens for the request), and `latency` (the candidate's
  current latency estimate). The candidate with the highest total score wins; ties break
  deterministically by `backend_id` ordering, not by iteration order.
- A candidate whose advertised context window is smaller than the request's estimated
  `token_budget` is excluded from scoring entirely, not merely scored low.
- `RoutingDecision` reports the chosen `backend_id`, every candidate's total score, and which
  rules matched — routing decisions must be self-explaining so they can be logged (M3 exit
  criteria: "decision logs explain model selection for each request").
- Sticky session preference: when sticky routing is enabled and `session_id` is given, the
  engine records the winning backend the first time it routes that session, and returns the
  recorded backend directly (bypassing scoring) on later requests for the same session, as
  long as that backend is still among the healthy `candidates`.
- Sticky routing is toggleable by configuration (default: enabled), independent of the policy
  rules themselves — disabling it makes every request go through normal scoring regardless of
  `session_id`.
- `create_app` (`src/llm_home_lab/api/app.py`) accepts a `RoutingEngine` and a collection of
  candidate backends in place of today's single `backend: ChatBackend` parameter, and calls
  `select_backend` per request instead of always using the one wired-in backend.
- `ChatCompletionRequest` (`src/llm_home_lab/api/models.py`) gains two optional fields,
  `session_id: str | None` and `task_type: str | None`, so callers can supply routing hints
  without breaking OpenAI wire compatibility (both default to `None` and are ignored by clients
  that do not set them).

## Behavior

**Latency-preferring rule picks the fastest healthy candidate**: given two healthy candidates
and a policy rule that scores lower latency higher, the candidate with the lower current latency
estimate is selected.

**Task-type rule narrows selection**: a rule scoped to `task_type="code"` adds score only for
matching requests; a request with a different (or absent) `task_type` does not receive that
rule's contribution and falls through to the remaining rules.

**Token budget exclusion**: a candidate whose context window is smaller than the request's
estimated token budget is dropped from the candidate set before scoring — it cannot win even if
every other rule would favor it.

**No candidates left**: if excluding by token budget (or an empty `candidates` set) leaves
nothing to score, `select_backend` raises `NoAvailableBackendError` rather than silently picking
an unsuitable backend.

**Reproducibility**: calling `select_backend` twice with the same request, the same candidate
set (including the same latency estimates), and the same session state returns the identical
`RoutingDecision` both times.

**Sticky session, first turn**: the first request for a new `session_id`, with sticky routing
enabled, is scored normally; the winning backend is then recorded as that session's sticky
backend.

**Sticky session, later turns**: a later request for the same `session_id` returns the recorded
sticky backend without re-scoring, as long as it is still present and healthy in `candidates`.

**Sticky session, recorded backend now unhealthy**: if the sticky backend is no longer in
`candidates`, that request falls back to normal scoring among the remaining candidates for that
one request (whether the sticky record itself is updated is an interaction with backend health
policy — see Open Questions).

**Sticky routing disabled**: with sticky routing off, repeated requests for the same
`session_id` are scored independently every time; no sticky record is read or written.

**Edge cases**:

- `session_id` omitted → sticky logic never applies; the request is scored normally.
- An unrecognized `task_type` value is not an error — it simply matches no task-type rule.
- Zero healthy candidates → `NoAvailableBackendError`, distinguishable from a backend runtime
  failure (`BackendError` and subclasses in `backends/base.py`).

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/features/20260717-policy-routing-engine.feature`.

## Related

- Plan: [policy-routing-engine](../plans/20260717-policy-routing-engine.md)
- Plan: [orchestrator-program](../plans/20260711-orchestrator-program.md) (M3 — policy-based routing
  engine)
- Spec: [session-manager-core](20260717-session-manager-core.md) (the `session_id` sticky routing keys
  off)
- Module: [`backends/base.py`](../../src/llm_home_lab/backends/base.py) (the `ChatBackend`
  protocol routing selects among)
- Issue: `.plan/milestones/m3-routing-and-reliability/issues/issue-001-policy-routing-engine.md`
  (#6)
- Acceptance: `docs/specs/features/20260717-policy-routing-engine.feature`

## Open Questions

- How a sticky record interacts with backend health policy (#8) once the sticky backend goes
  unhealthy — whether routing should permanently reassign the session to a new sticky backend,
  or only fall back per-request until the original recovers — is deferred to the failover
  design.
- Where per-backend latency estimates come from at this stage: #8 introduces a proper
  health-score model with probe history; until then this engine needs some minimal injected
  latency input (e.g., a rolling average the caller supplies) rather than owning probing itself.
  Resolved for now: `api/app.py` injects each host's current `in_flight / max_concurrent_requests`
  ratio as the `latency_ms` input, so the `prefer-lower-latency` default policy behaves as
  least-connections load balancing instead of always tie-breaking to the alphabetically-first
  backend_id. A real probed latency estimate can replace this later without changing the engine.
- Whether routing policy is defined in code at startup (assumed for the initial implementation)
  or becomes hot-reloadable configuration is an open question from the program plan; not
  blocking this spec.
- Whether `task_type` should instead be inferred from the request rather than caller-declared is
  left for a later iteration if declared values prove unreliable in practice.
