# Operator Observability Dashboards (Terminal + Web)

## Status

draft

## Problem

An operator running the orchestrator has no fast, at-a-glance way to see which nodes are
registered/healthy/loaded, how full the scheduling queue is, or how many tokens sessions are
consuming. Today the only visibility is reading structured logs (`access`/`health`/`audit`
loggers) or manually polling `GET /v1/nodes`.

## Audience / Value

The home-lab operator (the project's own user), during active use of the orchestrator — wants a
live view comparable to `docker stats`, `htop`, or the RabbitMQ management UI, instead of grepping
logs or polling an endpoint by hand.

## Solution shape (not final)

Two complementary front-ends over the same underlying metrics/diagnostics data (host state, queue
depth, per-session/host token usage):

1. **Terminal dashboard** (nearer-term) — a Python TUI, run locally as a CLI, polling the
   orchestrator's HTTP diagnostics endpoints. This is a client the operator runs against a
   (possibly remote) orchestrator, not a service to containerize.
2. **Web management UI** (longer-term, RabbitMQ-style) — a browser-based dashboard for node/queue/
   token visibility. Needs persisted time-series metrics and its own web app layer.

Both depend on a shared metrics backend — host load, queue depth, token usage per session/host —
extending the existing `/v1/nodes` diagnostics and `llm_home_lab.audit`/`access`/`health` logging
into structured, queryable state. That shared backend piece is expected to land as part of #12
(Add monitoring, SLOs, and alerting).

## Options

- **Option A — TUI only**: smallest scope, immediate value, no new persistence needed if it polls
  live state rather than historical trends.
- **Option B — Web UI only**: bigger scope (time-series store, web app, auth integration),
  remote/multi-viewer friendly.
- **Option C — Both, sequenced**: metrics backend lands as part of #12, TUI follows next as the
  first consumer, web UI is a separate, later initiative.

## Constraints / Appetite

Home-lab scale, single maintainer — favor low-maintenance tooling. A TUI fits the existing Python
stack with no new dependencies beyond a TUI library. A web UI would introduce a new frontend stack
that doesn't currently exist in this project, so it's a materially bigger bet.

## Rabbit holes

- Building a full time-series metrics store (Prometheus-style) before knowing what's actually
  worth graphing.
- Scope creep from "dashboard" into the alerting/SLO system that #12 already owns — keep the two
  distinct.
- TUI approach is unresearched: how existing "beautiful" LLM terminal monitoring tools are built is
  currently unknown and needs investigation (hypothesis: Python's Textual, used by tools like
  `dolphie`/`posting`, but unconfirmed).

## No-gos

- No new persistent database engine choice — this does not reopen ADR-0002 (orchestrator stays
  single-process). The web UI's own historical-sample store (see its spec) reuses SQLite, the
  engine ADR-0002 already chose, as a separate process/DB file — it does not touch the
  orchestrator's storage.
- ~~No commitment to building the web UI in the near term~~ — superseded: both the TUI and the web
  UI have been promoted to spec + plan (see Related), sequenced M5 (TUI) then M6 (web UI). Web UI
  scope stays deliberately small (own process, no new frontend build toolchain, no time-series
  engine beyond SQLite) to keep the original single-maintainer appetite concern addressed rather
  than reopened.

## Related

- Spec: [monitoring-slos-and-alerting](../specs/20260719-monitoring-slos-and-alerting.md) — the
  shared metrics/alerting backend this idea's TUI and web UI would consume, now implemented
- Plan: [monitoring-slos-and-alerting](../plans/20260719-monitoring-slos-and-alerting.md)
- Spec: [tui-operator-dashboard](../specs/20260719-tui-operator-dashboard.md) — Option A, promoted
- Plan: [tui-operator-dashboard](../plans/20260719-tui-operator-dashboard.md)
- Spec: [web-management-ui](../specs/20260719-web-management-ui.md) — Option B, promoted
- Plan: [web-management-ui](../plans/20260719-web-management-ui.md)
- Roadmap: [operator-dashboards](../roadmap/operator-dashboards.md)
- Issue: #12 — Add monitoring, SLOs, and alerting (milestone M4) — done; this idea's TUI/web-UI
  front ends are now milestones M5/M6
- ADR-0002 — SQLite vs PostgreSQL revisit trigger; not applicable here, orchestrator stays
  single-process

## Open Questions

- What library/pattern do other "beautiful" LLM terminal monitoring tools actually use? Textual is
  proposed in the TUI spec as the starting choice; not independently researched against
  alternatives.
- What is the minimal metrics backend needed for the TUI, versus what the web UI additionally
  needs (time-series storage, retention)? Resolved in the two specs: TUI parses `/metrics` live
  with no storage; web UI adds its own small SQLite rolling-sample store.
- ~~Should the web UI be scoped as its own separate idea/spec~~ — resolved: yes, see
  [web-management-ui](../specs/20260719-web-management-ui.md).
