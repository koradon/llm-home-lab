# Background health poller, independent of any client

## Status

accepted

## Context and Problem Statement

`docs/specs/20260717-failover-and-health-policy.md` deliberately left "who calls `record_probe`
and on what cadence — a background poller versus piggy-backing on `/health/ready` calls" as an
app-wiring decision, and the implementation plan explicitly chose the simpler option: probes are
recorded only when something calls `/health/ready`, with no scheduler, and the TUI dashboard was
the intended prober ("the dashboard is exactly the kind of external prober that design expects, so
it drives the cadence itself" — `src/llm_home_lab/tui/client.py:44`). That plan documented the
accepted limitation up front: "a backend that never gets probed... never transitions state."

This surfaced as a real operational problem, not just a theoretical gap: a registered node
(`llm.home`'s Windows host) went offline and came back online while curation traffic
(`service`) was actively running against the orchestrator, with no TUI window open. Because
nothing called `/health/ready` during that window, `HealthMonitor` kept reporting the node as
unhealthy — from an earlier, real failure — for the entire time it was actually healthy and idle,
so it received zero routed requests. `GET /v1/nodes` only reports the last recorded probe; it does
not itself trigger one, so operators have no reliable way to "check on demand" either, short of
manually hitting `/health/ready` themselves.

The TUI dashboard should be a pure, read-only visualizer of orchestrator state — it should not be
a load-bearing component whose absence silently degrades routing correctness.

## Considered Options

- **A. Background poller, kept alongside `/health/ready`'s existing recording.** Add an asyncio
  task (started via a FastAPI lifespan) that loops over `registry.hosts()` on a fixed interval,
  calling the same `check_health()` → `record_probe()` (and `external_load_probe.probe()`) path
  `/health/ready` already uses. `/health/ready` keeps recording too, for on-demand callers (health
  checks, `curl`, CI).
- **B. Move probing exclusively into the background poller; `/health/ready` becomes a pure read of
  current state.** Same poller as A, but the endpoint stops calling `check_health()` itself.
- **C. Do nothing; document that continuous external probing (the TUI, or a cron `curl
  /health/ready`) is a required operational practice.**

## Decision Outcome

Chosen option: **A**, because it fixes the actual defect (health state must never depend on
whether an external client happens to be polling) while keeping `/health/ready`'s existing,
already-tested behavior as a synchronous on-demand check (useful for container healthchecks, CI,
or an operator who wants an immediate answer rather than waiting for the next poll tick) — the two
call sites share one probing routine, so there's no duplicated logic to keep in sync.

**B** was rejected: `/health/ready`'s current 200/503 response *is* a synchronous, on-demand health
check by design (see its own docstring intent and `docs/runbooks/`) — removing its ability to
actually probe would change that endpoint's meaning for callers who rely on calling it directly
(e.g. a container orchestrator's own healthcheck) and forces them to wait for the next background
tick instead of getting a live answer.

**C** was rejected outright — it's the status quo that caused the incident described above; "an
operator must remember to keep a terminal open" is not an acceptable reliability property for
anything driving real traffic.

### Consequences

- Good — health state now updates whether or not the TUI (or anything else) is running; the TUI
  becomes what it should always have been: a read-only view of state the orchestrator maintains on
  its own.
- Good — `/health/ready` is unchanged for existing callers (still synchronously probes and
  records); no breaking change to its documented behavior or existing tests.
- Good — one shared probing routine used by both the poller and `/health/ready` means the failover
  policy's hysteresis (failure/cooldown/recovery thresholds) sees a consistent probe cadence
  instead of being at the mercy of whenever a human happens to look at a dashboard.
- Bad — a new always-running background task per orchestrator process; needs a clean
  startup/shutdown (FastAPI lifespan) so it doesn't leak across app instances in tests, and a
  poll interval that balances recovery latency (per
  `docs/specs/20260717-failover-and-health-policy.md`'s cooldown/recovery thresholds) against load
  on every registered LM Studio host.
- Bad — every registered host now receives probe traffic continuously, not just when something
  happens to ask — a negligible cost for `check_health()`, but a real one if a future backend's
  health check becomes expensive.

## Related

- Spec: `docs/specs/20260717-failover-and-health-policy.md` (resolves its "who calls `record_probe`"
  Open Question)
- Plan: `docs/plans/20260717-failover-and-health-policy.md` (the "accepted limitation" this
  supersedes), `docs/plans/20260720-background-health-poller.md` (implementation)
- Module: `src/llm_home_lab/health/monitor.py`, `src/llm_home_lab/api/app.py` (`/health/ready`)
