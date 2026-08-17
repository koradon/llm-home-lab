# Capacity Summary Endpoint

## Status

draft

## Summary

A new `GET /v1/capacity` endpoint that returns a single number: the total request concurrency
currently available across every *online* registered host. It exists so that external consumers
— today, `facts-service`'s Procrastinate curation worker — can size their own concurrency to
match this orchestrator's actual live capacity instead of a value a human computed and hardcoded
once.

## User stories

- As the operator of `facts-service`'s curation worker, I want the worker to size its own
  concurrency from live orchestrator capacity, so that I no longer manually compute and set
  `CURATION_WORKER_CONCURRENCY` every time a host is added, removed, or goes offline.
- As an operator of this orchestrator, I want "online capacity" to mean the same thing everywhere
  it's reported, so that `/v1/capacity` and `/v1/nodes` can never disagree about which hosts
  count.

## Requirements

- New route `GET /v1/capacity` in `src/llm_home_lab/api/app.py`, alongside the existing
  `/v1/nodes` routes.
- No request body or query parameters.
- Response body: `{"total_max_concurrent_requests": <int>}`.
- The total is computed as the sum of `host.capacity.max_concurrent_requests` over every host in
  `registry.hosts()` for which `_node_status(host.host_id, at) == "online"`, evaluated at a single
  `at = datetime.now(UTC)` timestamp for the whole request (the same pattern `GET /v1/nodes`
  already uses).
- This reuses the existing `_node_status` helper directly — it must not re-implement or
  approximate the online/offline/unknown determination. See
  [ADR-0008](../adr/0008-dedicated-capacity-summary-endpoint.md) for why a shared helper (not
  parallel logic) is required.
- Hosts with `status` `"offline"` or `"unknown"` contribute nothing to the total, regardless of
  their registered `max_concurrent_requests`.
- No new authentication/authorization requirements beyond whatever already applies to
  `GET /v1/nodes` — this endpoint exposes strictly less information (an aggregate, not per-host
  detail).

## Behavior

**Zero online hosts is not an error.** If the registry is empty, or every registered host is
`"offline"`/`"unknown"`, the endpoint returns `HTTP 200` with
`{"total_max_concurrent_requests": 0}` — an empty sum, not a 4xx/5xx. A consumer autoscaling on
this value should treat `0` as "no capacity available right now," not as a signal to retry
differently.

**A host with `max_concurrent_requests=0` contributes exactly `0`** to the sum whether it is
online or not — there is nothing special-cased about a zero-capacity host; it is included in the
"online" filter like any other host and simply adds nothing.

**This is read-only and has no side effects.** `GET /v1/capacity` does not probe hosts itself; it
reads whatever health state the background poller ([ADR-0006](../adr/0006-background-health-poller.md))
has already recorded, exactly as `GET /v1/nodes` does. It performs no I/O beyond reading
in-memory/registry state.

**Consistency with `/v1/nodes`.** For the same instant, summing `max_concurrent_requests` over
the hosts `GET /v1/nodes` reports with `"status": "online"` must equal
`total_max_concurrent_requests` from `GET /v1/capacity` — both derive from the same
`_node_status` calls.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file:
`docs/specs/features/2026-08-10-capacity-summary-endpoint.feature`.

## Related

- ADR: [0008-dedicated-capacity-summary-endpoint](../adr/0008-dedicated-capacity-summary-endpoint.md)
  — why a dedicated endpoint was chosen over client-side aggregation of `/v1/nodes`
- Spec: [20260717-multi-node-registry-and-scheduler](20260717-multi-node-registry-and-scheduler.md)
  — `GET /v1/nodes`, host registry, `_node_status`
- Spec: [20260717-failover-and-health-policy](20260717-failover-and-health-policy.md) — the
  online/offline/unknown health model `_node_status` reports
- Consumer (cross-repo, not in this repository): `facts-service`'s Procrastinate curation worker,
  tracked at `tripper/facts-service/.plan/issues/20260810-curation-worker-autoscale-concurrency.md`
- Issue: `.plan/milestones/m4-production-hardening-and-multi-node-operations/issues/20260810-add-capacity-summary-endpoint.md`
- Acceptance: `docs/specs/features/2026-08-10-capacity-summary-endpoint.feature`

## Open Questions

- Whether a future consumer needs a breakdown by backend type or per-host capacity alongside the
  total — out of scope for now; `GET /v1/nodes` already covers per-host detail if a consumer needs
  it.
- Whether `/v1/capacity` should be cached like `/v1/nodes`' external-load probe — not needed here
  since this endpoint does no I/O of its own; deferred if that changes.
