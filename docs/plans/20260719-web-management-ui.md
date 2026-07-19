# Web Management UI Plan

## Status

draft

## Related

- Spec: [web-management-ui](../specs/20260719-web-management-ui.md)
- Idea: [operator-observability-dashboards](../ideas/operator-observability-dashboards.md)
- Depends on: [tui-operator-dashboard plan](20260719-tui-operator-dashboard.md) (`diagnostics/
  metrics_parser.py`, built there, imported here unchanged)
- Depends on: [`state/sqlite_base.py`](../../src/llm_home_lab/state/sqlite_base.py) (`SqliteStore`
  pattern `WebUiStore` follows)
- Depends on: [`security/key_store.py`](../../src/llm_home_lab/security/key_store.py)
  (`ApiKeyStore.from_file` shape `WebUiKeyStore` mirrors)
- Issue: (to be created, milestone M6)

## Scope

Build a new top-level package `src/llm_home_lab_webui/` (poller, SQLite-backed rolling store, API,
auth, static frontend), its own optional dependency group and entry point, and an optional
`docker-compose.yml` service. Requires the M5 TUI plan's `diagnostics/metrics_parser.py` to already
exist.

Concrete decisions for this plan (resolving the spec's deferred questions):

- **Poll interval / retention defaults**: `WEBUI_POLL_INTERVAL_S=15`, `WEBUI_RETENTION_HOURS=24` —
  proposed in the spec, fixed here as the shipped defaults, both overridable via env var (matching
  this codebase's fixed-with-override convention).
- **Auth model**: static Bearer keys via a new `config/webui_keys.json`, structurally identical to
  `config/api_keys.json` (`clients: [{client_id, keys: [{key, expires_at}]}]`) but a distinct file
  and a distinct `WebUiKeyStore` class — no `allowed_path_prefixes` needed (the web UI has no path
  segmentation to enforce, unlike the orchestrator's multi-client model), just a bearer-token
  presence/expiry check.
- **Sample schema**: a single `samples` table (`ts INTEGER, metric_name TEXT, host_id TEXT NULL,
  value REAL`), one row per metric per poll — simplest schema that supports
  `GET /api/series?metric=...&since=...` with a plain `WHERE metric_name = ? AND ts >= ?` query;
  explicitly not normalized into per-metric tables, since cardinality is small (a handful of
  metrics x host count, every 15s).

Out of scope for this plan (per the spec's Open Questions):

- Any login flow beyond static Bearer keys (no username/password).
- Downsampling or long-term retention beyond `WEBUI_RETENTION_HOURS`.
- Multi-user access differentiation — every valid web UI key has identical access today.

## Steps

1. **Package skeleton** (`src/llm_home_lab_webui/`) — `__init__.py`, `main.py` (entry point),
   `app.py` (`create_webui_app(...)`), sibling to `src/llm_home_lab/` at the `src/` level (a
   separate installable/importable package, matching the spec's "distinct process" requirement).
2. **Models** (`models.py`) — `Sample` (`ts: datetime`, `metric_name: str`, `host_id: str | None`,
   `value: float`), `NodeSnapshot`/`AlertSnapshot` (thin wrappers around the orchestrator's own
   `/v1/nodes`/`/v1/alerts` JSON shapes, stored as the "latest snapshot" rather than re-modeled).
3. **`store.py`** — `WebUiStore(db_path: str, retention: timedelta)` following
   `state/sqlite_base.py`'s `SqliteStore` pattern (own `_connection()` contextmanager, `PRAGMA
   journal_mode=WAL`, schema-on-init):
   - `record_samples(samples: list[Sample]) -> None` — bulk insert.
   - `prune(at: datetime) -> None` — `DELETE FROM samples WHERE ts < ?` using `at - retention`.
   - `series(metric_name: str, since: datetime) -> list[Sample]`.
   - `latest_nodes()`/`latest_alerts()` / `record_latest_nodes(...)`/`record_latest_alerts(...)` —
     single-row-per-key "latest snapshot" tables (`UPSERT` by a fixed key), separate from the
     time-series `samples` table since these are point-in-time, not historical series.
4. **`poller.py`** — `run_poll_loop(client: OrchestratorDiagnosticsClient, store: WebUiStore,
   interval_s: float)`:
   - reuses `OrchestratorDiagnosticsClient` from `llm_home_lab.tui.client` unchanged (imported
     across the package boundary — both are part of this monorepo's `src/`) for `list_nodes()`,
     `list_alerts()`, `fetch_metrics_text()`.
   - on each tick: parses metrics via `llm_home_lab.diagnostics.metrics_parser.parse_metrics_text`,
     writes `Sample` rows for queue depth and each host's token usage, upserts the latest
     nodes/alerts snapshots, then calls `store.prune(now)`.
   - on a `DiagnosticsClientError`: records nothing new (keeps last snapshot as-is) and sets an
     in-memory `last_error_at`/`stale_since` the API layer reads — this is the mechanism behind the
     spec's "stale, not blank" behavior.
5. **`api.py`**:
   - `GET /api/nodes` → latest snapshot + `stale_since` (`null` if the last poll succeeded).
   - `GET /api/alerts` → latest snapshot + `stale_since`.
   - `GET /api/series?metric=<name>&since=<iso8601>` → `store.series(...)`, 400 on an unknown
     `metric` name (validated against a fixed allow-list matching what `poller.py` actually
     records).
6. **`auth.py`** — `WebUiKeyStore.from_file(path)` (same `from_file`-at-startup shape as
   `ApiKeyStore`), a FastAPI dependency checking `Authorization: Bearer` against it; applied to
   every `/api/*` route. Missing `config/webui_keys.json` at startup → empty store (every request
   401s) rather than a crash, matching `_load_key_store()`'s missing-file convention — but unlike
   the orchestrator, there is no `WEBUI_AUTH_ENABLED=false` escape hatch, since this is
   browser-facing on the network by design.
7. **Static frontend** (`static/index.html`, `static/app.js`, `static/style.css`) — vanilla JS
   polling `/api/nodes`, `/api/alerts`, `/api/series` on an interval matching
   `WEBUI_POLL_INTERVAL_S`; three sections mirroring the TUI's three panels, plus `<canvas>`
   line charts for series data (hand-rolled minimal drawing, no charting library). Prompts for a
   Bearer key on first load (stored in `sessionStorage`, resent as the `Authorization` header on
   every `/api/*` call) if a request 401s.
8. **Packaging**:
   - `pyproject.toml`: `[project.optional-dependencies] webui = []` (no new runtime deps — reuses
     `fastapi`/`uvicorn`/`httpx` already declared); `[project.scripts] llm-home-lab-webui =
     "llm_home_lab_webui.main:run"`.
   - `docker-compose.yml`: add a commented-out-by-default `webui` service block (image built from
     the same `Dockerfile`, different entry command/port), per the deployment-model idea's
     "both, decide later" direction — not enabled unless the operator opts in.
9. **Tests**:
   - `test_webui_store.py` — insert/prune/series roundtrip, retention boundary, upsert-latest
     semantics.
   - `test_webui_poller.py` — mocked `OrchestratorDiagnosticsClient`: successful tick writes
     samples and clears `stale_since`; a `DiagnosticsClientError` sets `stale_since` and leaves
     prior samples untouched.
   - `test_webui_api.py` — `/api/nodes`/`/api/alerts` stale-marker behavior, `/api/series` shape
     and unknown-metric 400, auth required on all three.
   - `test_webui_auth.py` — valid/invalid/expired key, missing key file.
10. **Docs**: add a "Web dashboard" section to the root `README.md`'s Quickstart (optional install
    via `uv sync --extra webui`, `config/webui_keys.json` example, `docker compose --profile webui
    up` or equivalent invocation) — concrete usage only, per the "README is user-facing"
    convention.
11. **Verification** — `uv run pytest --cov=llm_home_lab_webui`, `uv run ruff check .`, `uv run
    ruff format --check .`, `uv run mypy src`; then the `verify` skill: run the web UI against a
    real running orchestrator, open the dashboard in a browser, confirm live values, a chart
    populating over a few poll cycles, and the stale banner appearing when the orchestrator is
    stopped.

Implemented test-first (`tdd` skill), in this order: `WebUiStore` (pure, no FastAPI/HTTP) →
`poller.py` (mocked client) → `api.py` + `auth.py` (FastAPI test client) → static frontend →
packaging/docs last.

## Risks

- **Two-package layout (`llm_home_lab` + `llm_home_lab_webui`) is new to this repo** — every prior
  milestone lived in one package. Mitigated by keeping the cross-import surface minimal and
  explicit (`OrchestratorDiagnosticsClient`, `parse_metrics_text` only) rather than reaching into
  orchestrator internals.
- **Hand-rolled `<canvas>` charting** carries more maintenance burden than a library, but avoids a
  new frontend toolchain — acceptable at this project's current scope (a handful of line charts);
  revisit if the frontend's complexity grows meaningfully.
- **No login/session UX beyond a Bearer prompt** — acceptable for a single-operator home lab;
  flagged in the spec's Open Questions as the first thing to revisit if multiple people need
  access.
- **`samples` table has no cap on rows within the retention window** — fine at 15s intervals and a
  handful of metrics, but a much shorter interval or many more hosts could grow the table faster
  than expected; revisit if profiling shows it.

## Open Questions

- Same as the spec's: retention/interval tuning, whether auth needs to grow beyond static keys, and
  whether this ships in this repo long-term — all deferred pending real usage.
