# TUI Operator Dashboard Plan

## Status

completed

## Related

- Spec: [tui-operator-dashboard](../specs/20260719-tui-operator-dashboard.md)
- Idea: [operator-observability-dashboards](../ideas/operator-observability-dashboards.md)
- Depends on: [`api/app.py`](../../src/llm_home_lab/api/app.py) (`/v1/nodes`, `/v1/alerts`,
  `/metrics` — read-only, unmodified by this plan)
- Depends on: [`security/key_store.py`](../../src/llm_home_lab/security/key_store.py)
  (`allowed_path_prefixes` — a client entry, no code change)
- Issue: (to be created, milestone M5)

## Scope

Build `src/llm_home_lab/diagnostics/metrics_parser.py` (shared with the future web UI plan) and
`src/llm_home_lab/tui/` (`client.py`, `app.py`), a new `tui` optional dependency group, and a new
`llm-home-lab-tui` entry point. No orchestrator endpoint changes — this plan is a pure client.

Concrete decisions for this plan (resolving the spec's deferred questions):

- **Widget layout**: three stacked `Textual` `DataTable` widgets (Nodes, Alerts, Queue & Tokens) in
  a single-screen `App`, refreshed in place on each poll tick rather than re-mounting — the simplest
  layout that satisfies the spec's three panels.
- **Shared parser location**: `metrics_parser.py` lives in a new `src/llm_home_lab/diagnostics/`
  package (not inside `tui/`) from the start, since the web UI plan will import the same module —
  avoids a later move/rename once M6 starts.
- **Distribution**: ships inside this repo as the `tui` optional dependency group (`uv sync
  --extra tui`), not a separate package — matches the spec's proposed default.

Out of scope for this plan:

- Any new orchestrator endpoint or change to `/v1/nodes`, `/v1/alerts`, or `/metrics`.
- The web management UI (separate plan, milestone M6) — this plan only builds the terminal client
  and the shared parser module it will later import.

## Steps

1. **`diagnostics` package** (`src/llm_home_lab/diagnostics/`, new package sibling to
   `observability/`) — `__init__.py` exporting `parse_metrics_text`.
2. **`metrics_parser.py`** — `parse_metrics_text(body: str) -> ParsedMetrics` where `ParsedMetrics`
   (in `diagnostics/models.py`) holds `queue_depth: int | None` and
   `token_usage_total: dict[str, int]`, parsed by matching the exact metric names
   `MetricsRegistry.render_prometheus` emits (`llm_home_lab_queue_depth`,
   `llm_home_lab_token_usage_total{host_id="..."}`). Missing/unparseable lines leave the
   corresponding field `None`/absent rather than raising — the spec's "fails visibly, not
   silently" requirement is enforced by the caller (TUI/web UI) checking for `None`, not by this
   function raising.
3. **`tui` package** (`src/llm_home_lab/tui/`):
   - `client.py` — `OrchestratorDiagnosticsClient(base_url, api_key)` with async `list_nodes()`,
     `list_alerts()`, `fetch_metrics_text()`, each a single `httpx.AsyncClient` GET with the Bearer
     header; raises a small `DiagnosticsClientError(kind: Literal["connection", "unauthorized",
     "server_error"])` on failure so `app.py` can pick the right banner without inspecting
     `httpx` exceptions directly.
   - `app.py` — `DashboardApp(textual.app.App)`:
     - `__init__(client, interval_s, parse_metrics_text)`.
     - `on_mount` starts a `set_interval(interval_s, self.poll)` timer.
     - `poll()` calls all three client methods concurrently (`asyncio.gather`, `return_exceptions
       =True`), updates the three `DataTable`s on success, and on a `DiagnosticsClientError` shows
       a `Banner`/status widget: `kind="unauthorized"` → "not authorized — check API key";
       `kind="connection"` → "cannot reach orchestrator, retrying"; `kind="server_error"` → "
       orchestrator error, retrying". The banner clears on the next successful poll.
     - Queue & Tokens table renders `queue_depth`/each `token_usage_total` entry as "unavailable"
       when `ParsedMetrics` fields are `None`/missing, per the spec.
     - `run(argv=None)` module-level function: argparse for `--base-url`, `--api-key`,
       `--interval`, reading `ORCHESTRATOR_BASE_URL`/`ORCHESTRATOR_API_KEY` as fallbacks; exits
       with a clear error if no API key is available from either source (no insecure default).
4. **Packaging** (`pyproject.toml`):
   - `[project.optional-dependencies] tui = ["textual>=0.60"]`.
   - `[project.scripts] llm-home-lab-tui = "llm_home_lab.tui.app:run"`.
5. **Tests**:
   - `test_metrics_parser.py` — known-good scrape text → expected `ParsedMetrics`; missing lines →
     `None`/absent fields, not an exception.
   - `test_diagnostics_client.py` — against a mocked HTTP transport (matching this repo's existing
     `httpx.MockTransport` pattern used for backend adapter tests): happy path per method, `401` →
     `DiagnosticsClientError(kind="unauthorized")`, connection error → `kind="connection"`, `500`
     → `kind="server_error"`.
   - `test_dashboard_app.py` — using Textual's `App.run_test()` harness: a successful poll
     populates all three tables; a `DiagnosticsClientError` of each kind shows the matching banner
     text and clears on the next successful poll; a missing metric renders "unavailable" in the
     Queue & Tokens table without affecting the other two tables.
6. **Docs**: add a "Terminal dashboard" section to the root `README.md`'s Quickstart (installation
   via `uv sync --extra tui`, example `llm-home-lab-tui --base-url ... --api-key ...` invocation,
   and a one-line note on adding the `config/api_keys.json` client entry) — per the "README is
   user-facing" convention, concrete usage steps only, no milestone/issue references.
7. **Verification** — `uv run pytest --cov=llm_home_lab`, `uv run ruff check .`, `uv run ruff
   format --check .`, `uv run mypy src`; then the `verify` skill: run the TUI against a real
   running orchestrator + LM Studio (not just mocked transports) to confirm the three panels
   populate and the error banners actually trigger against a genuinely stopped orchestrator.

Implemented test-first (`tdd` skill): `metrics_parser.py` (pure, no I/O) → `client.py` (mocked
transport) → `app.py` (Textual test harness) → packaging/docs last.

## Risks

- **Textual is unresearched against alternatives** (per the idea doc's open question) — proceeding
  with Textual as the pragmatic default since it's the most common choice for modern Python TUIs;
  revisit only if it proves a poor fit during implementation.
- **Parser drifts from `MetricsRegistry`'s actual output** if metric names change in a future
  observability plan — mitigated by `test_metrics_parser.py` asserting against the exact current
  format, so a mismatch fails a test rather than silently misreporting; still requires a human to
  remember to update the parser when the emitting side changes (no shared schema/contract test
  across the two modules today).
- **Concurrent `asyncio.gather` polling** means one slow/hanging endpoint could delay the whole
  poll tick — mitigated by a per-request `httpx` timeout (reuse this repo's existing backend
  adapter timeout convention) so a hang surfaces as a `connection`-kind error within a bounded
  time rather than blocking indefinitely.

## Open Questions

- Same as the spec's: whether to add a dedicated JSON diagnostics endpoint for queue
  depth/token usage instead of parsing Prometheus text, and whether the TUI should ever ship
  outside this repo — both deferred pending real usage.

## Addendum: post-ship additions

During dogfooding, real usage surfaced a backend bug and motivated dashboard enhancements beyond
the original scope:

- **`SchedulingQueue` leak fix**: a request that timed out waiting for a free host slot was never
  removed from the queue, so `queue_depth` stayed permanently inflated and stale entries could
  silently consume a future dispatch turn meant for a live request. Added
  `SchedulingQueue.cancel(request_id, session_id, priority)`, called from `chat_completions` on
  timeout. See `src/llm_home_lab/scheduling/queue.py` and `tests/test_scheduling_queue.py`.
- **Configurable dispatch timeout**: `dispatch_wait_timeout` was hardcoded at 30s with no way to
  tune it without a code change. Added `ORCHESTRATOR_DISPATCH_WAIT_TIMEOUT_S` env var in
  `main.py`.
- **`diagnostics/metrics_parser.py`** gained `p95_latency_ms` parsing.
- **New `tui/rates.py`** (`compute_token_rates`) and **`tui/load_history.py`**
  (`update_load_history`) — pure, tested modules computing client-side tokens/s and a rolling
  per-host load-ratio history, with no new orchestrator endpoint.
- **Dashboard visuals**: `Header`/`Footer`, zebra-striped tables, bordered panels with titles, a
  colored error banner, colored alert severity, and a per-host `Sparkline` (Node Load panel)
  driven by `load_history`.
