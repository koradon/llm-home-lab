# Multi-Node Registry and Scheduler

## Status

draft

## Summary

A `HostRegistry` that replaces the hardcoded backend list currently wired in `main.py` with
dynamic host registration, capability/capacity metadata, and heartbeat-based liveness — so model
hosts can join and leave the orchestrator without a restart. A `SchedulingQueue` sits in front of
[`RoutingEngine.select_backend`](../../src/llm_home_lab/routing/engine.py): it admits a request
immediately if a candidate host has spare capacity, or queues it (priority first, fair within a
priority) until a slot frees up. The orchestrator itself remains a single process managing
multiple remote model hosts — this spec does not introduce a distributed control plane, so
[ADR-0002](../adr/0002-sqlite-for-session-storage.md)'s SQLite choice is not revisited here.

## User stories

- As an operator, I want to add or remove a model host by registering/deregistering it, so that
  capacity changes don't require restarting the orchestrator.
- As an operator, I want a host that stops sending heartbeats to be automatically dropped from
  scheduling, so that a crashed or unreachable host doesn't keep receiving requests.
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
    `capabilities` describes what the host can serve (e.g. backend type, supported models,
    context window); `capacity` describes `max_concurrent_requests`. Re-registering an existing
    `host_id` replaces its capabilities/capacity and counts as a heartbeat.
  - `heartbeat(host_id, at)` — updates the host's last-seen timestamp. Raises if `host_id` is not
    registered (a host must register before it can heartbeat).
  - `deregister(host_id)` — explicit, immediate removal, independent of heartbeat timeout.
  - `expire_stale(at, ttl)` — removes every host whose last-seen timestamp is older than `ttl` as
    of `at`. Like `HealthMonitor.record_probe`, the registry never reads the wall clock itself;
    the caller (a periodic sweep in app wiring) supplies `at`, matching this repo's convention for
    deterministic, testable time-based state.
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

## Behavior

**Registering a new host makes it immediately eligible**: once `register` is called, the host
appears in `hosts()` and is included in scheduling candidates on the very next request — no
restart needed.

**Re-registering updates metadata without losing in-flight accounting**: calling `register` again
for an already-registered `host_id` updates capabilities/capacity and refreshes its last-seen
timestamp, but does not reset its current in-flight count.

**A host silent past its TTL is dropped on the next sweep**: `expire_stale(at, ttl)` removes any
host whose last heartbeat/registration is older than `ttl` relative to `at`; that host then no
longer appears in `hosts()` or in scheduling candidates until it registers again.

**Explicit deregistration is immediate**: `deregister(host_id)` removes the host regardless of how
recently it heartbeat, distinct from timeout-based expiry.

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

Keep scenarios in a sibling Gherkin file: `docs/specs/multi-node-registry-and-scheduler.feature`.

## Related

- Spec: [policy-routing-engine](policy-routing-engine.md) — `RoutingEngine.select_backend`
  consumes the `candidates` this registry now produces (in place of `main.py`'s hardcoded list).
- Spec: [failover-and-health-policy](failover-and-health-policy.md) — `HealthMonitor.is_healthy`
  still filters candidates exactly as today; registry capacity filtering is an additional,
  independent filter, not a replacement for health filtering.
- ADR: [0002-sqlite-for-session-storage](../adr/0002-sqlite-for-session-storage.md) — names this
  milestone as the SQLite-vs-PostgreSQL revisit trigger; confirmed with the user that "multi-node"
  here means one orchestrator process managing multiple remote model hosts, not multiple
  orchestrator instances, so the revisit does not apply and registry state is in-memory
  (unpersisted), matching `HealthMonitor`'s pattern rather than the SQLite-backed session/workspace
  stores.
- Module: [`main.py`](../../src/llm_home_lab/main.py) — current hardcoded `candidates` construction
  this spec replaces.
- Module: [`api/app.py`](../../src/llm_home_lab/api/app.py) — wires `candidates`, `router`, and
  `health_monitor` together today; will also wire the registry and scheduling queue.
- Plan: (to be written) `docs/plans/multi-node-registry-and-scheduler.md`
- Plan: [orchestrator-program](../plans/orchestrator-program.md) (M4 — multi-node registry and
  scheduler)
- Issue: `.plan/milestones/m4-production-hardening/issues/issue-001-multi-node-registry-and-scheduler.md`
  (#10)
- Acceptance: `docs/specs/multi-node-registry-and-scheduler.feature`

## Open Questions

- How a host actually reaches the registry — does a host call in over a new HTTP endpoint (e.g.
  `POST /v1/nodes/register`, `POST /v1/nodes/{id}/heartbeat`), or is registration a config/CLI-driven
  call into `HostRegistry` with heartbeats piggy-backing on the orchestrator's existing outbound
  `check_health()` probing — is an app-wiring decision left to the plan, matching how
  [failover-and-health-policy](failover-and-health-policy.md) deferred "who calls `record_probe`."
- Who calls `expire_stale` and on what cadence (a background sweep task vs. piggy-backing on each
  incoming request) is likewise left to the plan.
- Whether `capabilities` needs a structured schema (e.g. supported model names) beyond what
  `RoutingCandidate` already carries (`context_window`), or whether it's deferred until a routing
  policy rule actually needs to key on it, is left open until such a rule is needed.
- Whether queued-but-undispatchable requests need a max queue depth or timeout (to fail loudly
  instead of queuing forever when no host will ever satisfy them) is deferred — not required by
  this issue's acceptance criteria, but worth flagging before production use.
