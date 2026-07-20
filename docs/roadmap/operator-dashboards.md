# Operator Dashboards

## Status

draft

## Horizon

Next (TUI) / Later (Web UI)

## Summary

Front-ends over the M4 monitoring/metrics backend (node health, queue depth, per-session/host
token usage) so the operator doesn't have to read structured logs or poll `/v1/nodes` by hand.
Two initiatives, sequenced per the appetite recorded in
[operator-observability-dashboards](../ideas/operator-observability-dashboards.md).

## Items

### Terminal (TUI) operator dashboard

- Related: [tui-operator-dashboard](../specs/20260719-tui-operator-dashboard.md)
- Confidence: shipped (M5)
- Notes: built as a Textual client polling the orchestrator's diagnostics endpoints; live in real
  two-node (Mac + Windows) use.

### External node load visibility

- Related: [external-node-load-visibility](../specs/20260720-external-node-load-visibility.md),
  [ADR-0005](../adr/0005-lms-cli-for-external-node-load-visibility.md)
- Confidence: medium — speced and planned, not yet implemented
- Notes: extends the TUI Nodes panel with load caused by something other than this orchestrator,
  via LM Studio's `lms ps --host` CLI (its REST API has no such endpoint). This project's first
  subprocess/external-binary dependency; degrades gracefully if `lms` is missing.

### Web management UI

- Related: [operator-observability-dashboards](../ideas/operator-observability-dashboards.md)
  (Option B/C)
- Confidence: low — explicit no-go for near-term commitment in the idea doc (new frontend stack,
  time-series storage, auth integration)
- Notes: revisit once the TUI and shared metrics backend are in use; do not start without
  re-evaluating appetite first.
