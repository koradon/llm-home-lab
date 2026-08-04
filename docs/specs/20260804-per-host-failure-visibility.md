# Per-Host Failure Visibility

## Status

draft

## Summary

Surfaces `HealthMonitor`'s existing per-host failure history through `GET /v1/nodes` and the
TUI Nodes table, so an operator can identify *which* registered host is degrading and act on it
directly from the dashboard — without grepping logs or guessing from load-sparkline artifacts.
This is Option B of
[completion-quality-health-signal](../ideas/20260727-completion-quality-health-signal.md),
following Option A (shipped in #50), which made `HealthMonitor` react to real completion outcomes
rather than just liveness probes. It reuses `HealthMonitor`'s existing bounded probe history —
no new health-tracking state, no change to routing or the failover state machine (that's the
separate, deliberately-deferred #52).

## User stories

- As an operator, I want to see a per-host recent-failure count in `GET /v1/nodes` and the TUI,
  so that I can identify a degrading host directly from the dashboard instead of reading logs or
  inferring it from jagged load sparklines.
- As an operator monitoring an unattended overnight batch run, I want a host that's been
  intermittently failing (but hasn't yet crossed `failure_threshold`) to be visible before it's
  excluded outright, so that I can investigate or intervene early.
- As an operator, I want this to be purely informational, so that adding it never changes routing
  or failover behavior.

## Requirements

- `HealthMonitor` (`src/llm_home_lab/health/monitor.py`) gains `failure_count(backend_id) -> int`
  — the count of failed probes in the bounded `history` window it already maintains (same window
  `health_score()` reads; default last 20 probes). Returns `0` for a backend with no recorded
  probe history, matching `health_score`'s "healthy by default" convention. No new fields on
  `BackendHealthState` and no change to `record_probe`, `is_healthy`, or the failover state
  machine.
- `GET /v1/nodes` (`src/llm_home_lab/api/app.py`) gains a nested `health` object per host:
  `{"recent_failures": int}`, computed at read time the same way `status` already is (via a
  small helper alongside `_node_status`), not stored. Chosen as a nested object (rather than a
  flat field) to match `external_load`'s existing nested shape and leave room to add further
  health-derived fields later without another top-level key.
- TUI (`src/llm_home_lab/tui/app.py`) Nodes table gains a dedicated `errors` column, rendered
  from `host["health"]["recent_failures"]`: muted/neutral style when `0`, an error-colored badge
  when `> 0` — mirroring the existing `status` column's use of color to draw the eye to a problem
  host, and kept as its own column (not merged into `status`) so it stays independently scannable
  in a wide, mixed-status Nodes table.

## Behavior

**Reflects completion-quality failures, not just liveness.** Since #50, `record_probe` is called
with real completion outcomes (in addition to the background liveness poller), so
`failure_count` counts degenerate completions and probe failures alike — not only host
unreachability.

**Visible before exclusion, not just after.** A host with, say, 2 recent failures out of the last
20 probes shows `recent_failures: 2` even while still under `failure_threshold` and fully
`is_healthy`/`"online"` — this is exactly the "flaky but not yet excluded" case the 2026-07-26/27
incident showed had no diagnostic short of reading logs.

**No probe history yet.** A host that hasn't been probed reports `status: "unknown"` (unchanged
existing behavior) and `health.recent_failures: 0` — consistent with `health_score`'s
zero-history default, not an error state.

**Bounded to the same recency window as `health_score`.** `failure_count` only reflects the most
recent bounded window of probes (default 20) — an old failure ages out exactly when it would stop
contributing to `health_score`, so the two signals never visibly disagree about what's "recent."

**Read-only: no effect on routing or the failover state machine.** `_eligible_candidates` and
`HealthMonitor.is_healthy` are unchanged by this spec. A host's `recent_failures` count has no
bearing on whether it's selected as a routing candidate — that remains #52's explicitly separate,
deferred scope.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file:
`docs/specs/features/20260804-per-host-failure-visibility.feature`.

## Related

- Idea: [completion-quality-health-signal](../ideas/20260727-completion-quality-health-signal.md)
  — Option B, this spec
- Spec: [failover-and-health-policy](20260717-failover-and-health-policy.md) — `HealthMonitor`'s
  existing state machine, `history`, and `health_score()`, all reused as-is
- Spec: [tui-operator-dashboard](20260719-tui-operator-dashboard.md) — Nodes table this spec adds
  an `errors` column to
- Spec: [external-node-load-visibility](20260720-external-node-load-visibility.md) — precedent
  for the nested-object field shape on `GET /v1/nodes` (`external_load`), followed here for
  `health`
- Module: `src/llm_home_lab/health/monitor.py` (`HealthMonitor.failure_count`),
  `src/llm_home_lab/api/app.py` (`list_nodes`, `_node_status`),
  `src/llm_home_lab/tui/app.py` (`_render_nodes`)
- Issue: #51 — surface per-host failure counts in `/v1/nodes` and the TUI (this spec)
- Issue: #50 — feed real completion outcomes into `HealthMonitor` (Option A, shipped; this
  spec's failure counts reflect its signal)
- Issue: #52 — factor failure rate into routing (Option C, optional/lower priority, explicitly
  out of scope here)

## Open Questions

None — design choices (bounded-window count over consecutive-streak, separate TUI column over
merging into `status`, nested `health` object over a flat field) were confirmed during spec
review.
