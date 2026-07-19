# Web Management UI

## Status

draft

## Summary

A small, separate FastAPI service (`llm_home_lab_webui`, its own process and port) with a static
HTML/JS frontend — a browser-based, RabbitMQ-management-style view of node health/capacity, firing
alerts, and queue depth/token usage trends over time. It polls the orchestrator's existing
diagnostic endpoints (the same three the TUI uses: `GET /v1/nodes`, `GET /v1/alerts`, `GET
/metrics`) on an interval and persists samples into its own SQLite database — reusing the engine
ADR-0002 already chose, in a separate process and file, not reopening that ADR — so the dashboard
can show short-window historical trends, not just current values. No new frontend build toolchain:
static HTML/CSS/vanilla JS, matching this project's single-maintainer, low-new-dependency appetite
recorded in [operator-observability-dashboards](../ideas/operator-observability-dashboards.md).

## User stories

- As an operator, I want a browser dashboard reachable from any device on my network, so that I'm
  not tied to a terminal session for observability.
- As an operator, I want to see trends (e.g. last 24h) for availability, latency, queue depth, and
  token usage, not just current values, so that I can spot degrading trends before they page me.
- As an operator, I want the web UI to require its own authenticated access, so that dashboard
  access on the network is controlled independently of orchestrator API keys.
- As the maintainer, I want the web UI to be a separate, optional process, so that operators who
  don't want a browser dashboard aren't forced to run it.

## Requirements

- New top-level package `src/llm_home_lab_webui/` — a distinct FastAPI application from the
  orchestrator's, its own `create_webui_app(...)`, own entry point, own default port (`8090`,
  override via `WEBUI_PORT`) — kept separate so it can be deployed independently and so it never
  becomes a dependency of the orchestrator itself.
- `poller.py` — a background asyncio task polling the configured orchestrator
  (`ORCHESTRATOR_BASE_URL`, `ORCHESTRATOR_API_KEY`) on a fixed interval (default 15s,
  `WEBUI_POLL_INTERVAL_S`) for `/v1/nodes`, `/v1/alerts`, and `/metrics` — reuses the TUI's
  `metrics_parser.py` (moved to a shared location both packages import, e.g.
  `src/llm_home_lab/diagnostics/`, rather than duplicating the parser) to extract SLIs from the
  Prometheus scrape.
- `store.py` — `WebUiStore`, a `SqliteStore`-pattern (same direct-`sqlite3`, `PRAGMA
  journal_mode=WAL`, schema-on-init style as `state/sqlite_base.py`) rolling sample store in its
  own DB file (`webui_state.db`, path via `WEBUI_DB_PATH`), one row per poll per metric
  (`timestamp`, `metric_name`, `host_id` nullable, `value`). Retention window configurable
  (`WEBUI_RETENTION_HOURS`, default 24) with a prune step run after each poll — explicitly scoped
  as a short-term trend cache, not a general time-series engine (no downsampling, no long-term
  retention, no query language beyond a metric name + time range).
- `api.py` — the web UI's own read endpoints for its frontend: `GET /api/nodes` (latest polled
  snapshot), `GET /api/alerts` (current + recently-resolved), `GET /api/series?metric=...&since=...`
  (historical points from `store.py` for charting).
- `auth.py` — a session-cookie or Bearer gate in front of `/api/*` and the static frontend, backed
  by its own JSON key file (`config/webui_keys.json`), reusing `ApiKeyStore`'s
  load-from-file-at-startup shape rather than inventing a new format — the web UI's auth is
  independent of, and does not call into, the orchestrator's `ApiKeyStore`.
- Static frontend under `src/llm_home_lab_webui/static/` — plain HTML/CSS/vanilla JS, charts
  rendered with inline `<canvas>` (no charting library dependency), polling `/api/*` on an interval
  from the browser. No new JS package manager or build step.
- `[project.scripts] llm-home-lab-webui = "llm_home_lab_webui.main:run"`; new optional dependency
  group `webui` in `pyproject.toml` (no new runtime deps beyond what FastAPI/uvicorn/httpx already
  bring, since this package reuses them).
- `docker-compose.yml` gains an optional second service for the web UI, per the "both, decide
  later" direction in [deployment-model](../ideas/deployment-model.md) — not required to run the
  orchestrator.

## Behavior

**The web UI only ever reads the orchestrator's diagnostic surface.** It never talks to LM Studio
backends directly and never proxies `/v1/chat/completions` — the same read-only boundary the TUI
spec establishes.

**A poll failure degrades to "stale," not blank.** If the orchestrator is unreachable, `/api/*`
keeps serving the last successfully polled snapshot with a `stale_since` timestamp the frontend
renders as a visible banner, rather than returning an error or an empty dashboard.

**Historical series persist across a web UI restart.** `WebUiStore`'s SQLite file survives a
process restart the same way `state/sqlite_base.py`'s session store does (M2's "state survives
restart" pattern) — only the in-memory poll buffer since the last successful write is at risk, not
the retained history.

**The web UI's auth is independent of the orchestrator's.** A valid orchestrator API key does not
grant access to the web UI and vice versa — they are deliberately separate key files/identities,
since the web UI is a browser-facing surface on the network and the orchestrator's key model
(`allowed_path_prefixes`) is scoped to its own API paths.

**Retention is time-bounded, not size-bounded.** Rows older than `WEBUI_RETENTION_HOURS` are pruned
after each poll; there is no cap on rows within the window — acceptable at the poll interval and
metric cardinality this spec defines (a handful of gauges/counters per host, polled every 15s).

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/features/20260719-web-management-ui.feature`.

## Related

- Idea: [operator-observability-dashboards](../ideas/operator-observability-dashboards.md) — Option
  B, promoted here.
- Spec: [tui-operator-dashboard](20260719-tui-operator-dashboard.md) — shares the same three
  orchestrator endpoints and the `metrics_parser.py` module (relocated to a shared package in the
  plan).
- Spec: [monitoring-slos-and-alerting](20260719-monitoring-slos-and-alerting.md) — `/metrics`,
  `/v1/alerts` this spec polls.
- Spec: [multi-node-registry-and-scheduler](20260717-multi-node-registry-and-scheduler.md) —
  `/v1/nodes` shape.
- Spec: [security-and-governance-baseline](20260717-security-and-governance-baseline.md) — the
  `ApiKeyStore.from_file` shape `auth.py` mirrors.
- Module: [`state/sqlite_base.py`](../../src/llm_home_lab/state/sqlite_base.py) — the
  `SqliteStore` pattern `WebUiStore` follows.
- ADR: [0002-sqlite-for-session-storage](../adr/0002-sqlite-for-session-storage.md) — engine reused
  here for a separate process/file; does not reopen the orchestrator-scoped decision.
- Idea: [deployment-model](../ideas/deployment-model.md) — optional compose service.
- Roadmap: [operator-dashboards](../roadmap/operator-dashboards.md)
- Plan: (to be written) `docs/plans/20260719-web-management-ui.md`
- Issue: (to be created, milestone M6)
- Acceptance: `docs/specs/features/20260719-web-management-ui.feature`

## Open Questions

- Exact retention window and poll interval defaults — proposed (24h / 15s) as starting points, left
  tunable via env var; revisit once real usage shows what's actually worth graphing.
- Whether web UI auth should support more than static API keys (e.g. a simple username/password
  login) given it's browser-facing — static keys proposed as parity with the orchestrator's own
  model and the simplest starting point; revisit if multiple people ever need distinct access.
- Whether this ships in this repo (proposed) or as a separate repo/package — same-repo is proposed
  for single-maintainer simplicity; revisit if the web UI's dependency footprint grows.
