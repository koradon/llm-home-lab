# Local LLM Orchestrator — Program Plan

## Status

completed — M1, M2, M3, and M4 are all delivered (issues #1-#12 closed); see
[roadmap](../roadmap/README.md) for what follows.

## Related

- Vision: [README](../../README.md)
- Concept: [Local LLM Orchestrator Concept](../../Local_LLM_Orchestrator_Concept.md)
- Planning source of truth: `.plan/` (Planhub), synced to GitHub milestones/issues
- Specs: none yet — behavior for individual milestones should be captured in `docs/specs/` as it is defined

> This plan mirrors the milestone roadmap and issues tracked in `.plan/`. It records the
> **how**: delivery order (M1 → M2 → M3 → M4), scope per milestone, and the acceptance
> criteria each milestone must meet before the next begins.

## Scope

Covers the end-to-end build of the local LLM orchestrator: a single OpenAI-compatible API
gateway that routes agent requests across multiple stateless local model hosts, with an
orchestrator-owned state layer (session, workspace, tools), policy-driven routing, and
production-grade multi-node operations.

Out of scope for this plan:

- Exact dates and resourcing (tracked as horizons, not deadlines).
- Model training or fine-tuning; the orchestrator treats backends as stateless workers.
- Agent/client implementations that consume the API.

## Delivery status

| Milestone | Goal | Issues | Status |
| --- | --- | --- | --- |
| M1 — Core Orchestrator Foundation | OpenAI-compatible gateway + one LM Studio backend | #1, #2, #3 | done |
| M2 — Stateful Session and Tool Context | orchestrator-owned session/workspace/tool state | #4, #5, #7 | done |
| M3 — Intelligent Routing and Reliability | policy routing, failover, context cache | #6, #8, #9 | done |
| M4 — Production Hardening and Multi-Node Operations | multi-node registry, monitoring/SLOs, security baseline | #10, #11, #12 | done |

Tracking issue #13 (roadmap sequencing) was closed once this table and each milestone's plan
status were updated to `completed`. Future work is tracked in [docs/roadmap/](../roadmap/README.md)
instead.

## Steps

Delivery is sequential across milestones. Within a milestone, issues may proceed in parallel
unless noted. A milestone is complete only when all of its exit criteria hold.

### M1 — Core Orchestrator Foundation

Goal: a minimal but working orchestrator that exposes one stable API endpoint and forwards
requests to at least one LM Studio backend.

1. **OpenAI-compatible API gateway** — implement `/v1/chat/completions` compatibility,
   validate and map requests to the internal orchestration format, return OpenAI-style
   responses and errors.
2. **LM Studio backend adapter** — define a backend adapter interface, implement the LM Studio
   adapter for one host, and support timeout, retry, and normalized error mapping.
3. **Health and telemetry baseline** — add `/health/live` and `/health/ready` (readiness
   reflects backend availability); emit structured logs with request id, backend id, latency,
   and status.

Exit criteria:

- OpenAI-compatible clients call the endpoint with no code changes; validation errors are
  machine-readable.
- Adapter executes prompts against a configured LM Studio host; transport/timeout failures are
  classified and logged.
- Liveness/readiness are consumable by local monitoring; failing backends are visible in
  readiness output; every request has correlated log fields.

### M2 — Stateful Session and Tool Context

Goal: make model switching safe by keeping all operational context in the orchestrator.

1. **Session manager core** — design the session data model (messages, summaries, decisions,
   constraints), persist snapshots to local storage, and provide append/read/trim/summarize
   APIs.
2. **Workspace state capture** — capture branch, staged/unstaged diff metadata, open files, and
   test status into a normalized, size-bounded snapshot schema; add pruning for large repos.
3. **Tool state abstraction** — model-independent tool session layer (filesystem and terminal
   first), tracking invocation history and exposing replay/continuation hooks.

Exit criteria:

- Session state survives orchestrator restart; session APIs are deterministic and documented.
- Snapshots generate in predictable time with bounded, configurable payload size.
- Tool state (including terminal cwd/env/process continuity) is reusable across model switches,
  verified across two different backends.

### M3 — Intelligent Routing and Reliability

Goal: route each request to the best available model while keeping user outcomes stable.

1. **Policy-based routing engine** — pluggable routing inputs (task type, token budget,
   latency), a backend scoring/selection algorithm, and sticky model preference for active
   sessions.
2. **Failover and backend health policy** — health-score model with probe history, automatic
   failover with cooldown windows, and exclusion of unhealthy backends until recovery criteria
   are met.
3. **Context cache and compaction** — prompt-fragment cache keyed by session-state hash,
   selective retrieval with summarization fallback, and metrics for hit ratio and latency
   impact.

Exit criteria:

- Requests are routed per declared policy; routing decisions are reproducible in tests; sticky
  sessions are config-toggleable.
- Simulated outages trigger automatic rerouting; unhealthy backends are excluded until recovery;
  failover events surface in logs/metrics.
- Cache reduces median context-assembly time in benchmarks without dropping facts required for
  active tasks; hit/miss and compaction frequency are exposed as metrics.

### M4 — Production Hardening and Multi-Node Operations

Goal: evolve the orchestrator into a dependable multi-node control plane.

1. **Multi-node registry and scheduler** — register hosts with capability/capacity metadata, a
   scheduling queue with fairness and priority, and heartbeat-based auto de-registration.
2. **Monitoring, SLOs, and alerting** — define SLIs (availability, p95 latency, failover
   success), export dashboard metrics, and add actionable alerts for SLO burn and backend
   saturation.
3. **Security and governance baseline** — API authentication and per-client authorization, tool
   access policy with audit logging, and a secret management / credential rotation process.

Exit criteria:

- Hosts join/leave without restart; scheduler distributes per policy and node capacity; node
  metadata is queryable.
- Dashboards show current and historical health; alert rules are tested against simulated
  incidents; each critical alert links to a runbook.
- Unauthorized requests are blocked and audited; tool calls carry identity context; a security
  baseline document exists for contributors.

## Risks

- **State model churn**: session/workspace/tool schemas (M2) underpin routing and caching (M3);
  late schema changes ripple forward. Stabilize the schemas early with versioning.
- **Backend heterogeneity**: LM Studio behavior may differ across hosts/OSes, complicating the
  adapter contract and health scoring.
- **Cache correctness vs. latency**: aggressive compaction (M3) risks dropping facts needed for
  active tasks; requires benchmark guardrails, not just latency wins.
- **Operational scope creep in M4**: multi-node, monitoring, and security are each large; guard
  the milestone boundary so hardening does not block earlier value delivery.
- **Sequencing dependency**: routing/failover (M3) assumes reliable health signals from M1 and
  durable state from M2; slippage in earlier milestones compounds.

## Open Questions

- Local storage choice for session/workspace/tool state (embedded DB vs. files) — deserves an
  ADR before M2 implementation.
- Routing policy configuration format and whether policies are hot-reloadable.
- Cache key strategy: exact session-state hash vs. semantic fragment matching.
- Multi-node transport/registry protocol (heartbeat mechanism, discovery) for M4.
- Authentication model for the API and per-client authorization granularity.
