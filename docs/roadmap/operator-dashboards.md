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

- Related: [operator-observability-dashboards](../ideas/operator-observability-dashboards.md)
  (Option A/C)
- Confidence: medium
- Notes: nearer-term, no new frontend stack — a Python TUI client polling the orchestrator's
  existing diagnostics endpoints. Library choice (likely Textual) is an open question in the
  idea doc.

### Web management UI

- Related: [operator-observability-dashboards](../ideas/operator-observability-dashboards.md)
  (Option B/C)
- Confidence: low — explicit no-go for near-term commitment in the idea doc (new frontend stack,
  time-series storage, auth integration)
- Notes: revisit once the TUI and shared metrics backend are in use; do not start without
  re-evaluating appetite first.
