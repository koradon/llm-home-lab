# Background health poller

## Status

draft

## Related

- Spec: [failover-and-health-policy](../specs/20260717-failover-and-health-policy.md)
- ADR: [0006-background-health-poller](../adr/0006-background-health-poller.md)
- Supersedes the "accepted limitation" in
  [failover-and-health-policy plan](../plans/20260717-failover-and-health-policy.md) (Scope /
  Risks: "Probe cadence dependency")

## Scope

Add an asyncio background task, started via a FastAPI lifespan, that probes every registered host
on a fixed interval and feeds the same `health_monitor.record_probe()` /
`external_load_probe.probe()` path `/health/ready` already uses — so health state stays current
whether or not anything ever calls `/health/ready` (the TUI included). `/health/ready` itself is
unchanged: it keeps probing synchronously on demand, for callers who want an immediate answer
(container healthchecks, CI, an operator's own `curl`).

Out of scope: per-backend poll intervals (one global interval, like the existing
`failure_threshold`/`cooldown`/`recovery_threshold`), and changing `/health/ready`'s own behavior
(ADR 0006, option B, rejected).

## Steps

1. **Extract the shared probing routine.** `/health/ready`'s handler body (`for host in
   registry.hosts(): backend = _backend_for(...); health = await backend.check_health();
   health_monitor.record_probe(...); await external_load_probe.probe(...)`) becomes a standalone
   function inside `create_app` (e.g. `async def _probe_all_hosts() -> list[dict]`, returning the
   same per-host report shape `/health/ready` builds today), called by both the endpoint and the
   new poller — no duplicated probing logic between the two call sites.
2. **Background poll loop.** A small loop (e.g. `async def _health_poll_loop(interval: float):
   while True: try: await _probe_all_hosts() except Exception: log and continue; await
   asyncio.sleep(interval)`) — a single failing probe or unexpected exception must not kill the
   loop, since that would silently re-introduce the exact bug this plan fixes.
3. **Lifespan wiring** (`src/llm_home_lab/api/app.py`) — `create_app` gains a
   `health_poll_interval: float | None` parameter (`None` disables the background poller entirely,
   for tests that don't want a live task running). `FastAPI(lifespan=...)`: on startup,
   `asyncio.create_task(_health_poll_loop(...))` if enabled; on shutdown, cancel the task and await
   it (swallowing `CancelledError`) so nothing leaks across app instances between tests.
4. **Entry point** (`src/llm_home_lab/main.py`) — `create_default_app` reads
   `ORCHESTRATOR_HEALTH_POLL_INTERVAL_S` (default: a few seconds — short enough that a recovered
   host clears the failure-policy's cooldown/recovery window in a reasonable time, long enough not
   to hammer every registered LM Studio host's health endpoint continuously) and passes it through.
5. **Tests** (`tests/test_health.py` or a new `tests/test_health_poller.py`) — TDD, one behavior at
   a time:
   - A host that starts unhealthy (from a prior failed probe) and then starts responding
     healthily transitions to healthy **without any test client ever calling `/health/ready`** —
     only advancing time and letting the background task run — this is the core regression test
     for the incident that motivated this plan.
   - The loop survives one host's `check_health()` raising (doesn't crash the task, still probes
     other hosts / continues on the next tick).
   - `health_poll_interval=None` means no background task exists (existing tests that construct
     `create_app` without expecting a live task keep passing unaffected).
   - Lifespan starts and cleanly cancels the task (no "Task was destroyed but it is pending"
     warnings on app shutdown).
6. **Verification** — full test suite (`uv run pytest --cov=llm_home_lab`), `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy src`.

## Risks

- **Tests that construct `create_app` directly must not accidentally get a live background task**
  they never asked for and never clean up — default `health_poll_interval` for anything other than
  `main.py`'s real entry point should be `None`/disabled unless a test explicitly opts in, to avoid
  leaking asyncio tasks across the existing test suite.
- **Poll interval tension**: too short adds continuous load to every registered LM Studio host for
  marginal recovery-latency benefit; too long re-introduces a meaningful "stuck offline" window
  after a real recovery. Pick a default and say so explicitly rather than leaving it unstated.
- **A crashing probe must not kill the loop** — one host timing out or raising should not stop
  every other host from being probed on schedule; needs an explicit test, not just a hopeful
  try/except.

## Open Questions

- None currently — `health_score` feeding into routing and per-backend thresholds remain deferred
  per the original spec, unaffected by this plan.
