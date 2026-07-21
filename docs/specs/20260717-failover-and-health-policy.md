# Failover and Backend Health Policy

## Status

draft

## Summary

A health-tracking policy that turns a backend's raw, point-in-time `check_health()` probes into
a stable healthy/unhealthy decision with hysteresis, so the orchestrator stops routing to a
backend the moment it degrades and only resumes once it has demonstrably recovered — instead of
flapping on every noisy probe. The [routing engine](20260717-policy-routing-engine.md) already assumes
its `candidates` are "currently considered healthy"; this policy is what produces that filtered
list.

## User stories

- As the orchestrator, I want a backend excluded from routing as soon as it degrades, so that
  requests stop failing against a backend that's already down.
- As an operator, I want a recently-recovered backend to prove itself over a few consecutive
  successful probes before it takes traffic again, so that a flapping backend doesn't thrash
  routing decisions.
- As an operator, I want every failover and recovery transition logged and queryable, so that I
  can see why routing behavior changed without reproducing the outage.
- As a test author, I want the health state machine driven by explicit timestamps rather than
  the wall clock, so cooldown and recovery behavior is deterministic in tests.

## Requirements

- Provide a `HealthMonitor` with `record_probe(backend_id, healthy, at)` — `at` is a caller-
  supplied timestamp, never read from the system clock internally, so the monitor's behavior is
  as deterministic and testable as [`RoutingEngine`](../../src/llm_home_lab/routing/engine.py).
- Provide `HealthMonitor.is_healthy(backend_id, at) -> bool` — the query callers use to decide
  whether a backend belongs in the `candidates` sequence passed to
  `RoutingEngine.select_backend`. A backend with no recorded probes is healthy by default.
- Track per-backend state: consecutive failure count, consecutive (post-cooldown) success count,
  current healthy/unhealthy status, and the timestamp its current unhealthy period began.
- **Failure threshold**: `failure_threshold` (default 3) consecutive failed probes transition a
  healthy backend to unhealthy and start a cooldown window.
- **Cooldown window**: `cooldown` (default 30 seconds) is the minimum time a backend stays
  excluded after becoming unhealthy, regardless of any probes recorded during that window —
  probes are still recorded (and count toward the score below) but cannot end the cooldown
  early.
- **Recovery threshold**: once the cooldown has elapsed, `recovery_threshold` (default 2)
  consecutive successful probes transition the backend back to healthy. A failed probe during
  this post-cooldown recovery phase resets the success count to zero and restarts the cooldown
  window from that failure's timestamp.
- **Health score**: `HealthMonitor.health_score(backend_id) -> float` reports the fraction of
  healthy probes in the most recent bounded window of probe history (default last 20 probes),
  `1.0` if no probes are recorded yet. This is a metrics/observability signal, not what drives
  the exclusion decision — the consecutive-failure/cooldown/recovery state machine above is the
  sole authority on `is_healthy`.
- **Failover events**: every healthy→unhealthy or unhealthy→healthy transition is logged via a
  dedicated `llm_home_lab.health` logger (mirroring the `llm_home_lab.access` logger in
  `src/llm_home_lab/api/app.py`) and appended to a queryable `HealthMonitor.events` list
  (`FailoverEvent`: `backend_id`, `from_healthy`, `to_healthy`, `at`), so failover activity is
  visible without grepping probe history.

## Behavior

**Below threshold, still healthy**: fewer than `failure_threshold` consecutive failed probes
leaves a backend healthy — `is_healthy` stays `True` and no failover event is recorded.

**Crossing the failure threshold triggers failover**: the `failure_threshold`-th consecutive
failed probe flips the backend to unhealthy, records a healthy→unhealthy `FailoverEvent`, and
starts the cooldown window from that probe's timestamp.

**Excluded for the full cooldown, even if probes turn healthy immediately**: a successful probe
recorded before the cooldown window elapses does not end the exclusion — `is_healthy` remains
`False` until `at >= unhealthy_since + cooldown`.

**Recovery requires consecutive successes after cooldown**: once the cooldown has elapsed,
`recovery_threshold` consecutive successful probes are required before `is_healthy` returns
`True` again; a single success is not enough when `recovery_threshold > 1`.

**A failure during recovery restarts the cooldown**: if a probe fails after the cooldown has
elapsed but before `recovery_threshold` successes have accumulated, the success count resets to
zero and a new cooldown window starts from that failure's timestamp — the backend does not
recover on the interrupted streak.

**Crossing the recovery threshold ends the failover**: the probe that completes
`recovery_threshold` consecutive successes (after cooldown) flips the backend back to healthy
and records an unhealthy→healthy `FailoverEvent`.

**Health score reflects recent probe history independent of exclusion state**: `health_score`
changes with every recorded probe (bounded to the most recent window) even while `is_healthy` is
pinned `False` by an active cooldown — the score is informational and never overrides the state
machine's exclusion decision.

**No probes recorded yet**: a backend with zero recorded probes is healthy (`is_healthy` returns
`True`) and reports a `health_score` of `1.0` — matching how a newly-registered backend should be
usable immediately, not presumed guilty.

**Resolves the routing engine's sticky/health open question**: filtering `candidates` by
`HealthMonitor.is_healthy` before calling `RoutingEngine.select_backend` means an unhealthy
sticky backend is simply absent from `candidates` for that call. `RoutingEngine`'s existing
behavior — re-score among the remaining candidates for that one request, without overwriting the
sticky record — is exactly what fires, with no changes needed to `RoutingEngine` itself and no
awareness of health policy required inside it. The sticky record then resolves back to the
recovering backend automatically once `HealthMonitor.is_healthy` reports it healthy again.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/features/20260717-failover-and-health-policy.feature`.

## Related

- Spec: [policy-routing-engine](20260717-policy-routing-engine.md) — `HealthMonitor.is_healthy` is what
  filters the `candidates` sequence that spec's `RoutingEngine.select_backend` consumes; this
  spec also resolves that spec's deferred Open Question on sticky/health interaction (see
  Behavior above).
- Module: [`backends/base.py`](../../src/llm_home_lab/backends/base.py) — `BackendHealth` /
  `ChatBackend.check_health()`, the raw per-probe signal this policy adds hysteresis on top of.
- Plan: [failover-and-health-policy](../plans/20260717-failover-and-health-policy.md)
- Plan: [orchestrator-program](../plans/20260711-orchestrator-program.md) (M3 — failover and backend
  health policy)
- Issue: `.plan/milestones/m3-routing-and-reliability/issues/issue-002-failover-and-health-policy.md`
  (#8)
- Acceptance: `docs/specs/features/20260717-failover-and-health-policy.feature`

## Open Questions

- ~~Who calls `record_probe` and on what cadence~~ — resolved: a background poller (started via
  the FastAPI lifespan) probes every registered host on a fixed interval, in addition to
  `/health/ready` continuing to probe synchronously for on-demand callers. See
  `docs/adr/0006-background-health-poller.md` and
  `docs/plans/20260720-background-health-poller.md`. Timestamps are still supplied by the caller
  either way — `HealthMonitor` itself remains clock-agnostic.
- Whether `failure_threshold`/`cooldown`/`recovery_threshold` should be configurable per-backend
  rather than global monitor-wide defaults is deferred until a concrete need for per-backend
  tuning appears.
- Whether `health_score` should ever feed into `RoutingEngine`'s policy scoring (as an additional
  `PolicyRule` input) is left for a later iteration — today it is observability-only.
