# Per-host failure visibility

## Status

completed

## Related

- Spec: [per-host-failure-visibility](../specs/20260804-per-host-failure-visibility.md)
- Idea: [completion-quality-health-signal](../ideas/20260727-completion-quality-health-signal.md)
  (Option B)
- Plan: [completion-quality-health-signal](20260804-completion-quality-health-signal.md) —
  Option A (#50), the prerequisite this plan builds on
- Issue: #51 — surface per-host failure counts in `/v1/nodes` and the TUI (this plan)
- Blocked by: #50 (shipped) — `HealthMonitor` now sees real completion outcomes, not just
  liveness
- Blocks: #52 (factor failure rate into routing) — deliberately separate, not touched here

## Scope

Add a read-only per-host `recent_failures` count, derived entirely from `HealthMonitor`'s
existing bounded `history` deque, and surface it through `GET /v1/nodes` and the TUI Nodes table.
No new health-tracking state, no change to `record_probe`/`is_healthy`/the failover state
machine, no change to routing (`_eligible_candidates`) — all confirmed out of scope in the spec.

## Steps

1. **`HealthMonitor.failure_count`** (`src/llm_home_lab/health/monitor.py`), placed next to
   `health_score()` since it reads the same state:

   ```python
   def failure_count(self, backend_id: str) -> int:
       state = self._states.get(backend_id)
       if state is None:
           return 0
       return sum(1 for healthy in state.history if not healthy)
   ```

   Test first, in `tests/test_health_monitor.py`, alongside the existing `has_probe_history`/
   `health_score` tests:
   - a backend with no recorded probes reports `failure_count` `0`
   - a backend with 2 failures and 3 successes recorded reports `failure_count` `2`
   - a failure that ages out of the bounded `history` window (default `maxlen=20`) stops being
     counted once it's pushed out

2. **`GET /v1/nodes`** (`src/llm_home_lab/api/app.py`). Add a small helper next to
   `_node_status` (~line 174):

   ```python
   def _node_health(host_id: str) -> dict[str, int]:
       return {"recent_failures": health_monitor.failure_count(host_id)}
   ```

   and add `"health": _node_health(host.host_id)` to the per-host dict in `list_nodes()`
   (~line 406-439), alongside the existing `"status": _node_status(...)` entry.

   Test in `tests/test_node_registry_endpoints.py` (it already builds an app with a real
   `HealthMonitor()` instance — see `_app()`): register a host, call
   `health_monitor.record_probe(host_id, healthy=False, at=...)` directly a couple of times, then
   assert `GET /v1/nodes`'s `health.recent_failures` matches. Also cover the zero-history case
   (a freshly registered, never-probed host reports `health.recent_failures == 0`).

3. **TUI Nodes table** (`src/llm_home_lab/tui/app.py`):
   - Add a styling helper next to `_styled_node_status`/`_styled_external_load` (~line 88-101):

     ```python
     def _styled_failure_count(recent_failures: object) -> Text:
         count = recent_failures if isinstance(recent_failures, int) else 0
         if count == 0:
             return Text("0", style="dim")
         return Text(str(count), style="bold red")
     ```

   - Add `"errors"` to the `add_columns(...)` call in `on_mount` (~line 512-514), positioned
     after `"status"` so the two health-adjacent columns sit together.
   - In `_render_nodes` (~line 577-593), read `host.get("health", {})` (defensive default, in
     case an older orchestrator without this field is being polled) and pass
     `health.get("recent_failures", 0)` through `_styled_failure_count` as the new column value,
     inserted right after the `status` cell.

   Test in `tests/test_dashboard_app.py`, following the existing `_node()` helper /
   `_FakeClient` / `table.get_cell(row_key, column_key)` pattern used by
   `test_an_offline_node_status_is_styled_red_and_stays_listed` and the external-load styling
   tests:
   - a node with `health.recent_failures > 0` renders a non-zero, red-styled value in the
     `errors` column
   - a node with `health.recent_failures == 0` (or a missing `health` key, for
     forward-compatibility with a not-yet-updated field) renders `0` with no alarming style

4. **Verification** — `uv run pytest --cov=llm_home_lab`, `uv run ruff check .`, `uv run ruff
   format --check .`, `uv run mypy src`.

## Risks

- **Stale reads of a bounded window.** `failure_count` reflects only the same last-20-probe
  window `health_score` already uses — this is called out explicitly in the spec's Behavior
  section so it doesn't surprise anyone comparing the two numbers against a longer memory of an
  incident.
- **TUI forward-compatibility.** Reading `host.get("health", {})` defensively (rather than
  `host["health"]`) means the TUI degrades to showing `0` rather than crashing if it's ever run
  against an orchestrator build that predates this field.

## Open Questions

None — resolved during spec review (bounded-window count, separate TUI column, nested `health`
object).
