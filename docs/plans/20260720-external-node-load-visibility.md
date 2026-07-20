# External Node Load Visibility Plan

## Status

draft

## Related

- Spec: [external-node-load-visibility](../specs/20260720-external-node-load-visibility.md)
- ADR: [0005-lms-cli-for-external-node-load-visibility](../adr/0005-lms-cli-for-external-node-load-visibility.md)
- Depends on: [`api/app.py`](../../src/llm_home_lab/api/app.py) (`/health/ready`'s per-host loop,
  `GET /v1/nodes`, `_node_status`)
- Depends on: [`tui/app.py`](../../src/llm_home_lab/tui/app.py) (Nodes panel/table)
- Issue: (to be created, unattached to a milestone)

## Scope

Build `ExternalLoadProbe` (`src/llm_home_lab/registry/external_load.py`), wire it into
`/health/ready`'s existing per-host loop and `GET /v1/nodes`, add the two new env vars, and extend
the TUI's Nodes panel with an external-load column.

Concrete decisions for this plan (resolving the spec's deferred items):

- **Extracting the host for `--host`**: `urllib.parse.urlparse(base_url).hostname` — stdlib only,
  no new dependency; correctly strips scheme/port from `http://192.168.50.108:1234`.
- **Subprocess invocation**: `asyncio.create_subprocess_exec(lms_binary, "ps", "--host", hostname,
  "--json", stdout=PIPE, stderr=PIPE)`, wrapped in `asyncio.wait_for(..., timeout=timeout_s)`. On
  `TimeoutError`, the process is killed (`proc.kill()`) before returning, so a hung `lms` process
  doesn't linger.
- **Cache storage**: a plain `dict[str, tuple[datetime, ExternalLoadStatus]]` inside
  `ExternalLoadProbe`, keyed by `host_id` (not `base_url`, so a re-registered host with a changed
  address correctly invalidates on its next natural probe rather than serving a stale cached
  result for the old address — mirrors the `_backend_for` capability-change fix from #42).
- **TUI column**: extend the existing Nodes `DataTable` (from
  [tui-operator-dashboard](../specs/20260719-tui-operator-dashboard.md)) with one more column,
  `ext_load`, reusing `_styled_node_status`-style coloring (`_styled_external_load`): `"unavailable"`
  muted/dim, `"idle"` default, anything else styled like a busy indicator (e.g. bold yellow),
  optionally suffixed with `(N queued)` when `queued > 0`.

Out of scope for this plan:

- Any change to routing/scheduling based on external load (spec explicitly keeps this
  informational-only).
- A dedicated web-UI (M6) rendering of this field — M6 isn't built yet; whoever builds it should
  pick this field up the same way it already will for `status`.
- Making `cache_ttl`/probe interval configurable per-host.

## Steps

1. **`registry/external_load.py`** — `ExternalLoadStatus` dataclass; `ExternalLoadProbe` with
   `__init__(lms_binary: str = "lms", timeout_s: float = 5.0, cache_ttl: timedelta =
   timedelta(seconds=15))` and `async def probe(host_id, base_url, at) -> ExternalLoadStatus`
   exactly per the spec's cache/failure-handling behavior. All failure modes (`FileNotFoundError`,
   `asyncio.TimeoutError`, non-zero exit, `json.JSONDecodeError`, malformed entries) converge on
   the same `available=False` result — a single internal `_probe_uncached` that always returns a
   status, never raises, called by `probe` only when the cache is stale.
2. **App wiring** (`src/llm_home_lab/api/app.py`):
   - `create_app` gains a required `external_load_probe: ExternalLoadProbe` parameter (matching
     this codebase's established pattern of required collaborators — see `metrics_registry`,
     `alert_evaluator`).
   - `/health/ready`'s existing per-host loop additionally calls
     `await external_load_probe.probe(host.host_id, host.capabilities.base_url, now)` per host,
     storing results the same request-scoped way `_node_status` already reads live state (no new
     module-level dict in `app.py` — `ExternalLoadProbe` owns its own cache internally, `app.py`
     just calls `probe` and reads the return value fresh each time `GET /v1/nodes` needs it, same
     as `_node_status` calling `health_monitor.is_healthy` fresh).
   - `GET /v1/nodes` calls `external_load_probe.probe(...)` per host (cheap: cache hit in the
     common case) and adds `"external_load": {"available": ..., "status": ..., "queued": ...}` to
     each entry.
3. **`main.py`** — `ORCHESTRATOR_LMS_BINARY_PATH` (default `"lms"`) and
   `ORCHESTRATOR_EXTERNAL_LOAD_PROBE_INTERVAL_S` (default `15`) read into
   `ExternalLoadProbe(lms_binary=..., cache_ttl=timedelta(seconds=...))` at
   `create_default_app()`.
4. **TUI** (`src/llm_home_lab/tui/app.py`):
   - `nodes_table.add_columns(..., "ext_load")`.
   - `_render_nodes` reads `host["external_load"]` from the (already-extended) `/v1/nodes`
     response and renders `_styled_external_load(...)`.
5. **Tests**:
   - `test_external_load_probe.py` — inject a fake subprocess runner (matching this repo's
     transport-injection convention, e.g. a `run: Callable[..., Awaitable[...]]` constructor
     param defaulting to `asyncio.create_subprocess_exec`, so tests don't spawn a real
     process): known-good JSON → correct aggregation (multi-model sum of `queued`, non-idle
     precedence); empty array → idle/queued=0; missing binary → unavailable; non-zero exit →
     unavailable; timeout → unavailable, process killed; cache hit within `cache_ttl` → runner not
     called a second time; cache miss after `cache_ttl` elapses → runner called again; a
     re-registered `host_id` with a different `base_url` is probed at its new address (not served
     a stale cached result keyed only by the old address's data).
   - `test_node_registry_endpoints.py` — `GET /v1/nodes` includes `external_load` per host, sourced
     from a fake `ExternalLoadProbe`.
   - `test_dashboard_app.py` — Nodes panel renders the new column for available/unavailable/busy
     cases.
6. **Docs**: README gets a short note under the TUI/nodes sections: `lms` is optional, install via
   `curl -fsSL https://lmstudio.ai/cli/install.sh | bash` if you want external-load visibility;
   everything else works without it. `Dockerfile` gets the same install step added (commented as
   optional if the base image build wants to skip it) per ADR-0005.
7. **Verification** — `uv run pytest --cov=llm_home_lab`, `uv run ruff check .`, `uv run ruff
   format --check .`, `uv run mypy src`; then the `verify` skill against the real two-node setup
   (Mac + Windows) already in use this session: confirm `GET /v1/nodes` reports real
   `external_load` for both, confirm the TUI renders it, and confirm pulling the Windows machine's
   network cable / powering it off degrades only that host's `external_load` to unavailable
   without affecting the rest of `/v1/nodes` or `/health/ready`.

Implemented test-first (`tdd` skill): `ExternalLoadProbe` fully (pure async logic, injected runner,
no real subprocess in tests) → app wiring → TUI column → Dockerfile/README docs last.

## Risks

- **`lms ps --host` is an undocumented CLI behavior**, not a stable, versioned API — a future LM
  Studio release could change its output shape or remove `--host` entirely without the same
  deprecation care as the REST API. Mitigated by treating any parse failure as `available=False`
  rather than crashing, but a silent behavior change (e.g., a renamed field) could make this
  quietly report `queued=0`/`idle` incorrectly rather than `unavailable` — worth a periodic manual
  sanity check against real LM Studio, not just relying on tests against a fixed known-good JSON
  fixture.
- **Docker image growth**: the `lms` binary is ~60MB+ (confirmed for macOS; Linux binary size not
  yet confirmed) — adds real weight to the orchestrator's image for a feature many operators won't
  use. Since it's an optional install step (per ADR-0005), an operator/maintainer building a
  minimal image can skip it.
- **Subprocess spawn cost**: even with caching, a probe cycle spawns one process per registered
  host that needs refreshing — fine at home-lab node counts (a handful), would not scale to many
  dozens of hosts without a longer interval.
- **Killing a timed-out subprocess** needs care to avoid zombie processes — `proc.kill()` +
  `await proc.wait()` in the timeout path, not just `kill()` alone.

## Open Questions

- Same as the spec's: per-host-configurable interval, TUI exact styling details, and the
  revisit-if-LM-Studio-adds-this-to-REST-API trigger — all deferred.
