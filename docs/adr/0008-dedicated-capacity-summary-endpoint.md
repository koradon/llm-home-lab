# Expose a dedicated `GET /v1/capacity` endpoint instead of client-side aggregation

## Status

accepted

## Context and Problem Statement

`facts-service`'s Procrastinate curation worker (a separate repo/consumer, not part of this
repo) currently requires an operator to manually compute and set
`CURATION_WORKER_CONCURRENCY` before every start, based on how much LM Studio capacity happens
to be registered with this orchestrator at the time. That number drifts out of date the moment a
host is added, removed, or goes offline, and nothing keeps it in sync automatically.

`GET /v1/nodes` (`src/llm_home_lab/api/app.py`) already returns each host's
`max_concurrent_requests` and a `status` field (`"online"` / `"offline"` / `"unknown"`) computed
by `_node_status(host_id, at)`, which reads `HealthMonitor.is_healthy(...)` as fed by the
background health poller ([ADR-0006](0006-background-health-poller.md)). Host registration is
permanent ([ADR-0004](0004-persist-node-registry-no-auto-deregistration.md)) — a host never
disappears from the registry on its own, only `status` reflects live reachability. So the
capacity the curation worker actually wants — the sum of `max_concurrent_requests` over
currently-online hosts — is fully derivable from data `/v1/nodes` already exposes.

The question is where that derivation should live: computed by each consumer from `/v1/nodes`,
or computed once by this orchestrator and exposed directly.

## Considered Options

- **A — Consumer polls `/v1/nodes` and computes the sum itself.** No new API surface on this
  side. Requires every consumer (today `facts-service`'s curation worker, potentially others
  later) to independently re-implement "what counts as online capacity" — filter hosts by
  `status == "online"`, sum `max_concurrent_requests` — against a general-purpose endpoint that
  also returns per-host fields (`backend_type`, `context_window`, `external_load`, etc.) the
  consumer doesn't need just to get one number.
- **B — Add a dedicated `GET /v1/capacity` endpoint** that returns
  `{"total_max_concurrent_requests": <int>}`, computed server-side as the sum of
  `host.capacity.max_concurrent_requests` over every host for which
  `_node_status(host.host_id, at) == "online"` — reusing that exact existing helper function, not
  a re-derivation of it.

## Decision Outcome

Chosen option: **B**. This orchestrator adds `GET /v1/capacity`, which internally calls the same
`_node_status(...)` helper `/v1/nodes` already uses to decide what counts as online, so the
definition of "online" for capacity purposes can never drift from the definition used everywhere
else in this API.

The alternative (A) was rejected because it pushes an aggregation rule — "online" excludes
`"offline"` and `"unknown"`, and capacity below that filter doesn't count — into every consumer
codebase. If this orchestrator's health/status semantics ever change (for example, adding a
"degraded" status, or changing what makes a host count as unhealthy), a client-side sum would
silently go stale in every consumer until each one is separately updated. A dedicated endpoint
keeps "what counts as online capacity" in exactly one place: this orchestrator.

### Consequences

- Good, because there is exactly one authoritative definition of "online capacity"; any future
  change to `_node_status` or to what counts toward capacity automatically applies to every
  consumer without a coordinated multi-repo change.
- Good, because consumers (the curation worker today) get a minimal, purpose-built response
  shape (`{"total_max_concurrent_requests": int}`) instead of having to parse the full `/v1/nodes`
  payload and re-derive a number they don't otherwise need.
- Good, because the new endpoint is a thin, read-only aggregation over data `/v1/nodes` already
  computes — no new state, no new background work, low implementation risk.
- Bad, because this is one more public endpoint to keep stable and documented as consumers
  outside this repo start depending on it — a breaking change to its response shape now requires
  cross-repo coordination the same way `/v1/nodes` already does.
- Neutral: this does not change routing behavior or `_eligible_candidates` — `/v1/capacity` is
  purely informational, consumed by an external autoscaler, not by this orchestrator's own
  scheduling.

## Related

- Spec: `docs/specs/2026-08-10-capacity-summary-endpoint.md`
- ADR: [0004-persist-node-registry-no-auto-deregistration](0004-persist-node-registry-no-auto-deregistration.md)
  — registration permanence, why `status` (not registry membership) is the "online" signal
- ADR: [0006-background-health-poller](0006-background-health-poller.md) — how `HealthMonitor` is
  kept fresh independent of any client, which `_node_status` reads from
- Module: `src/llm_home_lab/api/app.py` (`_node_status`, `GET /v1/nodes`)
- Consumer: `facts-service` repo's Procrastinate curation worker (cross-repo, not in this
  repository) — see its issue at
  `tripper/facts-service/.plan/issues/20260810-curation-worker-autoscale-concurrency.md`
- Issue: `.plan/milestones/m4-production-hardening-and-multi-node-operations/issues/20260810-add-capacity-summary-endpoint.md`
