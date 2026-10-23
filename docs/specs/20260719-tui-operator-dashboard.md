# TUI Operator Dashboard

## Status

draft

## Summary

A Textual-based terminal client, shipped as its own console script in this repo, that polls a
running orchestrator's existing diagnostic endpoints (`GET /v1/nodes`, `GET /metrics`) and renders
live node health/capacity, completion metrics, and node load —
comparable to `docker stats` or `htop`, that the operator runs against a (possibly remote)
orchestrator. It also registers, edits, and deregisters nodes (see "The TUI is no longer strictly
read-only" below); either way it introduces no new orchestrator endpoints and no persistence of
its own, matching the "TUI first, no new backend surface" appetite recorded in
[operator-observability-dashboards](../ideas/operator-observability-dashboards.md).

## User stories

- As an operator, I want to see registered nodes, their health, and their load at a glance in my
  terminal, so that I don't have to poll `/v1/nodes` by hand or grep logs.
- As an operator running the TUI against a remote orchestrator, I want to authenticate the same way
  any other client does (Bearer key), so observability access doesn't bypass the existing security
  baseline.

## Requirements

- New package `src/llm_home_lab/tui/` (`__init__.py`, `client.py`, `metrics_parser.py`, `app.py`),
  sibling to `observability/`, `registry/`, etc. — reuses this repo's existing `httpx` dependency
  rather than adding a new HTTP client library.
- `client.py` — `OrchestratorDiagnosticsClient(base_url: str, api_key: str)`, an async `httpx`
  wrapper originally scoped to `list_nodes()`, `list_alerts()`, `fetch_metrics_text()` — one call
  per existing GET endpoint — and since extended with `register_node()`, `update_node()`,
  `deregister_node()` as the Nodes panel grew mutation support.
  `Authorization: Bearer <api_key>` header on each, matching how any other client authenticates
  against `enforce_auth`.
- `metrics_parser.py` — a minimal Prometheus text-exposition parser that extracts only the metric
  names this repo's `MetricsRegistry.render_prometheus` actually emits (`llm_home_lab_queue_depth`,
  `llm_home_lab_token_usage_total{host_id="..."}`, plus the three windowed SLIs) — not a general
  Prometheus client library dependency, since the wire format this repo produces is small and
  hand-rolled already (see the monitoring spec).
- `app.py` — a Textual `App` with a fixed-interval polling loop (default 2s, `--interval`) driving
  two panels:
  - **Nodes** — `host_id`, `backend_type`, `status`, `last_seen`, one row per host from `list_nodes()`. A toolbar above the table (`+ New Node` / `✕ Remove Node`
    buttons, plus `n`/`x`/`e` keybindings) opens `NodeFormScreen` to register a new node, edit the
    row currently selected via cursor, or deregister it after a confirm dialog. The form includes
    `model_sizes_gb` and `model_aliases`, each edited as repeating name/value input rows, not
    free text. Display order is managed separately (see **Node order** below).
  - **Node Load** — one green sparkline per node, charting
    `in_flight / max_concurrent_requests`. Full utilization is the desired lab state.
    There is no own/external split or external-load column in Nodes.
  - The `status` cell carries the load counter after the styled status word, e.g. `online 8/8` or
    `offline 0/5` (`in_flight`/`max_concurrent_requests`). The counter is bold yellow while the node
    is busy (`in_flight` above 0), unstyled when idle, and dim when the node is offline or unknown;
    there is no separate in-flight/capacity column. Sorting by `status` orders by the status word.
  - Alerts and Queue & Tokens are removed entirely, with no replacement panel or folded-in
    queue/token summary. The existing Nodes completion metrics remain.
- CLI flags / env vars: `--base-url`/`ORCHESTRATOR_BASE_URL` (default `http://localhost:8080`),
  `--api-key`/`ORCHESTRATOR_API_KEY` (required, no insecure default — matches this repo's
  auth-required-by-default posture), `--interval`.
- `textual`, plus `rich` because `tui/app.py` imports `rich.text.Text` directly, are regular
  dependencies in `pyproject.toml` `[project].dependencies` — the TUI is not optional.
- New entry point: `[project.scripts] llm-home-lab-tui = "llm_home_lab.tui.app:run"`.
- Operators wanting to use the TUI add a client entry to `config/api_keys.json` scoped via
  `allowed_path_prefixes: ["/v1/nodes"]` (`/metrics` is already auth-exempt) — no
  change to `ApiKeyStore`/`ClientConfig` needed, this is purely a new client entry.

## Behavior

**Node order.** The Nodes table and the Node Load sparklines share one displayed order. By default
it is the operator's manual order: an optional integer `position` on the node record (stored with
the node's capabilities, exposed by `GET /v1/nodes`, settable via `POST /v1/nodes/register` and
`PATCH /v1/nodes/{host_id}`). `/v1/nodes` lists positioned nodes first by `position`, then
unpositioned ones in registration order, so a new node lands last and records stored before the
field existed keep working. `u`/`d` move the selected node up/down (the TUI PATCHes the new
positions). `s` cycles a session-only sort column (node, status, reqs/min, avg latency, tokens;
rows without a value sort last), `r` toggles direction, `o` clears the sort; the column sort is
never persisted and moving is disabled while one is active.

**Polling is fixed-interval, not event-driven.** No websocket/SSE surface is added to the
orchestrator for this — matches home-lab scale and keeps the orchestrator's API contract unchanged.

Dashboard refreshes call only `GET /v1/nodes` and `GET /metrics`, never `/v1/alerts` or
`/health/ready`. Metrics remain necessary for the Nodes completion columns.
Host health comes from the background poller described in
[ADR-0006](../adr/0006-background-health-poller.md). With polling disabled, health stays
unknown until an on-demand readiness check records it.

**The TUI is no longer strictly read-only.** The original design limited it to the three GET
diagnostic endpoints; it has since grown a Nodes panel that also edits (`PATCH`), registers
(`POST /v1/nodes/register`), and deregisters (`DELETE`) nodes, gated by explicit user action
(a modal + confirm dialog for deregistration) rather than anything automatic. See
[per-alias-completion-metrics](20260730-per-alias-completion-metrics.md) for context on one field
(`model_aliases`) this mutation surface edits.

**A failed poll shows an inline error, not a crash.** Connection errors (including connect
timeouts) show "cannot reach orchestrator, retrying"; other transport timeouts show
"orchestrator slow to respond, retrying". `401`/`403` (bad or missing key) and `5xx` responses
also produce status banners. The client keeps its 5-second default timeout, and the next
scheduled poll retries automatically. The TUI never exits on a single failed poll.

**Missing/expired API key produces a clear auth-error state**, not a silent hang — the first failed
poll due to `401` renders a distinct "not authorized" banner instead of the generic
connection-error banner, so an operator immediately knows to check their key rather than suspect a
network issue.

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
- Spec: [web-management-ui](20260719-web-management-ui.md) — shares diagnostics client and
  metrics parser components.
- Spec: [per-alias-completion-metrics](20260730-per-alias-completion-metrics.md) — alias-level
  backend metrics; TUI rendering is out of scope.
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
- Distribution: shipped inside this repo as part of the core package (decided) vs. a separate
  installable package — revisit if operators want the TUI without installing the orchestrator
  itself.
