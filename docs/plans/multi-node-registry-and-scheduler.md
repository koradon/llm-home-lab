# Multi-Node Registry and Scheduler Plan

## Status

draft

## Related

- Spec: [multi-node-registry-and-scheduler](../specs/multi-node-registry-and-scheduler.md)
- Depends on: [policy-routing-engine](../specs/policy-routing-engine.md) and
  [`src/llm_home_lab/routing/`](../../src/llm_home_lab/routing/) (`RoutingEngine.select_backend`'s
  `candidates` sequence, which this registry now produces instead of `main.py`'s hardcoded list)
- Depends on: [failover-and-health-policy](../specs/failover-and-health-policy.md) and
  [`src/llm_home_lab/health/`](../../src/llm_home_lab/health/) (`HealthMonitor.is_healthy` keeps
  filtering candidates unchanged; registry capacity filtering is applied independently)
- Issue: #10 (Build multi-node registry and scheduler, M4)

## Scope

Build a `HostRegistry` (`src/llm_home_lab/registry/`) for dynamic host registration, heartbeat,
explicit/timeout-based de-registration, and per-host capacity accounting; and a `SchedulingQueue`
(`src/llm_home_lab/scheduling/`) that admits a request immediately when an eligible host has a
free slot, or queues it (priority first, round-robin-fair within a priority tier) otherwise. Wire
both into `create_app`/`main.py`, replacing the hardcoded `candidates` list.

Resolving the spec's deferred wiring questions for this plan:

- **Registration transport**: new HTTP endpoints — `POST /v1/nodes/register`,
  `POST /v1/nodes/{host_id}/heartbeat`, `DELETE /v1/nodes/{host_id}`. Unlike health probing (where
  the orchestrator polls hosts outward via `check_health()`), a host must actively announce
  itself to join — there's no existing outbound mechanism to piggy-back on the way
  `/health/ready` did for probes.
- **`expire_stale` cadence**: piggy-back on the existing `/health/ready` handler, which already
  iterates all candidates — add one `registry.expire_stale(now, ttl)` call there rather than
  introducing a background poller/scheduler thread, mirroring the failover plan's choice to avoid
  a poller.
- **Capacity schema**: minimal — `HostCapabilities(backend_type: str, context_window: int)` and
  `HostCapacity(max_concurrent_requests: int)`. No richer capability matching (e.g. named model
  lists) until a routing policy rule actually needs it.

Out of scope for this plan (per the spec's Open Questions):

- Max queue depth or a per-request queue timeout that fails a request outright when no host will
  ever satisfy it — not required by the issue's acceptance criteria. The chat-completions handler
  does apply a bounded wait (see Step 5) so a request can't hang forever, but that's an app-wiring
  safeguard, not a `SchedulingQueue` feature.
- Structured capability matching beyond `backend_type`/`context_window`.

## Steps

1. **Registry package** (`src/llm_home_lab/registry/`, new package sibling to `backends/`,
   `routing/`, `health/`, `state/`) — `__init__.py` exporting the public surface.
2. **Registry models** (`src/llm_home_lab/registry/models.py`) — `HostCapabilities`,
   `HostCapacity`, `HostInfo` (`host_id`, capabilities, capacity, `in_flight: int`,
   `last_seen: datetime`), and a `HostNotRegisteredError` for `heartbeat`/`deregister` on an
   unknown `host_id`.
3. **`HostRegistry`** (`src/llm_home_lab/registry/registry.py`) — `register`, `heartbeat`,
   `deregister`, `expire_stale(at, ttl)`, `hosts()`, `in_flight(host_id)`, `acquire_slot(host_id)`
   / `release_slot(host_id)` (capacity accounting the scheduler drives). All time-based behavior
   takes `at` explicitly — no internal wall-clock reads, matching `HealthMonitor`.
4. **Scheduling package** (`src/llm_home_lab/scheduling/`) — `models.py` (`QueueEntry`:
   `request_id`, `session_id`, `priority`, `at`) and `queue.py` (`SchedulingQueue`): `enqueue`,
   `dispatch(registry, at)`. Internally: a dict of priority tier → ordered dict of `session_id` →
   deque of entries, plus a per-tier round-robin cursor over `session_id`s, so `dispatch` scans
   tiers ascending and, within a tier, cycles session queues from the cursor rather than draining
   one session's backlog first.
5. **App wiring** (`src/llm_home_lab/api/app.py`) — `create_app` gains `registry: HostRegistry`
   and `scheduling_queue: SchedulingQueue` parameters. New router: `POST /v1/nodes/register`,
   `POST /v1/nodes/{host_id}/heartbeat`, `DELETE /v1/nodes/{host_id}`. `/health/ready` additionally
   calls `registry.expire_stale(now, ttl)`. `chat_completions` builds candidates from
   `registry.hosts()` (converted to `RoutingCandidate`), filtered by `health_monitor.is_healthy`
   (unchanged) and by remaining capacity; if at least one eligible host has a free slot, dispatch
   proceeds immediately (`acquire_slot`, route, `release_slot` on completion). If every eligible
   host is full, `enqueue` the request and poll `dispatch` on a short bounded backoff (e.g. up to
   30s total) until a slot frees, returning a 503 (reusing the existing `NoAvailableBackendError`
   → 503 mapping) if the bound is exceeded. This is a polling wait, not event-driven wakeup —
   acceptable at this scale per the Scope note above, and can be swapped for an `asyncio.Event`-
   per-request design later without changing `SchedulingQueue`'s API.
6. **Entry point** (`src/llm_home_lab/main.py`) — `create_default_app` constructs one shared
   `HostRegistry` and `SchedulingQueue`, passed to `create_app`; the hardcoded `candidates` list is
   removed (hosts now join via the registration endpoint, e.g. from a startup config loop or
   external agent — out of scope here beyond the endpoint existing).
7. **Existing test updates** (`tests/test_gateway.py`, `tests/test_health.py`) — thread
   `HostRegistry`/`SchedulingQueue` instances through `create_app`/`_app_for` calls, registering
   the same backends the tests already construct as candidates so existing scenarios keep passing
   under the new signature.
8. **Verification** — full test suite (`uv run pytest --cov=llm_home_lab`), `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy src`.

Implemented test-first (`tdd` skill): one test from the spec's behavior/acceptance scenarios at a
time, red → green, then a refactor pass once all scenarios pass. Suggested order: `HostRegistry`
(register/heartbeat/deregister/expire_stale/hosts) fully first — it has no dependency on the
queue — then `SchedulingQueue` (priority, then fairness) against a fake/in-memory registry, then
app wiring last.

## Risks

- **Polling-based dispatch wait**: the bounded-backoff poll in `chat_completions` (Step 5) adds
  latency jitter (up to one poll interval) versus an event-driven wakeup, and burns a coroutine
  slot per queued request for the duration of the wait. Acceptable for home-lab-scale concurrency;
  revisit if queue depths or wait times grow large enough to matter.
- **Round-robin cursor state leak**: the per-tier `session_id` cursor in `SchedulingQueue` must be
  cleaned up when a session's queue empties, or the cursor can grow unbounded / point at stale
  entries. Cover this with an explicit test (enqueue, fully drain one session, enqueue a new
  session) rather than only testing the steady-state fairness case.
- **Capacity accounting drift**: `acquire_slot`/`release_slot` must be paired reliably even when a
  backend call raises (`BackendTimeoutError`, `BackendConnectionError`) — a missed `release_slot`
  on the error path would permanently under-report a host's free capacity. Wrap the dispatch call
  in the same `try/finally` pattern already used for `create_app`'s error handling.
- **Two independent filters on `candidates`**: health filtering and capacity filtering are both
  applied before routing, but by different modules (`HealthMonitor`, `HostRegistry`) that don't
  know about each other. Keep them composed in `app.py` only (as today's health filter already is)
  rather than teaching either module about the other's concept of eligibility.

## Open Questions

- Same as the spec's: whether `capabilities` needs a richer schema, and whether a max queue depth
  or per-request queue timeout is needed, are deferred until a concrete need appears.
