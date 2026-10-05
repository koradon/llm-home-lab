# Per-host task throughput and busy/total slot display

## Status

draft

## Summary

The TUI's `ext_load` column rendered `N queued` for llama-server hosts, but there `queued` was
only the count of busy slots (7 of 8 slots processing is not a backlog of 7). This spec replaces
that label with `busy (7/8 slots)`, and adds two hardware-performance columns next to it:
`tasks/h` (tasks processed per hour) and `tasks/h/slot` (that rate divided by the host's slot
count). Both are exposed on `GET /v1/nodes`.

## User stories

- As the operator, I want the load column to say how many of a host's slots are busy, so I am not
  misled into reading a busy slot as a queued request.
- As the operator, I want tasks per hour, and tasks per hour per slot, for each host, so I can
  judge how well each machine performs relative to its parallelism.

## Requirements

- `ExternalLoadStatus` gains `total_slots` and `busy_slots` (both `None` by default).
  `LlamaCPPServerLoadProbe` sets them to the `/slots` list length and the `is_processing` count;
  `ExternalLoadProbe` (LM Studio) leaves them `None`. `queued` is unchanged (backward compatible).
- `GET /v1/nodes` `external_load` adds `total_slots` and `busy_slots`.
- TUI `ext_load`: when `total_slots` is known and non-zero, render `<status> (<busy>/<total>
  slots)` (just `idle` when nothing is busy); otherwise keep `<status> (N queued)` — true queue
  depth for LM Studio.
- **A processed task** is one healthy completion: a non-streaming result that is not degenerate
  (non-empty content, `finish_reason == "stop"`), or a stream that produced content and ended
  with `finish_reason == "stop"` — the same condition as the existing
  `MetricsRegistry.record_host_completion`. A `BackendError`, an empty or truncated completion,
  and a stream without a clean stop are **not** counted. A streaming completion counts even if
  the backend reported no usage.
- Completions are recorded in a durable `CompletionLog` (SQLite table `host_completions`:
  `host_id`, `completed_at` in UTC), at the same call sites as `record_host_completion`.
- **Window:** the trailing `window` (default 1 hour, `ORCHESTRATOR_THROUGHPUT_WINDOW_S`) ending
  at the query time, half-open: a completion at exactly `now - window` is out, one at `now` is in.
  Rows older than the window are pruned on write.
- `tasks_per_hour = count_in_window * 1h / window`.
- `tasks_per_hour_per_slot = tasks_per_hour / total_slots`, or `null` (rendered `n/a`) when
  `total_slots` is unknown (`None`) or zero.
- `GET /v1/nodes` adds, per node, `throughput: {window_s, tasks_per_hour,
  tasks_per_hour_per_slot}`. The TUI renders `tasks/h` and `tasks/h/slot` after `ext_load`, one
  decimal; a missing value renders `n/a`.

## Behavior

**No completions in the window** gives `tasks_per_hour == 0.0` and, with a known slot count,
`tasks_per_hour_per_slot == 0.0` — never a division by zero. The only divisor is `total_slots`,
and it is guarded.

**Unknown slot count is not guessed.** LM Studio hosts report no slot pool, so their per-slot
rate is `n/a`; `max_concurrent_requests` is a routing cap, not a slot count, and is not
substituted.

**The rate survives restarts** because the log is on disk; a freshly restarted orchestrator
reports the last window's completions straight away. A window longer than the log's history is
still divided by the full window, so a new host's rate is a lower bound until the window fills.

## Acceptance scenarios (BDD)

`docs/specs/features/20260810-per-host-task-throughput.feature`.

## Related

- Spec: [llamaserver-backend-adapter](20260804-llamaserver-backend-adapter.md) — load probe
  whose open question this resolves
- Spec: [per-node-throughput-and-token-metrics](2026-08-10-per-node-throughput-and-token-metrics.md)
  — the cumulative in-memory counters; this spec adds the durable windowed rate beside them
- ADR: [0011-durable-completion-log-for-windowed-throughput](../adr/0011-durable-completion-log-for-windowed-throughput.md)
- Module: `src/llm_home_lab/observability/completion_log.py`

## Open Questions

- Follow-up: per-slot rate for LM Studio hosts, if a slot-like count becomes available.
- Follow-up: windowed token throughput (tokens per hour) from the same log.
