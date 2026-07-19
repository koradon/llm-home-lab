# TUI Operator Dashboard

## Status

draft

## Summary

A Textual-based terminal client, shipped as its own console script in this repo, that polls a
running orchestrator's existing diagnostic endpoints (`GET /v1/nodes`, `GET /v1/alerts`, `GET
/metrics`) and renders live node health/capacity, firing alerts, and queue depth/token usage —
comparable to `docker stats` or `htop`. It is a read-only client the operator runs against a
(possibly remote) orchestrator; it introduces no new orchestrator endpoints and no persistence of
its own, matching the "TUI first, no new backend surface" appetite recorded in
[operator-observability-dashboards](../ideas/operator-observability-dashboards.md).

## User stories

- As an operator, I want to see registered nodes, their health, and their load at a glance in my
  terminal, so that I don't have to poll `/v1/nodes` by hand or grep logs.
- As an operator, I want to see currently firing alerts with severity and runbook link, so that I
  notice issues without watching the `llm_home_lab.alerts` logger directly.
- As an operator, I want to see queue depth and per-host token usage, so that I understand load and
  cost without standing up a full Prometheus/Grafana stack.
- As an operator running the TUI against a remote orchestrator, I want to authenticate the same way
  any other client does (Bearer key), so observability access doesn't bypass the existing security
  baseline.

## Requirements

- New package `src/llm_home_lab/tui/` (`__init__.py`, `client.py`, `metrics_parser.py`, `app.py`),
  sibling to `observability/`, `registry/`, etc. — reuses this repo's existing `httpx` dependency
  rather than adding a new HTTP client library.
- `client.py` — `OrchestratorDiagnosticsClient(base_url: str, api_key: str)`, an async `httpx`
  wrapper with three methods: `list_nodes()`, `list_alerts()`, `fetch_metrics_text()` — one call per
  existing endpoint, `Authorization: Bearer <api_key>` header on each, matching how any other client
  authenticates against `enforce_auth`.
- `metrics_parser.py` — a minimal Prometheus text-exposition parser that extracts only the metric
  names this repo's `MetricsRegistry.render_prometheus` actually emits (`llm_home_lab_queue_depth`,
  `llm_home_lab_token_usage_total{host_id="..."}`, plus the three windowed SLIs) — not a general
  Prometheus client library dependency, since the wire format this repo produces is small and
  hand-rolled already (see the monitoring spec).
- `app.py` — a Textual `App` with a fixed-interval polling loop (default 2s, `--interval`) driving
  three panels:
  - **Nodes** — `host_id`, `backend_type`, `in_flight`/`max_concurrent_requests`, `last_seen`, one
    row per host from `list_nodes()`.
  - **Alerts** — `rule_name`, `severity`, `state`, `value`/`threshold_value`, `runbook_url`, one row
    per currently-firing alert from `list_alerts()`.
  - **Queue & Tokens** — queue depth and per-host cumulative token usage, parsed from
    `fetch_metrics_text()`.
- CLI flags / env vars: `--base-url`/`ORCHESTRATOR_BASE_URL` (default `http://localhost:8080`),
  `--api-key`/`ORCHESTRATOR_API_KEY` (required, no insecure default — matches this repo's
  auth-required-by-default posture), `--interval`.
- New optional dependency group `tui` (`textual`) in `pyproject.toml` — not a core dependency, since
  running the orchestrator itself never requires a terminal UI.
- New entry point: `[project.scripts] llm-home-lab-tui = "llm_home_lab.tui.app:run"`.
- Operators wanting to use the TUI add a client entry to `config/api_keys.json` scoped via
  `allowed_path_prefixes: ["/v1/nodes", "/v1/alerts"]` (`/metrics` is already auth-exempt) — no
  change to `ApiKeyStore`/`ClientConfig` needed, this is purely a new client entry.

## Behavior

**Polling is fixed-interval, not event-driven.** No websocket/SSE surface is added to the
orchestrator for this — matches home-lab scale and keeps the orchestrator's API contract unchanged.

**The TUI is strictly read-only.** It has no path to node registration/deregistration or any
config-changing endpoint; scope is limited to the three GET diagnostic endpoints, matching the
idea doc's intent to keep observability separate from operational mutation.

**A failed poll shows an inline error, not a crash.** Connection errors, `401`/`403` (bad or
missing key), and `5xx` responses are rendered as a status banner in the running TUI; the next
scheduled poll retries automatically. The TUI never exits on a single failed poll.

**Missing/expired API key produces a clear auth-error state**, not a silent hang — the first failed
poll due to `401` renders a distinct "not authorized" banner instead of the generic
connection-error banner, so an operator immediately knows to check their key rather than suspect a
network issue.

**The metrics parser fails visibly, not silently, on unexpected `/metrics` output.** If an expected
metric line is absent from a scrape (e.g. a future change to `MetricsRegistry` renames or removes
it), the Queue & Tokens panel shows "unavailable" for that row rather than a stale or fabricated
value.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file:
`docs/specs/features/20260719-tui-operator-dashboard.feature`.

## Related

- Idea: [operator-observability-dashboards](../ideas/operator-observability-dashboards.md) — Option
  A, promoted here.
- Spec: [monitoring-slos-and-alerting](20260719-monitoring-slos-and-alerting.md) — `/metrics`,
  `/v1/alerts`, and the Prometheus metric names this spec's parser depends on.
- Spec: [multi-node-registry-and-scheduler](20260717-multi-node-registry-and-scheduler.md) —
  `/v1/nodes` shape.
- Spec: [security-and-governance-baseline](20260717-security-and-governance-baseline.md) — Bearer
  auth and `allowed_path_prefixes` this spec's client entry follows.
- Spec: [web-management-ui](20260719-web-management-ui.md) — shares the same three orchestrator
  endpoints and, in the plan, the same `metrics_parser.py` module.
- Roadmap: [operator-dashboards](../roadmap/operator-dashboards.md)
- Plan: (to be written) `docs/plans/20260719-tui-operator-dashboard.md`
- Issue: (to be created, milestone M5)
- Acceptance: `docs/specs/features/20260719-tui-operator-dashboard.feature`

## Open Questions

- Exact Textual widget/layout choice (`DataTable` per panel vs. a single combined view) — left to
  the plan.
- Whether queue depth/token usage eventually deserve a small dedicated JSON diagnostics endpoint
  instead of parsing Prometheus text — deferred; parsing text is proposed as the simpler starting
  point since it needs no orchestrator change.
- Distribution: shipped inside this repo as an optional dependency group (proposed) vs. a separate
  installable package — revisit if operators want the TUI without installing the orchestrator
  itself.
