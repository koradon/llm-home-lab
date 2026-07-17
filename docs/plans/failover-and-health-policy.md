# Failover and Backend Health Policy Plan

## Status

draft

## Related

- Spec: [failover-and-health-policy](../specs/failover-and-health-policy.md)
- Depends on: [policy-routing-engine](../specs/policy-routing-engine.md) and
  [`src/llm_home_lab/routing/`](../../src/llm_home_lab/routing/) (the `candidates` sequence this
  health-filters before it reaches `RoutingEngine.select_backend`)
- Depends on: [`backends/base.py`](../../src/llm_home_lab/backends/base.py) (`BackendHealth`,
  `ChatBackend.check_health()` — the raw per-probe signal this policy adds hysteresis on top of)
- Issue: #8 (Implement failover and backend health policy, M3)

## Scope

Build a `HealthMonitor` that turns per-backend `check_health()` probes into a stable
healthy/unhealthy decision with a failure threshold, cooldown window, and recovery threshold, and
wire it into `create_app` so unhealthy backends are filtered out of the `candidates` sequence
before routing.

Out of scope for this plan (per the spec's Open Questions):

- A background polling loop — probes are recorded only when `/health/ready` is called, piggy-
  backing on the existing readiness check rather than inventing a scheduler. This means a backend
  that never gets probed (no one calls `/health/ready`) never transitions state; that's an
  accepted limitation of this plan, not a bug to fix here.
- Per-backend threshold configuration — `failure_threshold`, `cooldown`, and `recovery_threshold`
  are global `HealthMonitor` constructor defaults, not tunable per backend.
- Feeding `health_score` into `RoutingEngine`'s `PolicyRule` scoring — it stays an
  observability-only signal for this plan.

## Steps

1. **Health package** (`src/llm_home_lab/health/`, new package sibling to `backends/`,
   `routing/`, `state/`) — `__init__.py` exporting the public surface.
2. **Health models** (`src/llm_home_lab/health/models.py`) — `ProbeResult` (`healthy: bool`, `at:
   datetime`), `FailoverEvent` (`backend_id`, `from_healthy`, `to_healthy`, `at`), and an internal
   per-backend state dataclass (`consecutive_failures`, `consecutive_successes`, `is_healthy`,
   `unhealthy_since: datetime | None`, a bounded probe-history deque for the score).
3. **`HealthMonitor`** (`src/llm_home_lab/health/monitor.py`) —
   `HealthMonitor(failure_threshold=3, recovery_threshold=2, cooldown=timedelta(seconds=30))`:
   `record_probe(backend_id, healthy, at)` updates per-backend state and appends to probe
   history; `is_healthy(backend_id, at)` returns the current exclusion decision (`True` for an
   unseen `backend_id`); `health_score(backend_id)` returns the fraction-healthy over the bounded
   history (`1.0` if empty); `events` exposes recorded `FailoverEvent`s in order. Every
   healthy→unhealthy/unhealthy→healthy transition is logged via a `llm_home_lab.health` logger,
   mirroring the `llm_home_lab.access` pattern in `api/app.py`.
4. **App wiring** (`src/llm_home_lab/api/app.py`) — `create_app` gains a `health_monitor:
   HealthMonitor` parameter. `/health/ready` keeps calling each candidate's `check_health()` as
   today, additionally feeding each result into `health_monitor.record_probe(backend_id, healthy,
   datetime.now(UTC))`. `chat_completions` filters `candidates` to `health_monitor.is_healthy(...)`
   before calling `router.select_backend`, so an unhealthy sticky backend is simply absent —
   `RoutingEngine`'s existing per-request fallback (already implemented, untouched here) takes
   over. Add an exception handler mapping `NoAvailableBackendError` to a 503 response, the same
   pattern as the existing `BackendTimeoutError` → 504 handler.
5. **Entry point** (`src/llm_home_lab/main.py`) — `create_default_app` constructs one shared
   `HealthMonitor` (default thresholds) and passes it to `create_app`.
6. **Existing test updates** (`tests/test_gateway.py`, `tests/test_health.py`) — both construct
   `create_app(candidates=..., router=...)` directly from the #6 work; thread a `HealthMonitor`
   instance through their `create_app`/`_app_for` calls so they keep passing under the new
   signature.
7. **Verification** — full test suite (`uv run pytest --cov=llm_home_lab`), `uv run ruff check
   .`, `uv run ruff format --check .`, `uv run mypy src`.

Implemented test-first (`tdd` skill): one test from the spec's behavior/acceptance scenarios at a
time, red → green, then a refactor pass once all scenarios pass.

## Risks

- **Cooldown-boundary off-by-one**: whether `at >= unhealthy_since + cooldown` or `at >
  unhealthy_since + cooldown` gates recovery eligibility is exactly the kind of boundary that
  silently inverts under a refactor. Cover the "still excluded right up to the boundary, eligible
  right after" transition with an explicit test, not just a comfortably-past-cooldown one.
- **Probe cadence dependency**: since probes only arrive via `/health/ready` calls (no background
  poller), a backend's health state is only as fresh as the last time something called that
  endpoint. If nothing calls it during an outage, `is_healthy` keeps reporting stale state — this
  is an accepted scope limitation (see Scope), but worth flagging so it isn't mistaken for a bug
  later.
- **Two-clock inconsistency risk**: `HealthMonitor` takes `at` as an explicit parameter (per the
  spec's no-hidden-clock discipline) while `app.py` supplies `datetime.now(UTC)` at the call site.
  If a future caller passes a different clock source (e.g. request-received time vs. probe-
  completed time) for different calls, cooldown/recovery math could see out-of-order timestamps.
  Keep exactly one call site (`/health/ready`) supplying probe timestamps for now.

## Open Questions

- Same as the spec's: background polling, per-backend threshold tuning, and wiring `health_score`
  into routing scoring are deferred, not blocking this plan.
