# Multi-Node Registry and Scheduler

## Status

draft

## Summary

A `HostRegistry` that replaces the hardcoded backend list currently wired in `main.py` with
dynamic host registration and capability/capacity metadata — so model hosts can join and leave the
orchestrator without a restart. ~~Heartbeat-based liveness~~ — **superseded by
[ADR-0004](../adr/0004-persist-node-registry-no-auto-deregistration.md)**: registration no longer
expires on a TTL; reachability ("online"/"offline") is `HealthMonitor`'s concern alone, surfaced
via the API and the TUI rather than causing removal from the registry. A `SchedulingQueue` sits in front of
[`RoutingEngine.select_backend`](../../src/llm_home_lab/routing/engine.py): it admits a request
immediately if a candidate host has spare capacity, or queues it (priority first, fair within a
priority) until a slot frees up. The orchestrator itself remains a single process managing
multiple remote model hosts — this spec does not introduce a distributed control plane, so
[ADR-0002](../adr/0002-sqlite-for-session-storage.md)'s SQLite choice is not revisited here.

## User stories

- As an operator, I want to add or remove a model host by registering/deregistering it, so that
  capacity changes don't require restarting the orchestrator.
- As an operator, I want an unreachable host automatically excluded from scheduling, so that a
  crashed or unreachable host doesn't keep receiving requests — but I want it to stay visible as
  "offline" rather than silently vanish from the registry (see
  [ADR-0004](../adr/0004-persist-node-registry-no-auto-deregistration.md)).
- As the orchestrator, I want to know each host's capabilities and current capacity, so that
  routing only considers hosts that can actually serve a request right now.
- As a caller, I want a request that arrives when every eligible host is at capacity to queue
  rather than fail, and I want higher-priority requests served first, so that best-effort traffic
  doesn't starve latency-sensitive traffic.
- As an operator, I want to inspect registered hosts and their metadata, so that I can diagnose
  capacity or scheduling problems without reading logs.
- As a test author, I want registry and scheduler behavior driven by caller-supplied timestamps,
  matching [`HealthMonitor`](../../src/llm_home_lab/health/monitor.py), so heartbeat-timeout
  behavior is deterministic in tests.

## Requirements

- Provide a `HostRegistry` with:
  - `register(host_id, capabilities, capacity, at)` — adds or re-registers a host.
    `capabilities` describes what the host can serve and how to reach it (`backend_type`,
    `context_window`, `base_url`); `capacity` describes `max_concurrent_requests`. Re-registering
    an existing `host_id` replaces its capabilities/capacity and counts as a heartbeat.
    `HostRegistry` itself stays backend-agnostic — it never constructs a `ChatBackend`; app wiring
    uses `capabilities.backend_type`/`base_url` to build one (see Related).
  - `get(host_id) -> HostInfo` — returns the current state of one host. Raises
    `HostNotRegisteredError` if `host_id` is not registered. Backs the partial-update endpoint below
    so it can merge changed fields into current state without scanning `hosts()`.
  - `heartbeat(host_id, at)` — updates the host's last-seen timestamp. Raises if `host_id` is not
    registered (a host must register before it can heartbeat).
  - `deregister(host_id)` — explicit, immediate removal. This is now the **only** removal path —
    see [ADR-0004](../adr/0004-persist-node-registry-no-auto-deregistration.md).
  - ~~`expire_stale(at, ttl)`~~ — **removed per ADR-0004**: a host is never removed from the
    registry just because it stopped heartbeating; `last_seen` is retained as data, not used to
    drive removal.
  - `hosts() -> Sequence[HostInfo]` — a queryable snapshot of every currently registered host
    (`host_id`, capabilities, capacity, in-flight count, last-seen) for diagnostics.
  - `in_flight(host_id) -> int` and internal accounting hooks (`acquire_slot`/`release_slot`) the
    scheduler uses to track how many requests a host is currently serving.
- Provide a `SchedulingQueue` with:
  - `enqueue(request_id, session_id, priority, at) -> None` — adds a pending request. `priority`
    is an `int`, lower value dispatches first (default tier `0`).
  - `dispatch(registry, at) -> str | None` — returns the `request_id` of the next request that can
    be admitted (some eligible, non-full host exists), removing it from the queue, or `None` if
    every waiting request's eligible hosts are all at capacity.
  - **Priority first**: a lower-priority-number request always dispatches before a
    higher-priority-number one, regardless of enqueue order.
  - **Fair within a priority tier**: within the same priority, dispatch does not let one
    `session_id` monopolize a host while other sessions have requests waiting at that same tier —
    dispatch cycles round-robin across distinct `session_id`s present in that tier, rather than
    strict FIFO, so one chatty session can't starve others queued at the same priority.
  - A request the queue cannot immediately dispatch is not dropped — it stays queued until
    `dispatch` is called again (e.g. on the next `release_slot` or heartbeat sweep) and a host has
    a free slot.
- `main.py`'s hardcoded `candidates` list is replaced: `RoutingEngine.select_backend`'s
  `candidates` argument is now built from `HostRegistry.hosts()` (converted to
  `RoutingCandidate`), filtered by `HealthMonitor.is_healthy` exactly as today, so registry
  integration does not change `RoutingEngine` or `HealthMonitor` themselves.
- A host with a full capacity (`in_flight(host_id) == capacity.max_concurrent_requests`) is
  excluded from the candidates offered to `RoutingEngine` for a given dispatch attempt, the same
  way an unhealthy host is excluded today.

## Model availability control

Added after an operational incident: requesting a model that exists in LM Studio's catalog but
isn't currently loaded causes LM Studio to just-in-time load it, silently doubling memory use.
The gateway must not be able to trigger that by simply forwarding whatever `model` a client asks
for.

- `HostCapabilities` gains `allowed_models: list[str] | None = None`. When set, a request for a
  `model` not in this list is never routed to that host — no backend call is made to check.
- When `allowed_models` is `None` (the default — no static list configured), the gateway instead
  asks the backend what it currently has loaded via an *optional* `ChatBackend.list_models() ->
  list[str] | None` method, checked with `getattr`/`hasattr` rather than added to the `ChatBackend`
  Protocol as a required method — a backend that doesn't implement it (including every existing
  test double) is simply not subject to this check, preserving today's permissive behavior for
  those. `LMStudioBackend` implements it via the same `GET /v1/models` endpoint `check_health`
  already probes, returning `None` (not raising) on a transport error, matching `check_health`'s
  error-tolerant style.
- A host is "model-capable" for a request if: `allowed_models` contains the requested model, OR
  `allowed_models` is `None` and (`list_models()` isn't implemented, OR it returns `None`, OR it
  returns a list containing the requested model). Any other case excludes that host for this
  request.
- If **no registered host** is model-capable for the requested model, the gateway rejects the
  request with `400` (a `ModelNotAvailableError`) — a distinct, client-error case from the existing
  `NoAvailableBackendError`/`503` ("hosts exist but are unhealthy or full right now"). Model
  capability is computed once per request, before health/capacity filtering, not re-checked on
  every poll iteration of the capacity-wait loop.
- `POST /v1/nodes/register` accepts an optional `allowed_models` field in its payload, threaded
  straight into `HostCapabilities`.

## Partial node updates

Added because changing one field of an already-registered host (e.g. bumping
`max_concurrent_requests` after tuning) otherwise required re-sending the full registration
payload — an operator had to `GET /v1/nodes` first, copy every existing field, and re-`POST` the
whole thing to `/v1/nodes/register`, or risk silently clearing fields it omitted.

- `PATCH /v1/nodes/{host_id}` accepts the same fields as `POST /v1/nodes/register` (minus
  `host_id` itself), all optional. Only fields present in the request body are changed; omitted
  fields keep their current value. Uses Pydantic's `exclude_unset` semantics, so a field must be
  entirely absent from the JSON body to be treated as "unchanged" — sending `null` for an optional
  field explicitly clears it (e.g. `{"allowed_models": null}` resets `allowed_models` to
  unrestricted).
- Returns `404` (`HostNotRegisteredError`) if `host_id` is not currently registered — a PATCH never
  creates a host; only `POST /v1/nodes/register` does.
- Implemented as a thin merge in front of the existing `register`: fetch the current `HostInfo` via
  `registry.get(host_id)`, apply the changed fields onto its `capabilities`/`capacity` via
  `dataclasses.replace`, then call `registry.register(...)` with the merged result — so it inherits
  `register`'s existing behavior for free: in-flight count is preserved, and a changed `base_url`
  reconstructs the cached backend the same way a full re-registration does.

### On-demand loading within a memory budget

A model that isn't currently loaded doesn't have to be a hard reject — LM Studio's just-in-time
loading is genuinely useful — but "how many models" is the wrong unit to bound it by, since model
sizes vary enormously (three small models can fit where one large one wouldn't). LM Studio's HTTP
API exposes neither a model's memory footprint nor overall host memory usage (confirmed: no
`/api/v0/system`-style endpoint, and no HTTP load/unload endpoint either — only the `lms` CLI or
the desktop UI can force a load/unload), so the orchestrator cannot measure this itself and relies
on operator-declared estimates.

- `HostCapabilities` gains `memory_budget_gb: float | None = None` and
  `model_sizes_gb: dict[str, float] | None = None`. Both are opt-in; a host with neither
  configured keeps the strict default from the section above (not-loaded → `400`).
- When `memory_budget_gb` is set and the requested model is not currently loaded, the gateway
  allows the request through (letting LM Studio's JIT loading fire) only if
  `sum(model_sizes_gb[m] for m in currently_loaded_models) + model_sizes_gb[requested_model] <=
  memory_budget_gb`. If *any* currently-loaded model or the requested model itself is missing from
  `model_sizes_gb`, headroom can't be verified — this fails closed (excluded), not open.
- An already-loaded model is always admitted regardless of budget — it costs no new memory, so
  there's nothing to check.
- If a host is excluded from candidacy specifically because of the budget check (over budget, or
  headroom unverifiable due to a missing size entry) rather than because the model is outright
  disallowed (`allowed_models` doesn't include it), and no *other* host can serve the request
  either, the gateway responds `503` (`ModelCapacityExceededError` — "no room right now, might
  work later if something is unloaded") instead of the flat `400` `ModelNotAvailableError` used
  when a model is simply never going to be servable anywhere.
- There is no forced eviction — the orchestrator cannot unload a model to make room (no API for
  it). The budget is a hard ceiling on what it will *trigger*, not an active memory manager.

## Behavior

**Registering a new host makes it immediately eligible**: once `register` is called, the host
appears in `hosts()` and is included in scheduling candidates on the very next request — no
restart needed.

**Re-registering updates metadata without losing in-flight accounting**: calling `register` again
for an already-registered `host_id` updates capabilities/capacity and refreshes its last-seen
timestamp, but does not reset its current in-flight count.

**A host silent past its TTL is excluded from scheduling, not removed from the registry** (per
[ADR-0004](../adr/0004-persist-node-registry-no-auto-deregistration.md)): `HealthMonitor` marks it
unhealthy/offline and `_eligible_candidates` excludes it from routing, but it still appears in
`hosts()`/`GET /v1/nodes` with its offline status visible, until an operator explicitly
`deregister`s it.

**Explicit deregistration is immediate**: `deregister(host_id)` removes the host regardless of how
recently it heartbeat, distinct from timeout-based expiry.

**Partial updates never create a host and never reset unrelated fields**: `PATCH
/v1/nodes/{host_id}` on an unregistered host is a `404`, not an implicit registration; on a
registered host, any field absent from the request body keeps its existing value.

**A request queues when all eligible hosts are full**: if every host whose capabilities satisfy
the request is at `max_concurrent_requests`, `dispatch` returns `None` for that request and it
remains queued rather than failing.

**Freeing a slot unblocks the highest-priority waiter**: when `release_slot` frees capacity on a
host, the next `dispatch` call admits the lowest-priority-number request among those the
now-available host is eligible to serve.

**No priority starves indefinitely under fairness**: within one priority tier, round-robin across
`session_id`s means a session with many queued requests does not get consecutive dispatch slots
ahead of a different session's single waiting request at the same tier.

**Node metadata is queryable independent of scheduling state**: `hosts()` always reflects the
current registry contents, whether or not any requests are currently queued or in flight.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/features/20260717-multi-node-registry-and-scheduler.feature`.

## Related

- Spec: [policy-routing-engine](20260717-policy-routing-engine.md) — `RoutingEngine.select_backend`
  consumes the `candidates` this registry now produces (in place of `main.py`'s hardcoded list).
- Spec: [failover-and-health-policy](20260717-failover-and-health-policy.md) — `HealthMonitor.is_healthy`
  still filters candidates exactly as today; registry capacity filtering is an additional,
  independent filter, not a replacement for health filtering.
- ADR: [0002-sqlite-for-session-storage](../adr/0002-sqlite-for-session-storage.md) — names this
  milestone as the SQLite-vs-PostgreSQL revisit trigger; confirmed with the user that "multi-node"
  here means one orchestrator process managing multiple remote model hosts, not multiple
  orchestrator instances, so the revisit does not apply. ~~Registry state is in-memory
  (unpersisted), matching `HealthMonitor`'s pattern~~ — **superseded by
  [ADR-0004](../adr/0004-persist-node-registry-no-auto-deregistration.md)**: the registry is now
  SQLite-backed (reusing this same engine choice) so registrations survive a restart;
  `HealthMonitor`'s own state remains in-memory and unaffected.
- ADR: [0004-persist-node-registry-no-auto-deregistration](../adr/0004-persist-node-registry-no-auto-deregistration.md)
  — removes `expire_stale`/TTL-based deregistration, persists the registry, and requires
  `GET /v1/nodes` to expose online/offline status (real-world multi-node testing surfaced that
  silent expiry was actively harmful, not just a deferred nicety).
- Module: [`main.py`](../../src/llm_home_lab/main.py) — current hardcoded `candidates` construction
  this spec replaces.
- Module: [`api/app.py`](../../src/llm_home_lab/api/app.py) — wires `candidates`, `router`, and
  `health_monitor` together today; will also wire the registry and scheduling queue, and builds a
  `ChatBackend` from each registered host's `capabilities.backend_type`/`base_url` via a small
  `backend_type -> factory` map (only `"lmstudio"` supported initially, matching
  [`main.py`](../../src/llm_home_lab/main.py)'s current single-backend setup).
- Plan: (to be written) `docs/plans/20260717-multi-node-registry-and-scheduler.md`
- Plan: [orchestrator-program](../plans/20260711-orchestrator-program.md) (M4 — multi-node registry and
  scheduler)
- Issue: `.plan/milestones/m4-production-hardening/issues/issue-001-multi-node-registry-and-scheduler.md`
  (#10)
- Acceptance: `docs/specs/features/20260717-multi-node-registry-and-scheduler.feature`

## Open Questions

- ~~How a host actually reaches the registry~~ — resolved: `POST /v1/nodes/register`,
  `PATCH /v1/nodes/{id}`, `POST /v1/nodes/{id}/heartbeat`, `DELETE /v1/nodes/{id}`,
  `GET /v1/nodes`.
- ~~Who calls `expire_stale` and on what cadence~~ — moot per ADR-0004: `expire_stale` is removed
  entirely.
- ~~Whether `capabilities` needs a structured schema (e.g. supported model names)~~ — resolved,
  see "Model availability control" below: `HostCapabilities.allowed_models`.
- Whether queued-but-undispatchable requests need a max queue depth or timeout (to fail loudly
  instead of queuing forever when no host will ever satisfy them) is deferred — not required by
  this issue's acceptance criteria, but worth flagging before production use.
- `DELETE /v1/nodes/{host_id}` does not currently work for a `host_id` containing `/` (e.g. a
  default host registered by base URL) — tracked as its own issue (see `.plan/issues/`); ADR-0004's
  "explicit deregistration is the only removal path" design depends on this being fixed.
- Exact schema for the persisted registry table(s) and which SQLite file it lives in (shared with
  session state vs. a new file) — left to the implementation issue for ADR-0004.
- ~~Exact shape of the online/offline field on `GET /v1/nodes`~~ — resolved: a `status` string field
  per host, one of `"online"`, `"offline"`, or `"unknown"`. Sourced from a new
  `HealthMonitor.has_probe_history(backend_id) -> bool` (added alongside `is_healthy`, not
  replacing it) combined with the existing `is_healthy(host_id, at)`: no recorded probe yet →
  `"unknown"`; otherwise `"online"`/`"offline"` per `is_healthy`. The TUI Nodes panel
  (`src/llm_home_lab/tui/app.py`) renders it as a colored `status` column, matching the
  severity-coloring pattern used for Alerts.
