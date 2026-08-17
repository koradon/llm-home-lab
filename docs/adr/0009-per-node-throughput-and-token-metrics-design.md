# Cumulative per-host counters, healthy-only inclusion, backend-call latency, and Nodes-table placement for per-node metrics

## Status

accepted

## Context and Problem Statement

`docs/specs/2026-08-10-per-node-throughput-and-token-metrics.md` adds per-host throughput,
latency, and prompt/completion token-size metrics to the TUI. None of the underlying data exists
per-host today: `MetricsRegistry.record_request(endpoint, status_code, latency_ms, at)` is keyed
by URL path, not by backend host, so today's `p95_latency_ms` is orchestrator-wide;
`record_token_usage(host_id, prompt_tokens, completion_tokens, at)` is per-host but only
accumulates a combined running total, with no completion count, no average latency, and no
min/max. Four design questions had real alternatives and were resolved with the user via a Lavish
plan review before this ADR was written:

1. Should these metrics decay over a rolling window (like `availability`/`p95_latency_ms`), or
   accumulate for the process lifetime (like `token_usage_total`)?
2. Should a failed or degenerate completion attempt count toward a host's stats?
3. Does "time to handle a prompt" include time a request spent queued for a free slot, or only
   the backend's own processing time?
4. Do these show up as new rows in the existing Queue & Tokens panel (matching the
   `tokens[host_id]`/`tokens/s[host_id]` pattern), new columns on the Nodes table, or a new panel?

## Considered Options

**1. Aggregation shape**
- A — Rolling 5-minute sample window per host (a `deque` of `(latency_ms, tokens, at)` per host,
  evicted the same way `_requests` already is), matching `availability`/`p95_latency_ms`.
- B — Plain cumulative counters per host (`completions_total`, `latency_ms_total`,
  `prompt_tokens_total`, running min/max, etc.), with a live rate derived by diffing two poll
  snapshots — the same technique the TUI already uses to turn cumulative `token_usage_total` into
  `tokens/s[host_id]`.

**2. Which completions count**
- A — Every completion attempt, including `BackendError`s and degenerate completions (empty
  content or a non-`"stop"` finish reason).
- B — Only completions that pass the same healthy/degenerate check each completion path already
  applies before calling `health_monitor.record_probe(healthy=...)` (see
  [ADR-0006](0006-background-health-poller.md) for that health model).

**3. Latency definition**
- A — Full request duration from arrival, including time spent in `SchedulingQueue` waiting for a
  free slot.
- B — Backend-call duration only — wall time of `backend.complete(request)` (non-streaming) or the
  full `_stream_chunks` iteration (streaming).

**4. TUI placement**
- A — New rows in the existing Queue & Tokens panel, alongside `tokens[host_id]`/
  `tokens/s[host_id]`.
- B — New columns on the Nodes table.
- C — A new dedicated panel.

## Decision Outcome

**1B — cumulative counters, not a rolling window.** A rolling window would need a per-host sample
deque — more state than any other per-host metric carries today. Cumulative counters (`+=` on a
handful of `int`/`float` fields per host) give the same "live rate" the user wants for free, via
the poll-diff technique already proven for `tokens/s`, with materially less new state in
`MetricsRegistry`.

**2B — only healthy completions count.** Counting every attempt would let a struggling host's own
timeouts and errors distort its `avg_latency_ms` and token-size averages. The existing `errors`
column on the Nodes table already surfaces failure counts, so excluding failures from these new
metrics doesn't lose visibility — it keeps "how fast/big" and "how often it fails" as two separate
signals instead of conflating them into one skewed number.

**3B — backend-call duration only.** Including queue wait would conflate "this node is slow" with
"this node was saturated and the request had to wait for a slot" — two different operational
problems. The existing global `p95_latency_ms` and `queue_depth` metrics already partly cover
end-to-end and queueing latency; this new per-host metric is deliberately scoped to be a property
of the node itself.

**4B — new columns on the Nodes table**, overriding the option initially recommended during
review (4A, matching the existing per-host row pattern in the Queue & Tokens panel). The user
chose to keep everything about one node in a single row, accepting that the Nodes table grows to
roughly 150 characters across 12 columns and needs horizontal scroll in a typical terminal, rather
than trimming or relocating an existing column.

### Consequences

- Good, because no new sample-eviction logic is needed anywhere in `MetricsRegistry` — every new
  per-host field is a running counter or running min/max, the same shape `token_usage_total`
  already has.
- Good, because the live-rate half of the aggregation decision (1B) reuses
  `compute_token_rates`'s poll-diff technique instead of introducing a second way to compute a
  rate in the TUI.
- Good, because excluding failed/degenerate completions (2B) keeps this feature's numbers
  meaningful for a flaky host instead of silently averaging failures into "how fast is this node."
- Bad, because cumulative counters (1B) mean `avg_latency_ms` and the token min/max only ever
  reflect a host's whole lifetime since the orchestrator last restarted — a host that was slow for
  an hour and fast ever since still shows a blended average, with no way to see "how is it doing
  right now" the way the rolling-window metrics can. Revisit if this proves too smoothed-out to be
  operationally useful.
- Bad, because the Nodes table (4B) is now wide enough to require horizontal scroll in most
  terminals — a real usability cost accepted deliberately rather than by omission.
- Neutral: this does not change routing, `_eligible_candidates`, or `SchedulingQueue` — it is
  read-only instrumentation of the two existing completion paths.

## Related

- Spec: [2026-08-10-per-node-throughput-and-token-metrics](../specs/2026-08-10-per-node-throughput-and-token-metrics.md)
- ADR: [0006-background-health-poller](0006-background-health-poller.md) — the healthy/degenerate
  completion test this ADR's decision 2 reuses rather than re-deriving
- ADR: [0008-dedicated-capacity-summary-endpoint](0008-dedicated-capacity-summary-endpoint.md) —
  prior precedent of reusing an existing helper/pattern instead of parallel logic
- Module: `src/llm_home_lab/observability/metrics.py` (`MetricsRegistry`)
- Module: `src/llm_home_lab/tui/app.py` (Nodes table, `compute_token_rates`)
