# In-memory completion log for windowed tasks-per-hour

## Status

accepted

## Context

The operator wants tasks processed per hour per host, over a rolling window. The existing
`MetricsRegistry` host counters are cumulative and reset on restart, so they cannot answer "in the
last hour". No per-request completion record existed.

## Decision

We will add `CompletionLog`, an in-memory per-host deque of completion timestamps written at the
same two call sites as `record_host_completion`, so a "task" is the same healthy-completion
condition (failures and degenerate completions are not counted). Entries past the window are
dropped on write, keeping it small. The window defaults to 1 hour
(`ORCHESTRATOR_THROUGHPUT_WINDOW_S`). Slot count comes only from the probe's `total_slots`; an
unknown count yields `n/a`, not a guess. Nothing is persisted: durability was considered and
rejected as not required by the goal, and it would add a second database file and blocking I/O to
the request path.

## Consequences

Recording a completion is a cheap in-memory append that cannot fail the request. The rate is exact
for the window while the orchestrator runs. After a restart the rate divides by the time observed
since start (floored at 60 s, capped at the window), so it is usable immediately. Warming the
window across restarts is a possible follow-up.
