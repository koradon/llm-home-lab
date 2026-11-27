# Per-model observability and TUI breakdown under node status

## Status

accepted

## Context and Problem Statement

Nodes running `llama-server` in router mode (or multi-model nodes with `model_limits`, ADR-0015 / ADR-0016) can serve multiple models concurrently with different slot limits (for example `gemma-e4b` with 8 slots and `eurollm-22b` with 2 slots). Previously, the TUI displayed a trailing `models` column at the far right of the Nodes table, while `in_flight`, `throughput`, and completion metrics (`llm_home_lab_host_*`) were only tracked at the host level. Operators could not see how many slots each model currently occupied or which model caused load or errors on a multi-model node.

We need per-model live occupancy, throughput, and completion statistics in both the Nodes table (as child rows starting at the `status` column beneath each node's summary row, removing the trailing `models` column) and compact per-model sub-charts in the Node Load panel, without restructuring `SchedulingQueue` or breaking existing API payloads and Prometheus metrics.

## Decision Outcome

Chosen approach: **Purely additive per-model observability counters + parent/child TUI breakdown**.

### Backend and metrics (strictly additive)

1. **Per-model `in_flight`**: `HostInfo` adds `in_flight_by_model: dict[str, int]` (default empty), updated via an optional `model: str | None = None` parameter on `HostRegistry.acquire_slot`, `release_slot`, and `release_owned_slot`. `GET /v1/nodes` exposes `"in_flight"` and `"max_concurrent_requests"` on each entry in `nodes[].models[]`, plus `"in_flight_by_model"` on each node.
2. **Per-model windowed throughput**: `CompletionLog.record(host_id, at, model=None)` records an additive `(host_id, model)` timestamp deque alongside the existing `host_id` deque. `GET /v1/nodes` exposes `"throughput"` (`tasks_per_hour`, `tasks_per_hour_per_slot`) on each entry in `nodes[].models[]` and `"model_throughput"` on each node.
3. **Per-model completion metrics**: `MetricsRegistry.record_host_completion(..., model=None)` records parallel `(host_id, model)` accumulators and emits additive `llm_home_lab_host_model_*{host_id="...",model="..."}` Prometheus series (`completions_total`, `latency_ms_avg`, `prompt_tokens_{avg,min,max}`, `completion_tokens_{avg,min,max}`). All existing `llm_home_lab_host_*` series remain unchanged. `parse_metrics_text` parses them into parallel `host_model_*` dictionaries on `ParsedMetrics`.

### TUI layout and semantics

1. **Two distinct status layers**: Row 1 (`host_id` filled) represents node reachability (`online`/`offline`/`unknown`) and aggregated counters. Model work health (`loaded`, `loading`, `sleeping`, `failed`, per-model errors) lives in child rows 2..N+1 starting at the `status` column (`host_id`, `backend_type`, and `last_seen` left empty) with tree prefixes (`├─` / `└─`). A single failed model never marks a reachable node `offline`.
2. **Slot denominator and error reporting**: When a router node has 2+ healthy `loaded` models, Row 1 sums their slot limits (and excludes `failed`/unhealthy models from capacity). Row 1 `errors` sums per-model `recent_failures` plus unattributed node/transport `recent_failures`, explicitly labelling unattributed node errors as `N (K node)` when `K > 0`.
3. **Child-row visibility**: Router nodes show child rows for all non-`unloaded` models (and `unloaded` models only when they have nonzero activity/errors); single healthy `loaded` models and plain 0/1-model nodes stay on one row. Non-router nodes with 2+ `model_limits` entries render summary + per-model child rows.
4. **Compact per-model `Node Load` sub-charts**: Multi-row nodes also display indented 2-row `LoadChart` bars for active models underneath their 4-row node bar, toggled with `m`, reusing the same per-model `in_flight` and slot limits.

### Consequences

- Good: zero breaking changes to existing `/v1/nodes` fields, `/metrics` series, or `SchedulingQueue`.
- Good: operators immediately see per-model slot saturation, throughput, latency, tokens, and isolated model failures inline under `status` and in `Node Load`.
- Good: selecting any child model row targets its parent node for edit (`e`), remove (`x`), and reorder (`u`/`d`) actions, and sorting keeps child rows attached to their node.

## Related

- Issue: https://github.com/koradon/llm-home-lab/issues/91
- ADR: [0015-per-model-node-limits](0015-per-model-node-limits.md)
- ADR: [0016-llamaserver-router-mode-awareness](0016-llamaserver-router-mode-awareness.md)
- Spec: [tui-operator-dashboard](../specs/20260719-tui-operator-dashboard.md)
