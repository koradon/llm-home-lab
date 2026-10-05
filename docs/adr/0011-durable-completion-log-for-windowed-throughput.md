# Durable SQLite completion log for windowed tasks-per-hour

## Status

accepted

## Context

The operator wants tasks processed per hour per host, over a rolling window. The existing
`MetricsRegistry` host counters are cumulative and in-memory only, so they cannot answer "in the
last hour" and reset on restart. No durable per-request completion record existed.

## Decision

We will add `CompletionLog`, a minimal SQLite table (`host_id`, `completed_at`) written at the
same two call sites as `record_host_completion`, so a "task" is the same healthy-completion
condition (failures and degenerate completions are not counted). It reuses the repo's
`SqliteStore` base and lives in its own DB file (`ORCHESTRATOR_COMPLETION_LOG_DB_PATH`) so it
never touches the host-registry schema. Rows past the window are pruned on write, keeping it
small. The window defaults to 1 hour (`ORCHESTRATOR_THROUGHPUT_WINDOW_S`). Slot count comes only
from the probe's `total_slots`; an unknown count yields `n/a`, not a guess.

## Consequences

The rate survives restarts and is exact for the window. Each healthy completion costs one small
SQLite write and each `/v1/nodes` call one count query per host. Only the window's worth of
history is kept, so longer-range analysis would need a longer window or a different store.
