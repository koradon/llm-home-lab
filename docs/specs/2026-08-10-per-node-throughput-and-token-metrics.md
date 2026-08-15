# Per-node throughput, latency, and token-size metrics

## Status

draft

## Summary

Per-host completion metrics — how many prompts a node has completed, how fast, and how large
those prompts and their completions were — surfaced as five new columns on the TUI Nodes table:
`reqs`, `reqs/min`, `avg_lat_ms`, `prompt_tok`, `compl_tok`. None of this exists per-host today;
`MetricsRegistry` tracks request latency per URL path (not per backend) and only a cumulative
prompt+completion token sum per host, with no per-host completion count, no average latency, and
no min/max token tracking. This spec adds the missing per-host instrumentation and exposes it
through the existing Prometheus → `/metrics` → TUI pipeline used for every other metric today.

## User stories

- As the operator of this orchestrator, I want to see at a glance which node is fastest, which is
  slowest, and how big the prompts each node handles typically are, so I can spot a
  misconfigured or overloaded host without grepping logs.
- As the operator, I want these numbers to reflect real, successful work only, so a node that is
  failing doesn't get its averages skewed by its own errors.

## Requirements

- New `MetricsRegistry.record_host_completion(host_id, latency_ms, prompt_tokens,
  completion_tokens, at)`, called from both completion paths in
  `src/llm_home_lab/api/app.py` — the non-streaming handler (next to its existing
  `record_token_usage(...)` call, ~line 575) and `_stream_chunks` (next to its existing
  `record_token_usage(...)` call, ~line 652) — **only when the completion is healthy**:
  - Non-streaming: `not _is_degenerate_completion(result.content, result.finish_reason)`.
  - Streaming: `saw_content and last_finish_reason == "stop"`.
  A completion that fails this check (or raises `BackendError`) contributes nothing.
- `latency_ms` is the backend-call duration only:
  - Non-streaming: wall time of the `await backend.complete(request)` call.
  - Streaming: wall time of the full `async for chunk in backend.stream(request)` iteration in
    `_stream_chunks`.
  It excludes any time the request spent queued (`SchedulingQueue`) waiting for a free slot before
  a candidate host was chosen.
- Per host, `MetricsRegistry` tracks plain running counters — no sample deque, no rolling window:
  `completions_total`, `latency_ms_total`, `prompt_tokens_total`, `prompt_tokens_min`,
  `prompt_tokens_max`, `completion_tokens_total`, `completion_tokens_min`,
  `completion_tokens_max`. These are cumulative for the life of the orchestrator process, the same
  shape as today's `token_usage_total`.
- `MetricsRegistry.snapshot()` derives, per host: `avg_latency_ms = latency_ms_total /
  completions_total`, `avg_prompt_tokens = prompt_tokens_total / completions_total`,
  `avg_completion_tokens = completion_tokens_total / completions_total`, plus the running
  `prompt_tokens_min/max` and `completion_tokens_min/max` values.
- `MetricsRegistry.render_prometheus()` emits, per host_id label:
  `llm_home_lab_host_completions_total` (counter), `llm_home_lab_host_latency_ms_avg` (gauge),
  `llm_home_lab_host_prompt_tokens_avg`, `llm_home_lab_host_prompt_tokens_min`,
  `llm_home_lab_host_prompt_tokens_max`, `llm_home_lab_host_completion_tokens_avg`,
  `llm_home_lab_host_completion_tokens_min`, `llm_home_lab_host_completion_tokens_max` (gauges) —
  omitted entirely for a host with `completions_total == 0`, the same pattern
  `llm_home_lab_token_usage_total` already uses for hosts with no recorded usage.
- `src/llm_home_lab/diagnostics/metrics_parser.py` gains matching regexes for the new metric
  lines, populating `ParsedMetrics` per host_id.
- The TUI Nodes table (`src/llm_home_lab/tui/app.py`) gains five new columns, in this order after
  `last_seen`: `reqs`, `reqs/min`, `avg_lat_ms`, `prompt_tok`, `compl_tok`.
  - `reqs` is `completions_total` for that host, rendered as-is.
  - `reqs/min` is a live rate, derived by diffing two poll snapshots of `completions_total` —
    generalizing the existing `compute_token_rates` helper (already used for `tokens/s[host_id]`
    in the Queue & Tokens panel) rather than duplicating its poll-diff logic.
  - `prompt_tok` and `compl_tok` are each rendered as a single cell in `avg (min–max)` format,
    e.g. `540 (48–2048)`, to avoid needing four additional columns.

## Behavior

**A host with zero healthy completions renders unavailable, not a divide-by-zero.** A
freshly-registered host, or one that has never completed a healthy request, has
`completions_total == 0`. `avg_lat_ms`, `prompt_tok`, and `compl_tok` render as `"—"` for that
host — the same convention the TUI already uses when a metric is missing (e.g.
`p95_latency_ms` before any request has been recorded). `reqs` and `reqs/min` render as `0`.

**Failed and degenerate completions are invisible to these metrics.** A request that raises
`BackendError`, or completes with empty content or a non-`"stop"` finish reason, does not
increment `completions_total` and does not contribute to any average or min/max — even though it
still feeds `HealthMonitor` and the existing `errors` column via `health_monitor.record_probe`.
A persistently failing host can show a flat or stale `reqs`/`avg_lat_ms` alongside a climbing
`errors` count; that split is intentional, not a bug.

**Metrics are cumulative for the process lifetime, not windowed.** Unlike `availability` and
`p95_latency_ms` (which use a 5-minute rolling window), every metric in this spec accumulates
from orchestrator start and resets only on restart — the same behavior `token_usage_total`
already has. `reqs/min` is the one metric that reflects "right now": it is a rate computed from
the change in `reqs` between two TUI polls, not a stored sample window.

**Min/max only ever move in one direction between restarts.** `prompt_tokens_min/max` and
`completion_tokens_min/max` are running extremes over the host's whole lifetime since the
orchestrator last started — they do not decay or reset while the process keeps running, even if
recent prompts have been smaller/larger.

**Token stats cover both prompt and completion tokens.** `prompt_tok` reflects
`result.prompt_tokens` / `usage["prompt_tokens"]`; `compl_tok` reflects the completion-side
counterpart — both already available at the point `record_token_usage(...)` is called today.

**This is purely additive instrumentation.** No change to `_eligible_candidates`, routing, or
`SchedulingQueue`. Existing metrics (`availability`, `p95_latency_ms`, `host_saturation`,
`queue_depth`, `token_usage_total`) are unaffected.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file:
`docs/specs/features/2026-08-10-per-node-throughput-and-token-metrics.feature`.

## Related

- Spec: [20260717-multi-node-registry-and-scheduler](20260717-multi-node-registry-and-scheduler.md)
  — `HostRegistry`, per-host state this spec adds counters alongside
- ADR: [0009-per-node-throughput-and-token-metrics-design](../adr/0009-per-node-throughput-and-token-metrics-design.md)
  — cumulative-counter design over a rolling-window alternative; healthy-only inclusion rule;
  backend-call-only latency definition; Nodes-table placement
- Module: `src/llm_home_lab/observability/metrics.py` (`MetricsRegistry`)
- Module: `src/llm_home_lab/api/app.py` (`chat_completions`, `_stream_chunks`,
  `_is_degenerate_completion`)
- Module: `src/llm_home_lab/tui/app.py` (Nodes table, `compute_token_rates`)
- Acceptance: `docs/specs/features/2026-08-10-per-node-throughput-and-token-metrics.feature`

## Open Questions

- Whether the Nodes table's resulting width (~150 characters across 12 columns) needs an existing
  column trimmed or reordered — deferred; accepted as horizontal-scroll for now.
- Whether a future consumer needs these per-host stats windowed (matching `availability`/
  `p95_latency_ms`) instead of lifetime-cumulative — out of scope here; revisit if lifetime
  averages prove too smoothed-out to be useful operationally.
